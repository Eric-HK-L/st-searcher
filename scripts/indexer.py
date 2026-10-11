#!/usr/bin/env python3
"""
通用归档语料索引构建工具。

语料是一组压缩包（分卷 zip 或独立 zip/rar/7z，内部还可以套归档），
本工具只读取归档的**目录列表**建立索引，不解压正文。

归档清单由 config.json 决定（split_archive / split_parts / extra_archives），
代码里不写死任何具体文件名。

可选的可读元数据（放在 storage/meta/，两个 TSV，UTF-8，带表头）：
  records.tsv    id, hash(sha256), size, file(原始路径), fullpath(归档内路径), error, name
  catalog.tsv    id, cnname, enname, ..., category, lang, format, file, filename ...

子命令:
  build-meta   用两个 TSV 建 SQLite + CSV 索引（秒级，无需解压）
  scan-inner   扫描归档内部的真实文件名，写入 inner_files 表（可选抽样）
  search       在索引里检索
  stats        打印索引统计
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    CORPUS_DIR,
    DB_PATH,
    INDEX_DIR,
    META_DIR,
    ARCHIVE_EXTRA,
    PROJECT_DIR,
    ARCHIVE_PARTS,
    corpus_ready,
    missing_corpus_files,
)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS archives (
    fullpath      TEXT PRIMARY KEY,   -- 数据集内路径, e.g. data/rar/2009/c/xxx.rar
    collection    TEXT,               -- data | data2
    year          TEXT,
    kind          TEXT,               -- zip | rar | 7z | srt | ass | ssa
    shard         TEXT,
    id            INTEGER,            -- 对应 subtitle.tsv.id, data2 组为 NULL
    hash          TEXT,               -- 归档文件内容的 sha256
    size          INTEGER,
    original_path TEXT,               -- 语料中的原始路径
    error         INTEGER,            -- 0 正常, 1/2 表示导出时有异常
    subtitle_name TEXT,               -- 归档内文件的可读名字（可选）
    cnname        TEXT,               -- 中文名
    enname        TEXT,               -- 英文名
    resourceid    INTEGER,
    season        INTEGER,
    episode       INTEGER,
    segment       TEXT,
    category      TEXT,               -- 分类（可选，来自元数据）
    lang          TEXT,               -- /简体/繁体/英文/中英/
    format        TEXT,               -- /SRT/ASS/
    source        TEXT,               -- original / zimuzu / trans / official
    views         INTEGER,
    downloads     INTEGER,
    dateline      INTEGER
);

CREATE INDEX IF NOT EXISTS ix_arch_year     ON archives(year);
CREATE INDEX IF NOT EXISTS ix_arch_kind     ON archives(kind);
CREATE INDEX IF NOT EXISTS ix_arch_id       ON archives(id);
CREATE INDEX IF NOT EXISTS ix_arch_cnname   ON archives(cnname);
CREATE INDEX IF NOT EXISTS ix_arch_enname   ON archives(enname);
CREATE INDEX IF NOT EXISTS ix_arch_coll     ON archives(collection);

-- 归档内部真实文件名（由 scan-inner 填充）
CREATE TABLE IF NOT EXISTS inner_files (
    fullpath   TEXT NOT NULL,   -- 外层归档路径
    inner_path TEXT NOT NULL,   -- 归档内路径
    size       INTEGER,
    ext        TEXT,
    PRIMARY KEY (fullpath, inner_path)
);
CREATE INDEX IF NOT EXISTS ix_inner_ext ON inner_files(ext);
CREATE INDEX IF NOT EXISTS ix_inner_fp  ON inner_files(fullpath);
"""


# --------------------------------------------------------------------------
# 分卷 zip 的随机读取视图
# --------------------------------------------------------------------------
class ConcatFile(io.RawIOBase):
    """把分卷 zip 的各个 part 拼成一个可 seek 的只读文件对象。"""

    def __init__(self, paths):
        self.paths = [str(p) for p in paths]
        self.sizes = [os.path.getsize(p) for p in self.paths]
        self.offsets, total = [], 0
        for s in self.sizes:
            self.offsets.append(total)
            total += s
        self.total = total
        self.pos = 0
        self._fh = None
        self._idx = None

    def _fh_for(self, idx):
        if self._idx != idx:
            if self._fh:
                self._fh.close()
            self._fh = open(self.paths[idx], "rb")
            self._idx = idx
        return self._fh

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.total + offset
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.total - self.pos
        out = bytearray()
        while n > 0 and self.pos < self.total:
            idx = None
            for i, off in enumerate(self.offsets):
                if off <= self.pos < off + self.sizes[i]:
                    idx = i
                    break
            if idx is None:
                break
            fh = self._fh_for(idx)
            fh.seek(self.pos - self.offsets[idx])
            chunk = fh.read(min(n, self.offsets[idx] + self.sizes[idx] - self.pos))
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, b):
        d = self.read(len(b))
        b[: len(d)] = d
        return len(d)


def open_archives(which="all"):
    """yield (collection, ZipFile) —— 外层归档容器。"""
    if which in ("all", "data") and ARCHIVE_PARTS[0].exists():
        yield "data", zipfile.ZipFile(ConcatFile(ARCHIVE_PARTS))
    if which in ("all", "data2"):
        for p in ARCHIVE_EXTRA:
            if p.exists():
                yield "data2", zipfile.ZipFile(p)


# --------------------------------------------------------------------------
# build-meta
# --------------------------------------------------------------------------
def _to_int(v, default=None):
    v = (v or "").strip()
    if not v:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def _scan_archives_without_meta():
    """没有元数据时，直接从归档容器里列出条目（只有路径和大小）。

    这样「元数据」就是纯粹的锦上添花：有则显示可读片名，没有也能检索。
    """
    rows = []
    for coll, z in open_archives("all"):
        prefix = coll + "/"
        for info in z.infolist():
            name = info.filename
            if name.endswith("/") or not name.startswith(prefix):
                continue
            parts = name.split("/")
            rows.append((
                name,
                coll,
                parts[2] if len(parts) > 2 else "",
                os.path.splitext(name)[1].lstrip(".").lower(),
                parts[3] if len(parts) > 3 else "",
                None, None, info.file_size, name, 0, os.path.basename(name),
                None, None, None, None, None, None, None, None, None, None,
                None, None, None,
            ))
    return rows


def build_meta(db_path=DB_PATH, csv_out=None):
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    has_catalog = (META_DIR / "catalog.tsv").exists()
    has_records = (META_DIR / "records.tsv").exists()

    if not (has_catalog and has_records):
        print(f"[build-meta] 未找到元数据（{META_DIR}），改为直接扫描归档容器；")
        print("[build-meta] 检索仍可用，只是结果里没有可读片名/分类。")
        rows = _scan_archives_without_meta()
        return _write_archives(rows, db_path, csv_out, meta_used=False)

    # 1) catalog.tsv: id -> 条目级元数据
    sub = {}
    with open(META_DIR / "catalog.tsv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            sub[row["id"]] = row

    # 2) records.tsv: 一行 = 一个归档文件
    rows = []
    with open(META_DIR / "records.tsv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            fullpath = row["fullpath"]
            parts = fullpath.split("/")
            sid = row["id"].strip()
            meta = sub.get(sid, {})
            rows.append(
                (
                    fullpath,
                    parts[0],
                    parts[2] if len(parts) > 2 else "",
                    os.path.splitext(fullpath)[1].lstrip(".").lower(),
                    parts[3] if len(parts) > 3 else "",
                    _to_int(sid),
                    row["hash"],
                    _to_int(row["size"]),
                    row["file"],
                    _to_int(row["error"], 0),
                    row["subtitle-name"],
                    meta.get("cnname"),
                    meta.get("enname"),
                    _to_int(meta.get("resourceid")),
                    _to_int(meta.get("season")),
                    _to_int(meta.get("episode")),
                    meta.get("segment"),
                    meta.get("category"),
                    meta.get("lang"),
                    meta.get("format"),
                    meta.get("source"),
                    _to_int(meta.get("views")),
                    _to_int(meta.get("downloads")),
                    _to_int(meta.get("dateline")),
                )
            )

    return _write_archives(rows, db_path, csv_out, meta_used=True)


ARCHIVE_COLS = (
    "fullpath,collection,year,kind,shard,id,hash,size,original_path,error,"
    "subtitle_name,cnname,enname,resourceid,season,episode,segment,category,"
    "lang,format,source,views,downloads,dateline"
)


def _write_archives(rows, db_path, csv_out, meta_used):
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.executemany(
        f"INSERT OR REPLACE INTO archives ({ARCHIVE_COLS}) VALUES ({','.join('?' * 24)})", rows)
    con.commit()

    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(ARCHIVE_COLS.split(","))
            w.writerows(rows)
    n = con.execute("SELECT count(*) FROM archives").fetchone()[0]
    n_join = con.execute("SELECT count(*) FROM archives WHERE id IS NOT NULL").fetchone()[0]
    n_name = con.execute("SELECT count(*) FROM archives WHERE subtitle_name<>''").fetchone()[0]
    con.close()
    if not meta_used:
        print(f"[build-meta] {db_path}  rows={n}  （无元数据模式）")
    else:
        print(f"[build-meta] {db_path}  rows={n}  可 join 元数据={n_join}  有可读名字={n_name}")
    if csv_out:
        print(f"[build-meta] CSV 已写出 {csv_out}")
    return db_path


# --------------------------------------------------------------------------
# scan-inner
# --------------------------------------------------------------------------
# bsdtar -tvf 的日期列：老文件是 "Mar 15  2008"，新的是 "Mar 15 12:34"，
# 也有 ISO 风格的 "2020-01-01 12:34"。名字在日期之后。
_BSDTAR_DATE = re.compile(
    r"^(?:[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{4}|\d{2}:\d{2})"
    r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+")


def _bsdtar_rows(path: str):
    """bsdtar -tvf -> [(名字, 大小)]；格式不认识就 None。

    形如：-rw-r--r--  0 0      0      220030 Mar 15  2008 名字.ass
    日期列长度不固定（老文件 Mar 15  2008 / 新文件 Mar 15 12:34 / ISO 格式），
    所以按正则剥。剥不掉就整体放弃，宁可退化也绝不把日期混进文件名 ——
    那样下载会 404。

    注意不能因为 returncode != 0 就丢弃输出：固实 rar 会报
    "RAR solid archive support unavailable" 并返回 1，但目录列表是好的。
    """
    out = subprocess.run(["bsdtar", "-tvf", path], capture_output=True, timeout=180)
    rows = []
    for line in out.stdout.decode("utf-8", errors="replace").splitlines():
        if not line or line.startswith("total "):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            if line.rstrip().endswith("/"):
                continue                     # 目录
            return None
        mode, size, rest = parts[0], parts[4], parts[5]
        if mode[:1] in ("d", "l", "b", "c", "p"):     # 目录 / 链接 / 设备
            continue
        m = _BSDTAR_DATE.match(rest)
        if not m:
            return None
        name = rest[m.end():].rstrip()
        if not name:
            continue
        try:
            rows.append((name, int(size)))
        except ValueError:
            rows.append((name, -1))
    return rows or None


def _bsdtar_names(path: str):
    """退路：只列名字，大小记 -1（界面上显示「大小未知」）。"""
    out = subprocess.run(["bsdtar", "-tf", path], capture_output=True, timeout=180)
    names = out.stdout.decode("utf-8", errors="replace").splitlines()
    rows = [(n.rstrip("/"), -1) for n in names if n and not n.endswith("/")]
    return rows or None


def _list_with_bsdtar(blob: bytes, suffix: str):
    """rar / 7z 等交给 libarchive(bsdtar) 读取目录，带回每个文件的大小。"""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(blob)
        tmp.close()
        rows = _bsdtar_rows(tmp.name)
        if rows is None:
            rows = _bsdtar_names(tmp.name)       # 退化成只有名字
        return rows
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _list_inner(fullpath: str, blob: bytes):
    """返回 [(inner_path, size), ...]"""
    ext = os.path.splitext(fullpath)[1].lower()
    if ext == ".zip":
        try:
            iz = zipfile.ZipFile(io.BytesIO(blob))
            return [
                (i.filename, i.file_size) for i in iz.infolist() if not i.filename.endswith("/")
            ]
        except zipfile.BadZipFile:
            return None
    if blob[:4] == b"Rar!" or blob[:6] == b"7z\xbc\xaf\x27\x1c":
        return _list_with_bsdtar(blob, ext)
    return []  # 单文件归档，没有内部结构


def _scan_batch(payload):
    """一个 worker 只打开一次外层归档，处理整批条目。"""
    collection, names = payload
    out = []
    zmap = {coll: z for coll, z in open_archives(collection)}
    z = zmap.get(collection)
    if z is None:
        return [(n, None, "archive-not-found") for n in names]
    for name in names:
        try:
            blob = z.read(name)
        except Exception as e:
            out.append((name, None, f"read-error: {e}"))
            continue
        out.append((name, _list_inner(name, blob), None))
    return out


def scan_inner(db_path=DB_PATH, which="all", limit=None, seed=None, workers=8,
               skip_existing=True, batch=500):
    con = sqlite3.connect(db_path)
    q = "SELECT collection, count(*) FROM archives"
    if skip_existing:
        q += " WHERE fullpath NOT IN (SELECT DISTINCT fullpath FROM inner_files)"
    q += " GROUP BY 1"
    by_coll = {c: n for c, n in con.execute(q).fetchall()}

    targets = {c: [] for c in by_coll}
    for coll in by_coll:
        rows = con.execute(
            "SELECT fullpath FROM archives WHERE collection=?"
            + (" AND fullpath NOT IN (SELECT DISTINCT fullpath FROM inner_files)" if skip_existing else ""),
            (coll,),
        ).fetchall()
        targets[coll] = [r[0] for r in rows]
    if limit:
        for coll in targets:
            random.Random(seed).shuffle(targets[coll])
            targets[coll] = targets[coll][: max(1, int(limit * len(targets[coll]) / max(1, sum(by_coll.values()))))]

    jobs = []
    for coll, names in targets.items():
        for i in range(0, len(names), batch):
            jobs.append((coll, names[i : i + batch]))
    total = sum(len(n) for n in targets.values())
    print(f"[scan-inner] 待扫描 {total} 个归档, {len(jobs)} 个批次, {workers} 进程")

    done = 0
    t0 = time.time()
    buf = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_scan_batch, j) for j in jobs]
        for fut in as_completed(futs):
            for name, rows, err in fut.result():
                done += 1
                if err:
                    print(f"  !! {name}: {err}", file=sys.stderr)
                    continue
                for inner, size in rows or []:
                    buf.append((name, inner, size, os.path.splitext(inner)[1].lstrip(".").lower()))
            if len(buf) >= 20000:
                con.executemany("INSERT OR IGNORE INTO inner_files VALUES (?,?,?,?)", buf)
                con.commit()
                buf.clear()
            rate = done / max(1e-9, time.time() - t0)
            eta = (total - done) / rate if rate else 0
            print(f"  {done}/{total}  {rate:.0f} 归档/s  ETA {eta/60:.1f} min", flush=True)
    if buf:
        con.executemany("INSERT OR IGNORE INTO inner_files VALUES (?,?,?,?)", buf)
    con.commit()
    n = con.execute("SELECT count(*) FROM inner_files").fetchone()[0]
    print(f"[scan-inner] 用时 {time.time()-t0:.1f}s, inner_files 现有 {n} 行")
    con.close()


# --------------------------------------------------------------------------
# verify —— 校验每个归档的 sha256 是否与元数据一致
# --------------------------------------------------------------------------
def _verify_batch(payload):
    collection, names = payload
    import hashlib

    out = []
    zmap = {coll: z for coll, z in open_archives(collection)}
    z = zmap.get(collection)
    if z is None:
        return [(n, None, None, "archive-not-found") for n in names]
    for name, expect, size in names:
        try:
            b = z.read(name)
        except Exception as e:
            out.append((name, None, None, f"read-error: {e}"))
            continue
        out.append((name, hashlib.sha256(b).hexdigest() == expect, len(b) == size, None))
    return out


def verify(db_path=DB_PATH, which="all", workers=12, batch=400, limit=None, seed=None):
    con = sqlite3.connect(db_path)
    q = "SELECT fullpath, hash, size, collection FROM archives"
    if which != "all":
        q += f" WHERE collection='{which}'"
    rows = [r for r in con.execute(q)]
    if limit:
        random.Random(seed).shuffle(rows)
        rows = rows[:limit]
    jobs = []
    cur = {}
    for fullpath, h, size, coll in rows:
        cur.setdefault(coll, []).append((fullpath, h, size))
    for coll, items in cur.items():
        for i in range(0, len(items), batch):
            jobs.append((coll, items[i : i + batch]))

    total = len(rows)
    done = bad_hash = bad_size = err = 0
    t0 = time.time()
    failed = []
    print(f"[verify] 校验 {total} 个归档, {workers} 进程")
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_verify_batch, j) for j in jobs]):
            for name, hok, sok, e in fut.result():
                done += 1
                if e:
                    err += 1
                    failed.append((name, e))
                elif not hok:
                    bad_hash += 1
                    failed.append((name, "sha256 mismatch"))
                elif not sok:
                    bad_size += 1
                    failed.append((name, "size mismatch"))
            if done % 20000 < 400:
                print(f"  {done}/{total}  {done/max(1e-9,time.time()-t0):.0f} 归档/s", flush=True)
    print(
        f"[verify] 完成 {done}/{total}  用时 {time.time()-t0:.1f}s  "
        f"sha256不符={bad_hash} 大小不符={bad_size} 读取失败={err}"
    )
    if failed:
        p = INDEX_DIR / "verify_failed.tsv"
        with open(p, "w", encoding="utf-8") as f:
            for n, why in failed:
                f.write(f"{n}\t{why}\n")
        print(f"[verify] 失败清单 -> {p}")
    con.close()
    return bad_hash + bad_size + err


# --------------------------------------------------------------------------
# extract —— 按条件把归档内的文件解出来
# --------------------------------------------------------------------------
def _safe(name):
    name = name.replace("\\", "/").lstrip("/")
    name = name.replace("\x00", "")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def _extract_batch(payload):
    collection, items = payload  # items: (fullpath, dest_dir)
    zmap = {coll: z for coll, z in open_archives(collection)}
    z = zmap.get(collection)
    res = []
    for fullpath, dest in items:
        try:
            blob = z.read(fullpath)
        except Exception as e:
            res.append((fullpath, 0, f"read-error: {e}"))
            continue
        os.makedirs(dest, exist_ok=True)
        n = 0
        rows = _list_inner(fullpath, blob)
        if rows is None:
            res.append((fullpath, 0, "cannot-list"))
            continue
        if not rows:
            # 单文件归档：直接落地
            base = os.path.basename(fullpath)
            with open(os.path.join(dest, base), "wb") as f:
                f.write(blob)
            res.append((fullpath, 1, None))
            continue
        # 归档：写出每个内部文件
        ext = os.path.splitext(fullpath)[1].lower()
        if ext == ".zip":
            iz = zipfile.ZipFile(io.BytesIO(blob))
            for info in iz.infolist():
                if info.filename.endswith("/"):
                    continue
                rel = _safe(info.filename)
                if not rel:
                    continue
                p = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(iz.read(info))
                n += 1
        else:
            tmpd = tempfile.mkdtemp()
            try:
                tmp = os.path.join(tmpd, "a" + ext)
                with open(tmp, "wb") as f:
                    f.write(blob)
                subprocess.run(["bsdtar", "-xf", tmp, "-C", dest], check=True, timeout=300)
                n = sum(len(fs) for _, _, fs in os.walk(dest))
            except Exception as e:
                res.append((fullpath, n, f"bsdtar-error: {e}"))
                continue
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)
        res.append((fullpath, n, None))
    return res


def extract(db_path=DB_PATH, keyword=None, year=None, kind=None, lang=None,
            out_dir=None, limit=None, workers=8, dry_run=False):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sql = ["SELECT a.* FROM archives a WHERE 1=1"]
    args = []
    if keyword:
        sql.append(
            "AND (a.cnname LIKE ? OR a.enname LIKE ? OR a.subtitle_name LIKE ?"
            "     OR a.original_path LIKE ?"
            "     OR a.fullpath IN (SELECT fullpath FROM inner_files WHERE inner_path LIKE ?))"
        )
        args += [f"%{keyword}%"] * 5
    if year:
        sql.append("AND a.year=?")
        args.append(str(year))
    if kind:
        sql.append("AND a.kind=?")
        args.append(kind)
    if lang:
        sql.append("AND a.lang LIKE ?")
        args.append(f"%{lang}%")
    if limit:
        sql.append("LIMIT ?")
        args.append(limit)
    rows = con.execute(" ".join(sql), args).fetchall()
    print(f"[extract] 命中 {len(rows)} 个归档")
    if dry_run:
        for r in rows[:20]:
            print("   ", r["fullpath"], "|", r["cnname"], "|", r["subtitle_name"])
        return

    out_dir = Path(out_dir or (INDEX_DIR / "extracted"))
    jobs = {}
    for r in rows:
        stem = os.path.splitext(os.path.basename(r["fullpath"]))[0]
        dest = out_dir / r["collection"] / r["kind"] / str(r["year"]) / stem
        jobs.setdefault(r["collection"], []).append((r["fullpath"], str(dest)))
    payloads = []
    for coll, items in jobs.items():
        for i in range(0, len(items), 100):
            payloads.append((coll, items[i : i + 100]))

    t0 = time.time()
    n_files = 0
    errs = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_extract_batch, p) for p in payloads]):
            for fullpath, n, e in fut.result():
                if e:
                    errs += 1
                    print(f"  !! {fullpath}: {e}", file=sys.stderr)
                n_files += n
    print(f"[extract] 写出 {n_files} 个文件到 {out_dir}  用时 {time.time()-t0:.1f}s  失败 {errs}")

    manifest = out_dir / "manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fullpath", "cnname", "enname", "year", "lang", "format", "subtitle_name"])
        for r in rows:
            w.writerow([r["fullpath"], r["cnname"], r["enname"], r["year"], r["lang"],
                        r["format"], r["subtitle_name"]])
    print(f"[extract] 清单 -> {manifest}")
    con.close()


# --------------------------------------------------------------------------
# search / stats
# --------------------------------------------------------------------------
def search(db_path=DB_PATH, keyword=None, year=None, kind=None, lang=None, limit=30):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sql = [
        "SELECT a.fullpath, a.year, a.kind, a.size, a.cnname, a.enname, a.category,"
        "       a.lang, a.format, a.subtitle_name, a.season, a.episode,"
        "       (SELECT count(*) FROM inner_files i WHERE i.fullpath=a.fullpath) AS n_inner",
        "FROM archives a WHERE 1=1",
    ]
    args = []
    if keyword:
        sql.append("AND (a.cnname LIKE ? OR a.enname LIKE ? OR a.subtitle_name LIKE ?"
                   "     OR a.original_path LIKE ?"
                   "     OR a.fullpath IN (SELECT fullpath FROM inner_files WHERE inner_path LIKE ?))")
        like = f"%{keyword}%"
        args += [like] * 5
    if year:
        sql.append("AND a.year=?")
        args.append(str(year))
    if kind:
        sql.append("AND a.kind=?")
        args.append(kind)
    if lang:
        sql.append("AND a.lang LIKE ?")
        args.append(f"%{lang}%")
    sql.append("ORDER BY a.downloads DESC NULLS LAST LIMIT ?")
    args.append(limit)
    rows = con.execute(" ".join(sql), args).fetchall()
    for r in rows:
        print(
            f"{r['year']} [{r['kind']}] {r['size']:>9,}B  {r['cnname'] or '-':<22}"
            f" {r['enname'] or '':<38} {r['lang'] or '':<26} inner={r['n_inner']}"
        )
        print(f"        {r['fullpath']}")
        if r["subtitle_name"]:
            print(f"        ↳ {r['subtitle_name']}")
    print(f"--- {len(rows)} 条")
    con.close()


def stats(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    for title, q in [
        ("按集合", "SELECT collection, count(*) FROM archives GROUP BY 1"),
        ("按类型", "SELECT kind, count(*) FROM archives GROUP BY 1 ORDER BY 2 DESC"),
        ("按年份", "SELECT year, count(*) FROM archives GROUP BY 1 ORDER BY 1"),
        ("error!=0", "SELECT error, count(*) FROM archives WHERE error<>0 GROUP BY 1"),
        ("有可读名字", "SELECT count(*) FROM archives WHERE subtitle_name<>''"),
        ("有中文片名", "SELECT count(*) FROM archives WHERE cnname IS NOT NULL"),
        ("inner_files", "SELECT count(*) FROM inner_files"),
    ]:
        print(f"--- {title}")
        for row in con.execute(q):
            print("   ", *row)
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build-meta", help="用 TSV 建索引")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--csv", default=str(INDEX_DIR / "corpus_archives.csv"))

    p = sub.add_parser("scan-inner", help="扫描归档内部文件名")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--which", default="all", choices=["all", "data", "data2"])
    p.add_argument("--limit", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--batch", type=int, default=500)
    p.add_argument("--no-skip", action="store_true", help="重扫已完成的归档")

    p = sub.add_parser("search", help="检索")
    p.add_argument("keyword", nargs="?")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--year")
    p.add_argument("--kind")
    p.add_argument("--lang")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("stats", help="统计")
    p.add_argument("--db", default=str(DB_PATH))

    p = sub.add_parser("verify", help="校验归档 sha256")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--which", default="all", choices=["all", "data", "data2"])
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--batch", type=int, default=400)
    p.add_argument("--limit", type=int)
    p.add_argument("--seed", type=int)

    p = sub.add_parser("extract", help="按条件解出文件")
    p.add_argument("keyword", nargs="?")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--year")
    p.add_argument("--kind")
    p.add_argument("--lang")
    p.add_argument("--out", dest="out_dir")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")

    a = ap.parse_args()
    if a.cmd == "build-meta":
        build_meta(Path(a.db), a.csv)
    elif a.cmd == "scan-inner":
        scan_inner(Path(a.db), a.which, a.limit, a.seed, a.workers,
                   skip_existing=not a.no_skip, batch=a.batch)
    elif a.cmd == "verify":
        verify(Path(a.db), a.which, a.workers, a.batch, a.limit, a.seed)
    elif a.cmd == "extract":
        extract(Path(a.db), a.keyword, a.year, a.kind, a.lang, a.out_dir,
                a.limit, a.workers, a.dry_run)
    elif a.cmd == "search":
        search(Path(a.db), a.keyword, a.year, a.kind, a.lang, a.limit)
    elif a.cmd == "stats":
        stats(Path(a.db))


if __name__ == "__main__":
    main()
