#!/usr/bin/env python3
"""
归档语料检索器 —— 从「文件名」定位到归档中的对应文件并取出来。

典型用法：
    # 建一次匹配索引
    python3 scripts/searcher.py build-subs

    # 看片前：直接对着视频文件找
    python3 scripts/searcher.py find ~/Movies/The.Vampire.Diaries.S03E10.720p.mkv

    # 不落地，只看看有哪些候选
    python3 scripts/searcher.py find "权力的游戏 S08E01" --dry-run

    # 整个目录体检 / 批量补齐
    python3 scripts/searcher.py scan ~/Movies --apply

    # 起一个本地网页，随时搜随时下
    python3 scripts/searcher.py serve

匹配三级回退：
    L1 发布组名完全一致  release_key 相同          （同一压制版本，最准）
    L2 剧名 token + 季集  series 匹配 + SxxExx     （不同压制版本也能中）
    L3 片名 token 模糊    title token 重叠打分      （电影、改名文件）
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (  # noqa: E402
    AUTO_OPEN_BROWSER,
    CORPUS_DIR,
    DB_PATH,
    HOST,
    INDEX_DIR,
    PORT,
    PROJECT_DIR,
    corpus_ready,
    missing_corpus_files,
)
from indexer import _list_inner, open_archives  # noqa: E402

try:  # 可选依赖：装了就更准，没装也能跑
    from charset_normalizer import from_bytes as _cn_from_bytes
except ImportError:
    _cn_from_bytes = None

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".rmvb", ".rm", ".wmv", ".mov", ".m4v", ".ts",
    ".m2ts", ".flv", ".mpg", ".mpeg", ".vob", ".iso", ".webm", ".divx", ".ogm",
}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}

# ---------------------------------------------------------------------------
# 文件名归一化
# ---------------------------------------------------------------------------

# 语言后缀（出现在字幕文件名末尾）
_LANG_TOKEN = (
    r"(?:简体|繁体|简中|繁中|简英|繁英|中英|双语|中文|英文|英语|日文|日语|韩语|"
    r"中文字幕|英文字幕|chs|cht|chi|eng|jpn|kor|sc|tc|gb|big5|hans|hant|zhcn|zhtw|"
    r"zh|cn|en)"
)
_LANG_SUFFIX_RE = re.compile(
    r"[.\s_\-\[]*"
    r"(?:"
    + _LANG_TOKEN + r"\s*[&＆+]\s*" + _LANG_TOKEN +
    r"|" + _LANG_TOKEN +
    r")"
    r"[\]\s]*$",
    re.I,
)

# 发布组名里的噪声词（保留年份以外的数字，避免把 Iron.Man.3 削成 Iron.Man）
_NOISE = {
    "720p", "1080p", "2160p", "480p", "4k", "uhd", "hd", "sd", "fullhd",
    "hdrip", "brrip", "bdrip", "bluray", "blu", "ray", "bd", "webrip", "web",
    "webdl", "hdtv", "dvdrip", "dvd", "dvdr", "hdts", "ts", "tc", "cam",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "av1",
    "aac", "ac3", "eac3", "dts", "dtshd", "truehd", "flac", "mp3", "ddp",
    "ddp5", "dd5", "dd", "atmos", "5", "1", "7", "2", "0",
    "remux", "proper", "repack", "internal", "limited", "extended", "unrated",
    "complete", "season", "multi", "subs", "subbed", "dubbed", "hdr", "hdr10",
    "dolby", "vision", "dv", "10bit", "8bit", "amzn", "nf", "hmax", "dsnp",
    "atvp", "itunes", "ita", "vostfr", "ita", "eng", "chi", "chs", "cht",
    "gb", "big5", "mkv", "mp4", "avi", "rmvb",
}

_YEAR_RE = re.compile(r"(?:^|[.\s_\[(])((?:19|20)\d{2})(?:[.\s_\])]|$)")
_EP_RE = re.compile(
    r"(?:^|[.\s_\[(])"
    r"(?:s\s*(\d{1,2})\s*[.\s_-]*e\s*(\d{1,3})|(\d{1,2})\s*x\s*(\d{2,3})|"
    r"第\s*(\d{1,3})\s*季[.\s_-]*第\s*(\d{1,3})\s*[集话話])"
    r"(?:[.\s_\])-]|$)",
    re.I,
)
_CN_EP_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百]{1,4})\s*季")
_CN_EP2_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百]{1,4})\s*[集话話]")
# 只有季、没有集：S08 / Season 8 / S8
_SEASON_ONLY_RE = re.compile(
    r"(?:^|[.\s_\[(])(?:s|season[.\s_-]*)(\d{1,2})(?:[.\s_\])-]|$)",
    re.I,
)

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13,
           "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18,
           "十九": 19, "二十": 20}

# 这些词区分度太低，不能单独作为匹配依据
STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "with",
    "by", "from", "is", "are", "was", "were", "be", "it", "its", "my", "me",
    "you", "your", "i", "we", "us", "he", "she", "they", "them", "his", "her",
    "this", "that", "these", "those", "as", "but", "not", "no", "all", "so",
    "do", "did", "does", "have", "has", "had", "will", "would", "can", "could",
    "part", "pt", "vol", "chapter", "ep", "episode", "season", "series",
    "movie", "film", "full", "new", "old", "great", "big", "little", "last",
    "first", "one", "two", "three", "ii", "iii", "iv",
}
_EP_TOKEN_RE = re.compile(r"^(?:s\d{1,2}e\d{1,3}|\d{1,2}x\d{2,3}|s\d{1,2}|e\d{1,3})$", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def cn2int(s):
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    m = re.fullmatch(r"([一二三四五六七八九])?十([一二三四五六七八九])?", s)
    if m:
        return (int(_CN_NUM.get(m.group(1), 1)) * 10) + int(_CN_NUM.get(m.group(2), 0))
    return None


def fix_cp437(name: str) -> str:
    """zip 里未标 UTF-8 的中文名会被解成 CP437 乱码，还原回 GBK。"""
    if not name or name.isascii():
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        return name


def norm_key(s: str) -> str:
    """归一化成点分小写 key：只保留字母数字和汉字。"""
    s = unicodedata.normalize("NFKC", s).lower()
    out = []
    for ch in s:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
        else:
            out.append(".")
    return re.sub(r"\.+", ".", "".join(out)).strip(".")


def split_lang(stem: str):
    """剥掉末尾的语言标记，返回 (主干, 语言标签)。"""
    langs = []
    cur = stem
    for _ in range(3):
        m = _LANG_SUFFIX_RE.search(cur)
        if not m:
            break
        langs.append(m.group(0).strip(" ._-[]"))
        cur = cur[: m.start()]
    if not cur:
        cur = stem
        langs = []
    tag = classify_lang(langs)
    return cur, tag


def classify_lang(marks):
    txt = " ".join(marks).lower()
    has_chs = bool(re.search(r"简体|简中|简英|chs|\bsc\b|\bgb\b|中文|中英|双语", txt))
    has_cht = bool(re.search(r"繁体|繁中|繁英|cht|\btc\b|big5", txt))
    has_eng = bool(re.search(r"英文|英语|eng", txt))
    if "中英" in txt or "双语" in txt:
        return "chs_eng"
    if has_chs and has_eng:
        return "chs_eng"
    if has_cht and has_eng:
        return "cht_eng"
    if has_chs:
        return "chs"
    if has_cht:
        return "cht"
    if has_eng:
        return "eng"
    if re.search(r"日文|日语|jpn", txt):
        return "jpn"
    return ""


# 「特别篇」的写法。要求关键词是独立 token（前后是分隔符/行尾/紧跟数字），
# 否则 NOVA、SPecial Edition 之类会被误伤。
# 关键词前**必须**有分隔符（不能是文件名开头）：
# "Special.Forces.2011" 是一部电影的名字，不是特别篇。
_SP_TOKEN_RE = re.compile(
    r"(?:[.\s_\[(-])(special|sp|ova|oad|特典|特别篇|特別篇|番外篇|番外|剧场版|劇場版)"
    r"(?=[.\s_\])-]|$|\d)", re.I)

# "Special Edition" / "特别版" 是版本说明，不是特别篇
_SP_NOT_EDITION_RE = re.compile(r"[.\s_-]*(edition|版本?|collector|collectors)", re.I)


def parse_special(text: str):
    """识别特别篇，返回 (0, 编号或 None)；不是特别篇则返回 (None, None)。

    只在正常季集解析失败后才调用 —— 否则 Lost.S01E14.Special 这种
    （Special 是那一集的标题）会被误判。

    季号一律记 0，与数据里本来就有的 S00Exx 写法保持一致（Plex/Jellyfin 约定）。
    """
    m = _SP_TOKEN_RE.search(text)
    if not m:
        return None, None
    if _SP_NOT_EDITION_RE.match(text[m.end():]):
        return None, None
    tail = text[m.end():]
    mm = re.match(r"\s*(\d{1,3})", tail)
    return 0, int(mm.group(1)) if mm else None


def special_key(base: str, m, series_key: str) -> str:
    """一条特别篇的稳定标识 —— 用来把「同一集特别篇的不同语言/容器」归到一组。

    规则：取关键词之后、直到第一个画质噪声词为止的 token。
    开头若紧跟年份（…Special.2011.720p…）则跳过，这样 2011 和 2012 两集圣诞特别篇
    不会混在一起。关键词后面什么都没有时（Skins…Secret.Party.Special），
    退回用关键词之前的 token，并去掉属于剧名的那部分。
    """
    toks = []
    for t in norm_key(base[m.end():]).split("."):
        if not t:
            continue
        if not toks and re.fullmatch(r"(?:19|20)\d{2}", t):
            continue                      # 跳过紧跟在关键词后的年份
        # 碰到画质/噪声词就结束 —— 后面的发布组（-SHORTBREHD 之类）不算标题。
        # 这里必须是 break 而不是 continue：否则「…Special.720p.BluRay.x264-SHORTBREHD」
        # 会把前面的噪声全跳过，最后只取到发布组名 shortbrehd。
        if t in _NOISE or _EP_TOKEN_RE.match(t):
            break
        if t in STOPWORDS:
            continue                      # "From the Beginning" 这种，别在 From 就断
        toks.append(t)
    if not toks:
        # 关键词后面没内容（…Christmas.Special.720p…）—— 用前面紧邻的两个词
        cand = [t for t in norm_key(base[:m.start()]).split(".")
                if t and t not in _NOISE and not _EP_TOKEN_RE.match(t)]
        toks = cand[-2:]
    return ".".join(toks)


def _special_head(base: str, sp_start: int) -> str:
    """特别篇的文件名 -> 剧名部分。

    Black.Mirror.S02.Special.White.Christmas  -> Black.Mirror
    Downton.Abbey.S05.Christmas.Special.720p  -> Downton.Abbey
    downton.abbey.christmas.special.2011      -> downton.abbey.christmas（没季号可切）

    有季号就切在最后一个季号上，否则切在关键词处。
    """
    before = base[:sp_start]
    sm = None
    for sm in _SEASON_ONLY_RE.finditer(before):
        pass
    if sm:
        head = before[: sm.start()]
    else:
        head = before
    return re.sub(r"[.\s_-]*s\d{1,2}$", "", head, flags=re.I).strip(" ._-")


def parse_episode(text: str):
    m = _EP_RE.search(text)
    if m:
        if m.group(1):
            return int(m.group(1)), int(m.group(2))
        if m.group(3):
            return int(m.group(3)), int(m.group(4))
        if m.group(5):
            return cn2int(m.group(5)), cn2int(m.group(6))
    return None, None


# 只在这份白名单里的扩展名才会被剥掉。
# 不能用 Path.stem 无脑剥 —— 对 "Game.of.Thrones.S08E01" 这种
# 「发布名 + 季集号」的写法，Path 会把 ".S08E01" 当成扩展名吃掉，
# 结果季集信息整个丢失，搜索退化成「全季全糊匹配」。
_STRIPPABLE_EXT = (VIDEO_EXT | SUB_EXT
                   | {".txt", ".nfo", ".torrent", ".mka", ".sub", ".idx"})


def _strip_known_ext(name: str) -> str:
    dot = name.rfind(".")
    if dot > 0:
        if name[dot:].lower() in _STRIPPABLE_EXT:
            return name[:dot]
    return name


def parse_video_name(name: str):
    """视频文件名 / 发布名 -> 结构化信息。支持整季（只有季没有集）。"""
    stem = _strip_known_ext(Path(name).name)
    base, lang = split_lang(stem)
    season, episode = parse_episode(stem)
    if season is None:
        m = _CN_EP_RE.search(stem)
        season = cn2int(m.group(1)) if m else None
        m2 = _CN_EP2_RE.search(stem)
        episode = cn2int(m2.group(1)) if m2 else None
    if season is None:
        m = _SEASON_ONLY_RE.search(stem)
        if m:
            season = int(m.group(1))
            episode = None

    # 剧名部分：SxxExx / Sxx 之前；电影则整段
    head = base
    m = _EP_RE.search(base)
    if m:
        head = base[: m.start()]
    else:
        sm = _SEASON_ONLY_RE.search(base)
        if sm:
            head = base[: sm.start()]
    if season is not None:
        head = _CN_EP_RE.sub(" ", head)
        head = _CN_EP2_RE.sub(" ", head)

    release_key = norm_key(base)
    series_key = norm_key(head)
    cjk = [p for p in _CJK_RE.findall(head)
           if len(p) >= 2 and not _is_generic_cjk(p)]

    year = None
    m = _YEAR_RE.search(stem)
    if m:
        year = int(m.group(1))

    # 去掉末尾发布组 -GROUP
    clean = re.sub(r"[-\s](?:19|20)\d{2}.*$", "", head)
    group = None
    gm = re.search(r"-([A-Za-z0-9]{2,20})$", clean)
    if gm:
        group = gm.group(1).lower()

    title_toks = []
    for t in norm_key(head).split("."):
        if not t or _EP_TOKEN_RE.match(t) or t in _NOISE or t in STOPWORDS:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", t):
            continue
        if group and t == group:
            continue
        title_toks.append(t)

    return {
        "stem": stem,
        "release_key": release_key,
        "series_key": series_key,
        "title_key": ".".join(title_toks),
        "title_toks": title_toks,
        "cjk": cjk,
        "season": season,
        "episode": episode,
        "season_only": season is not None and episode is None,
        "year": year,
        "lang_hint": lang,
    }


# ---------------------------------------------------------------------------
# 索引构建
# ---------------------------------------------------------------------------
SUBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subs (
    id          INTEGER PRIMARY KEY,
    fullpath    TEXT NOT NULL,
    inner_path  TEXT NOT NULL,
    ext         TEXT,
    size        INTEGER,
    release_key TEXT,
    series_key  TEXT,
    title_key   TEXT,
    season      INTEGER,
    episode     INTEGER,
    lang        TEXT,
    is_bare     INTEGER DEFAULT 0,
    special_key TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_subs_ep    ON subs(season, episode);
CREATE INDEX IF NOT EXISTS ix_subs_skey  ON subs(series_key);
CREATE INDEX IF NOT EXISTS ix_subs_tkey  ON subs(title_key);
CREATE INDEX IF NOT EXISTS ix_subs_rkey  ON subs(release_key);
"""


def service_running():
    """服务是否在运行。返回 pid 或 None。

    重建索引会 DROP / 重建整张表，而服务端持有长连接；
    并发之下读连接会看到被换掉的页，报 "database disk image is malformed"。
    所以重建前必须先停服务。
    """
    try:
        from config import RUN_DIR
        f = RUN_DIR / "server.pid"
        if not f.exists():
            return None
        info = json.loads(f.read_text(encoding="utf-8"))
        pid = int(info.get("pid") or 0)
        if pid <= 0:
            return None
        os.kill(pid, 0)               # 只探测存活，不发信号
        return pid
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def require_service_stopped():
    """重建类命令的前置检查。服务在跑就拒绝执行。"""
    pid = service_running()
    if pid:
        print(f"❌ 检索服务正在运行（pid={pid}）。")
        print("   重建索引会重建整张表，与服务端的长连接并发时会读出坏页。")
        print("   请先执行 ./stop.sh，重建完再 ./start.sh。")
        return False
    return True


def build_subs(db_path=DB_PATH, verbose=True):
    """把 inner_files 与单文件归档整理成统一的匹配表。"""
    con = sqlite3.connect(db_path)
    con.executescript("DROP TABLE IF EXISTS subs;" + SUBS_SCHEMA)
    cur = con.cursor()

    buf = []
    n = 0

    def emit(fullpath, inner_path, ext, size, is_bare):
        nonlocal n
        searchable = fix_cp437(inner_path)          # 必须先还原乱码，再解析
        name = Path(searchable).name
        stem = os.path.splitext(name)[0]
        base, lang = split_lang(stem)
        season, episode = parse_episode(stem)

        head = base
        sp_key = ""
        m = _EP_RE.search(base)
        if m and episode == 0:
            # S01E00：发行方用「第 0 集」放圣诞特辑 / 先行篇，不能算进正片集数
            # （否则 Game of Thrones S01 会变成 11 集）。挪到第 0 季。
            # 必须先于下面的 `if m` 判断 —— S01E00 同样能被 _EP_RE 匹配到。
            head = base[: m.start()]
            season = 0
            sp_key = special_key(base, m, norm_key(head))
        elif m:
            head = base[: m.start()]
        else:
            # 没有正常季集号：再看是不是特别篇（Skins.S01.Special / 某剧 SP / 特别篇）
            sp_season, sp_ep = parse_special(base)
            if sp_season is not None:
                season, episode = sp_season, sp_ep
                sm = _SP_TOKEN_RE.search(base)
                head = _special_head(base, sm.start())
                sp_key = special_key(base, sm, norm_key(head))

        rel = norm_key(base)
        skey = norm_key(head)
        toks = [t for t in skey.split(".")
                if t and not _EP_TOKEN_RE.match(t) and t not in _NOISE
                and t not in STOPWORDS
                and not re.fullmatch(r"(?:19|20)\d{2}", t)]
        buf.append((fullpath, searchable, ext, size, rel, skey, ".".join(toks),
                    season, episode, lang, is_bare, sp_key))
        n += 1
        if len(buf) >= 20000:
            cur.executemany(
                "INSERT INTO subs (fullpath,inner_path,ext,size,release_key,"
                "series_key,title_key,season,episode,lang,is_bare,special_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            buf.clear()

    for fullpath, inner_path, ext, size in con.execute(
        "SELECT fullpath, inner_path, ext, size FROM inner_files "
        "WHERE ext IN ('srt','ass','ssa','sub')"
    ):
        emit(fullpath, inner_path, ext, size, 0)
    if verbose:
        print(f"  归档内文件 {n}")

    n0 = n
    for fullpath, ext, size in con.execute(
        "SELECT fullpath, kind, size FROM archives WHERE kind IN ('srt','ass','ssa')"
    ):
        emit(fullpath, os.path.basename(fullpath), ext, size, 1)
    if verbose:
        print(f"  单文件归档 {n-n0}")
    if buf:
        cur.executemany(
            "INSERT INTO subs (fullpath,inner_path,ext,size,release_key,"
            "series_key,title_key,season,episode,lang,is_bare,special_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", buf)
    # 特别篇常常只有一个名字（Special / SP / 白色圣诞），没有编号。
    # 按 (剧, 特别篇标识) 去重后排序编号 —— 同一个特别篇的不同语言/容器
    # 必须落在同一个编号里，否则「白色圣诞」的 4 种语言会变成 4 集。
    con.execute("""
        WITH mx AS (
            SELECT series_key, COALESCE(MAX(episode), 0) m FROM subs
            WHERE season = 0 AND episode IS NOT NULL GROUP BY series_key
        ),
        uniq AS (
            SELECT DISTINCT series_key, special_key FROM subs
            WHERE season = 0 AND episode IS NULL AND special_key <> ''
        ),
        ranked AS (
            SELECT series_key, special_key,
                   ROW_NUMBER() OVER (PARTITION BY series_key ORDER BY special_key) rn
            FROM uniq
        ),
        t AS (
            SELECT s.rowid rid,
                   COALESCE(mx.m, 0) + r.rn AS ep
            FROM subs s
            JOIN ranked r ON r.series_key = s.series_key
                         AND r.special_key = s.special_key
            LEFT JOIN mx ON mx.series_key = s.series_key
            WHERE s.season = 0 AND s.episode IS NULL AND s.special_key <> ''
        )
        UPDATE subs SET episode = (SELECT ep FROM t WHERE t.rid = subs.rowid)
        WHERE rowid IN (SELECT rid FROM t)
    """)
    # 兜底：既没编号也没标识的，按剧顺次补，别留在 NULL
    con.execute("""
        WITH mx AS (
            SELECT series_key, COALESCE(MAX(episode), 0) m FROM subs
            WHERE season = 0 AND episode IS NOT NULL GROUP BY series_key
        ),
        t AS (
            SELECT s.rowid rid, COALESCE(mx.m, 0) + ROW_NUMBER() OVER (
                       PARTITION BY s.series_key ORDER BY s.inner_path) rn
            FROM subs s LEFT JOIN mx ON mx.series_key = s.series_key
            WHERE s.season = 0 AND s.episode IS NULL
        )
        UPDATE subs SET episode = (SELECT rn FROM t WHERE t.rid = subs.rowid)
        WHERE rowid IN (SELECT rid FROM t)
    """)
    con.commit()

    # 重建会往 WAL 里写掉整张表的数据量（实测 WAL 能涨到和库一样大，
    # 534 MB）。这里收一下，不然每次重建都留一个巨大的 -wal 文件。
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass

    # 中文名不复制到 subs：archives.fullpath 是主键，查询时 JOIN 即可，
    # 省掉 ~120MB 冗余。这里只补一个大小写不敏感的中文检索辅助索引。
    con.execute("CREATE INDEX IF NOT EXISTS ix_arch_cnname ON archives(cnname)")

    # token 文档频率，用于 IDF 加权（区分 earth 和 wandering）
    con.executescript("DROP TABLE IF EXISTS token_df;")
    con.execute("CREATE TABLE token_df (token TEXT PRIMARY KEY, df INTEGER)")
    df = Counter()
    for (tk,) in con.execute("SELECT title_key FROM subs WHERE title_key <> ''"):
        for t in set(tk.split(".")):
            df[t] += 1
    con.executemany("INSERT INTO token_df VALUES (?,?)", df.items())
    con.commit()

    con.commit()
    con.execute("VACUUM")          # 回收建表过程中产生的空洞，能省 ~15%

    tot = con.execute("SELECT count(*) FROM subs").fetchone()[0]
    withep = con.execute("SELECT count(*) FROM subs WHERE season IS NOT NULL").fetchone()[0]
    if verbose:
        print(f"[build-subs] subs={tot}  带季集={withep} ({withep/tot*100:.1f}%)  "
              f"词表={len(df):,}")
    con.close()
    return tot


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------
LANG_LABEL = {"chs": "简体", "cht": "繁体", "chs_eng": "简英", "cht_eng": "繁英",
              "eng": "英文", "jpn": "日文", "": "未知"}

# 用户偏好排序（默认：双语简英 > 简体 > 繁英 > 繁体 > 英文）
LANG_RANK = {"chs_eng": 0, "chs": 1, "cht_eng": 2, "cht": 3, "eng": 4, "jpn": 6, "": 5}
EXT_RANK = {"ass": 0, "srt": 1, "ssa": 2, "sub": 3}


def _tokens(s):
    return set(t for t in (s or "").split(".") if t)


class IDF:
    """token -> 逆文档频率。区分 earth（烂大街）和 wandering（稀有）。"""

    def __init__(self, con):
        self.con = con
        self.total = con.execute("SELECT count(*) FROM subs").fetchone()[0] or 1
        self.cache = {}

    def df(self, tok):
        if tok not in self.cache:
            r = self.con.execute("SELECT df FROM token_df WHERE token=?", (tok,)).fetchone()
            self.cache[tok] = r[0] if r else 0
        return self.cache[tok]

    def has(self, tok):
        """只认索引里真实存在的词。x264/1080p 这类不在词表里的词不能用来检索。"""
        return self.df(tok) > 0

    def __call__(self, tok):
        import math
        df = self.df(tok)
        if df <= 0:
            return 0.0
        return math.log(1 + self.total / (1 + df))


def _is_generic_cjk(phrase):
    """「第一季」「第3集」这类词区分度为零，不能用来匹配。"""
    return bool(re.fullmatch(r"第?[0-9一二三四五六七八九十百]+[季集话話部]", phrase))


def score_candidate(video, cand, idf=None, ignore_episode=False):
    """返回 (分数, 说明, 等级) 或 None。等级 3=发布名一致 2=剧名+季集 1=片名相似

    ignore_episode=True 时只要求季一致（用于整季检索）。
    """
    v_release = video["release_key"]
    v_series = _tokens(video["series_key"])
    v_title = _tokens(video["title_key"])
    c_release = cand["release_key"] or ""
    c_series = _tokens(cand["series_key"])
    c_title = _tokens(cand["title_key"])

    level = 0
    why = []

    # L3→ 发布组名完全一致（最强）。整季模式下发布名不可能完全一致，跳过。
    if not ignore_episode:
        if v_release and v_release == c_release:
            level = 3
            why.append("发布名完全一致")
        elif v_release and len(v_release) > 12 and c_release.startswith(v_release + "."):
            level = 3
            why.append("发布名前缀一致")

    # 中文片名命中（用户输入中文，或文件名含中文）
    if level < 2 and video.get("cjk"):
        cn = cand.get("cnname") or ""
        for phrase in video["cjk"]:
            if len(phrase) >= 2 and phrase in cn:
                level = max(level, 2)
                why.append(f"中文片名匹配「{phrase}」")
                break

    # L2 剧名（+季集）
    if level < 2:
        s_tok = v_series or v_title
        if video["season"] is None:
            # 只给了剧名（没写季号）：剧名对上就算 L2。
            # 这一条还负责把「中文名搜到的剧」和「英文名发布」的条目接起来。
            if s_tok and c_series and _series_match_score(s_tok, c_series, idf) >= 0.75:
                level = max(level, 2)
                why.append("剧名匹配")
        elif cand["season"] is not None:
            season_ok = cand["season"] == video["season"]
            ep_ok = True if ignore_episode else cand["episode"] == video["episode"]
            if season_ok and ep_ok and s_tok and c_series:
                if _series_match_score(s_tok, c_series, idf) >= 0.75:
                    level = max(level, 2)
                    tail = (f"S{video['season']:02d}" if ignore_episode
                            else f"S{video['season']:02d}E{(video['episode'] or 0):02d}")
                    why.append(f"剧名+{tail}")

    # L1 片名相似（IDF 加权，稀有词为主）
    if level < 1:
        base = v_title or v_series
        if base and (c_title or c_series):
            pool = c_title | c_series
            inter = base & pool
            ratio = _idf_ratio(inter, base, idf)
            # 至少要有一个非停用词命中，防止 earth/the 这类泛词蒙中
            if ratio >= 0.65 and inter:
                level = 1
                why.append(f"片名匹配 {len(inter)} 词 (IDF {ratio:.0%})")

    if level == 0:
        return None

    # 季/集不一致就作废
    if video["season"] is not None and cand["season"] is not None:
        same_season = video["season"] == cand["season"]
        same_ep = (not ignore_episode) and video["episode"] == cand["episode"]
        if ignore_episode:
            ok = same_season
        else:
            ok = same_season and same_ep
        if not ok:
            if level >= 2:
                return None
            level = 0.5
            why.append("季集不一致")

    score = level * 1000
    score -= LANG_RANK.get(cand["lang"], 5) * 8
    score -= EXT_RANK.get(cand["ext"], 4) * 2
    # 电影：年份对上更可信（黑客帝国 1999 vs 动画版 2003）
    if video.get("year") and str(video["year"]) in (cand["release_key"] or ""):
        score += 30
    elif video.get("year"):
        score -= 20
    if cand["size"] and cand["size"] > 0:
        score += min(cand["size"] / 200000, 5)
    return (score, " / ".join(why), level)


_YEAR_TOKEN_RE = re.compile(r"(?:19|20)\d{2}")


def _idf_ratio(inter, base, idf):
    """交集占查询方的比重（IDF 加权）。用于「查询的词被命中多少」。"""
    if not base:
        return 0.0
    if idf is None:
        return len(inter) / len(base)
    num = sum(idf(t) for t in inter)
    den = sum(idf(t) for t in base)
    if den <= 0:
        # 整串都是停用词（"this is us" / "you" / "it"）时 IDF 全为 0，
        # 分子分母同时归零 —— 退回按词数算，否则这类剧永远搜不到。
        return len(inter) / len(base)
    return num / den


def _series_match_score(s_tok, c_tok, idf):
    """对称的剧名匹配度 = 交集 / 并集（先去掉年份 token）。

    用并集而不是查询方，是为了挡住「CSI 匹配到 CSI: Miami」这类：
    候选比查询多出实义词时，说明它其实是另一部剧，不该算精确匹配。
    年份属于噪声（This.Is.Us vs This.Is.Us.2016），比对前先剔除。
    """
    s = {t for t in s_tok if not _YEAR_TOKEN_RE.fullmatch(t)}
    c = {t for t in c_tok if not _YEAR_TOKEN_RE.fullmatch(t)}
    if not s or not c:
        return 0.0
    inter = s & c
    if not inter:
        return 0.0
    union = s | c
    if idf is None:
        return len(inter) / len(union)
    num = sum(idf(t) for t in inter)
    den = sum(idf(t) for t in union)
    if den <= 0:
        return len(inter) / len(union)
    return num / den


class CandidateList(list):
    """带元信息的候选列表：truncated=True 表示某条 SQL 撞到了行数上限，
    可能漏掉了结果（整季检索尤其容易被截断）。"""

    def __init__(self, *a, truncated=False, **kw):
        super().__init__(*a, **kw)
        self.truncated = truncated


def find_candidates(con, video, limit=200, lang_pref=None, ignore_episode=False,
                    sql_cap=None):
    """先用 SQL 收窄，再在 Python 里精排。

    ignore_episode=True -> 只要同一季的都算候选（整季检索用）。

    limit 是「每个子查询取多少行」的上限，不是最终结果条数。
    整季检索一定要给足 sql_cap —— 一季 22 集 × 每集 20 个文件就是 440 行，
    给 400 会正好把最后一集切掉（这个 bug 真实发生过）。
    """
    if sql_cap is None:
        # 整季模式按「集数 × 每集文件数」留足余量
        sql_cap = max(20000 if ignore_episode else 5000,
                      limit * (50 if ignore_episode else 25))

    rows = []
    v = video
    idf = IDF(con)
    seen_row = set()
    truncated = [False]

    def q(sql, args):
        got = 0
        cur = con.execute(sql, args)
        cols = [d[0] for d in cur.description]
        for tup in cur:
            got += 1
            # 不依赖调用方设置 row_factory=sqlite3.Row，元组和 Row 都能处理
            r = dict(zip(cols, tup)) if not isinstance(tup, sqlite3.Row) else dict(tup)
            k = (r["fullpath"], r["inner_path"])
            if k in seen_row:
                continue
            seen_row.add(k)
            rows.append(r)
        cap = args[-1] if args and isinstance(args[-1], int) else None
        if cap is not None and got >= cap:
            truncated[0] = True

    base = ("SELECT s.*, a.cnname, a.enname, a.year AS archive_year "
            "FROM subs s LEFT JOIN archives a ON a.fullpath = s.fullpath ")

    # 1) 发布名精确 / 前缀（整季模式下跳过，发布名必然带集号）
    if v["release_key"] and not ignore_episode:
        q(base + "WHERE s.release_key = ? LIMIT ?", (v["release_key"], sql_cap))
        q(base + "WHERE s.release_key LIKE ? LIMIT ?", (v["release_key"] + ".%", sql_cap))

    if v["season"] is None:
        ep_sql, ep_args = "", ()
    elif ignore_episode:
        ep_sql, ep_args = " AND s.season = ?", (v["season"],)
    else:
        ep_sql, ep_args = " AND s.season = ? AND s.episode = ?", (v["season"], v["episode"])

    # 2) 剧名 key 精确（+季集）
    if v["series_key"]:
        q(base + "WHERE s.series_key = ?" + ep_sql + " LIMIT ?",
          (v["series_key"],) + ep_args + (sql_cap,))
        q(base + "WHERE s.series_key LIKE ?" + ep_sql + " LIMIT ?",
          (v["series_key"] + ".%",) + ep_args + (sql_cap,))
    if v["title_key"]:
        q(base + "WHERE s.title_key = ?" + ep_sql + " LIMIT ?",
          (v["title_key"],) + ep_args + (sql_cap,))

    # 3) 中文片名（用户输入中文，或中文文件名的影视）
    if v.get("cjk"):
        for phrase in v["cjk"][:2]:
            if len(phrase) < 2:
                continue
            q(base + "WHERE a.cnname LIKE ?" + ep_sql + " LIMIT ?",
              (f"%{phrase}%",) + ep_args + (sql_cap,))
            q(base + "WHERE s.inner_path LIKE ?" + ep_sql + " LIMIT ?",
              (f"%{phrase}%",) + ep_args + (sql_cap,))

    # 4) 季集兜底（靠 Python 打分筛剧名）。这一档跨所有剧，上限单独给。
    if len(rows) < 20 and v["season"] is not None:
        if ignore_episode:
            q(base + "WHERE s.season = ? LIMIT ?", (v["season"], 30000))
        else:
            q(base + "WHERE s.season = ? AND s.episode = ? LIMIT ?",
              (v["season"], v["episode"], 8000))

    # 5) 片名 token 模糊（只用语料库里真实存在的词，稀有词优先）
    if len(rows) < 20:
        pool = [t for t in (v["title_toks"] or []) if idf.has(t)]
        if not pool:
            pool = [t for t in _tokens(v["series_key"]) if idf.has(t)]
        pool.sort(key=lambda t: -idf(t))
        for t in pool[:3]:
            if len(t) < 3:
                continue
            q(base + "WHERE s.title_key LIKE ?" + ep_sql + " LIMIT ?",
              (f"%{t}%",) + ep_args + (sql_cap,))
            if len(rows) >= 20:
                break

    seen = {}
    for r in rows:
        r["cnname"] = fix_cp437(r["cnname"]) if r.get("cnname") else r.get("cnname")
        r["enname"] = fix_cp437(r["enname"]) if r.get("enname") else r.get("enname")
        sc = score_candidate(v, r, idf, ignore_episode=ignore_episode)
        if not sc:
            continue
        r["_score"], r["_why"], r["_level"] = sc
        # 同一份字幕可能存在于多个归档，按内容指纹去重
        fp = (r["inner_path"].rsplit("/", 1)[-1].lower(), r["size"])
        prev = seen.get(fp)
        if prev is None or r["_score"] > prev["_score"]:
            seen[fp] = r
    out = CandidateList(seen.values(), truncated=truncated[0])
    out.sort(key=lambda r: (-r["_score"], r["inner_path"]))
    if lang_pref:
        want = set(lang_pref)
        filtered = [r for r in out if r["lang"] in want]
        if filtered:
            out = CandidateList(filtered, truncated=truncated[0])
    return out


def _group_label(pairs, fallback):
    """给一个「按季分组」取可读剧名（同样要剥掉季集尾巴）。"""
    flat = [c for _ep, cs in pairs for c in cs]
    return series_label([c.get("cnname") for c in flat],
                        [c.get("enname") for c in flat], fallback)


def season_title(season):
    """季号 -> 显示名。0 是特别篇（跟数据里 S00Exx 的写法一致）。"""
    return "特别篇" if season == 0 else f"第 {season} 季"


def fmt_size(n):
    """字节数 -> 人可读。字幕都很小，所以最小单位用 KB，不用 B。"""
    if not n or n < 0:              # -1 是「归档格式拿不到大小」的占位，0 按没有处理
        return "大小未知"
    if n < 1024 * 1024:
        kb = n / 1024
        return f"{kb:.1f} KB" if kb < 100 else f"{kb:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _common_prefix(strs):
    """最长公共前缀（大小写不敏感）。

    大小写不敏感是必要的：同一部剧的版本名可能有的首字母大写有的小写
    （Black.Mirror… / black.mirror…），按字节比会一开始就分叉，前缀退化成 1 个字符。
    """
    ss = sorted({x for x in strs if x}, key=lambda z: z.lower())
    if not ss:
        return ""
    a, b = ss[0].lower(), ss[-1].lower()
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return ss[0][:i].strip(" _-—·:：,，、.。[]【】")


# 片名里常见的「尾巴」：第N季 / 第N集 / 各种括号备注
_TITLE_NOISE = re.compile(
    r"第\s*[0-9一二三四五六七八九十百]+\s*[季集话話部]"
    r"|【[^】]*】|\[[^\]]*\]|\([^)]*\)|（[^）]*）")


def _norm_title(x):
    x = _TITLE_NOISE.sub(" ", x or "")
    return re.sub(r"\s+", " ", x).strip(" -_·:：,，、.。~～")


def series_label(cnnames, ennames, fallback):
    """从一堆片名里猜出「这部剧」的名字。

    归档级的中文名往往带着季集信息（「黑镜 第1季」「疑犯追踪 第二季第一集…」），
    直接拿第一条当标题会显示成《黑镜 第1季》，而用户要的是《黑镜》。

    做法：先清掉「第N季/第N集/括号备注」，然后
      1) 取出现次数最多的那个 —— 多数剧的绝大多数归档都叫同一个名字；
      2) 如果名字很分散（没有明显众数），退而取所有名字的最长公共前缀。
    同一条规则对中英文名各试一次。
    """
    for pool in (cnnames, ennames):
        normed = [_norm_title(x) for x in pool if x]
        if not normed:
            continue
        name, cnt = Counter(normed).most_common(1)[0]
        if len(name) >= 2 and cnt >= max(2, len(normed) * 0.3):
            return name
        pre = _common_prefix(normed)
        if len(pre) >= 2:
            return pre
    return (fallback or "").replace(".", " ")


_EP_IN_KEY = re.compile(r"(?:^|\.)(?:s\d{1,2}e\d{1,3}|\d{1,2}x\d{2,3}|s\d{1,2}|e\d{1,3})(?=\.|$)", re.I)


def release_family(release_key):
    """发布名 -> 这一版的标识：**从第一个画质词开始到结尾**。

    person.of.interest.s02e01.720p.web.dl.h.264.kings  -> 720p.web.dl.h.264.kings
    sherlock.the.blind.banker.hdtv.xvid.fov            -> hdtv.xvid.fov
    sherlock.the.great.game.hdtv.xvid.fov              -> hdtv.xvid.fov   ← 同一版

    为什么要从画质词开始：老剧常常**用分集标题**命名而不是 SxxExx
    （sherlock.the.blind.banker...）。如果只去掉季集号那一段，
    每集的标题不同，就会被当成不同的发布版本，一季里一集都凑不齐。
    画质词之后才是「画质 + 来源 + 发布组」这一版真正的标识。

    没有画质词时退回旧办法（去掉季集号那段）。
    """
    rk = release_key or ""
    m = _QUALITY_RE.search(rk)
    if m:
        return rk[m.start(1):].strip(".")
    return re.sub(r"\.+", ".", _EP_IN_KEY.sub("", rk)).strip(".")


def release_label(inner_path):
    """从原始文件名还原出可读的版本名（保留大小写）。

    跟 release_family 对齐：从第一个画质词开始截。
    这样「按分集标题命名」的发布不会把标题带进版本名里
    （Sherlock.A.Scandal.in.Belgravia.720p.HDTV.x264-FoV -> 720p.HDTV.x264-FoV）。
    """
    stem = os.path.splitext(os.path.basename(inner_path))[0]
    base, _lang = split_lang(stem)
    qm = _QUALITY_RE.search(base)
    if qm:
        return base[qm.start(1):].strip(" ._-")
    m = _EP_RE.search(base)
    if m:
        left = base[:m.start()].rstrip(" ._-")
        right = base[m.end():].lstrip(" ._-")
        return f"{left} {right}".strip() if right else left
    return base


# 画质 / 来源 / 编码标记 —— 公共前缀切到这里就该停，这些信息对用户有用
# 画质 / 来源 / 编码标记。用「前后都是分隔符」而不是 \b ——
# 有些发布名用下划线：belgravia.720p_hdtv_x264-fov，
# 720p 后面紧跟的是 _（也是词字符），\b 在这里匹配不上。
_QUALITY_RE = re.compile(
    r"(?:^|[.\s_\-\[()])("
    r"480p|576p|720p|1080p|1080i|2160p|4k|8k|uhd|hdtv|"
    r"web-?dl|web-?rip|web|bluray|blu-?ray|bdrip|brrip|dvdrip|hdrip|dvd|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|xvid|divx|avc|aac|ac3|eac3|dts|ddp|flac|"
    r"hd|sd"
    r")(?=[.\s_\-)\]]|$)", re.I)


def season_versions(con, video, season=None, lang_pref=None):
    """把一季的候选按「发布版本」分组，供整季下载使用。

    返回 [{"family","label","langs":{lang:{"eps":{ep:cand},"n":int}},
            "coverage","total","n_files"}, ...]，覆盖集数多的在前。

    「版本」= 画质 + 发布组的组合。同一版本下再按语言分开，
    这样每个 (版本, 语言) 都能打出一个「每集一个文件」的整季包。
    """
    groups = season_groups(con, video, season, lang_pref)
    if not groups:
        return []
    pairs = groups[0]["pairs"]
    total = len(pairs)
    fam = {}
    for ep, cands in pairs:
        for c in cands:
            f = release_family(c["release_key"]) or (c["series_key"] or "?")
            e = fam.setdefault(f, {"label": release_label(c["inner_path"]), "langs": {}})
            le = e["langs"].setdefault(c["lang"], {"eps": {}})
            le["eps"].setdefault(ep, c)          # cands 已按得分降序，先到先得
    out = []
    for f, e in fam.items():
        langs = {lg: {"eps": d["eps"], "n": len(d["eps"])} for lg, d in e["langs"].items()}
        out.append({"family": f, "label": e["label"], "langs": langs,
                    "coverage": max((v["n"] for v in langs.values()), default=0),
                    "total": total,
                    "n_files": sum(v["n"] for v in langs.values())})
    out.sort(key=lambda x: (-x["coverage"], -x["n_files"], x["family"]))
    # 只保留能覆盖到 2 集以上的版本；零散单集不算「整季」
    out = [x for x in out if x["coverage"] >= min(2, total)]
    # 各版本名共同的开头就是剧名，剥掉它，只留「720p.WEB-DL…」这种区分信息。
    # 注意别把画质标记也剥掉 —— 公共前缀可能一直延伸到「…720p.」，
    # 所以遇到第一个画质词就停。
    pre = _common_prefix([x["label"] for x in out])
    m = _QUALITY_RE.search(pre)
    if m:
        pre = pre[:m.start(1)]
    if len(pre) >= 3:
        for x in out:
            x["short"] = x["label"][len(pre):].strip(" ._-") or x["label"]
    else:
        for x in out:
            x["short"] = x["label"]
    return out


def _specials_pairs(con, video, lang_pref=None):
    """一部剧的特别篇，按「特别篇标识」分组 -> [(序号, [候选...]), ...]。

    用 season = 0 表示特别篇 —— 数据里本来就用 S00Exx 这么写
    （Deadwood.S00E01 / Euphoria.US.S00E01），跟 Plex、Jellyfin 的约定一致。

    特别篇的 series_key 未必和正片完全相同：Downton.Abbey.Christmas.Special
    会被切成 downton.abbey.christmas，所以这里用前缀匹配再兜一层。
    """
    sk = (video.get("series_key") or "").strip()
    if not sk:
        return []
    rows = con.execute(
        "SELECT s.*, a.cnname, a.enname FROM subs s"
        " LEFT JOIN archives a ON a.fullpath = s.fullpath"
        " WHERE s.season = 0 AND (s.series_key = ? OR s.series_key LIKE ?)"
        " ORDER BY s.special_key, s.inner_path",
        (sk, sk + ".%")).fetchall()
    if not rows:
        return []
    buckets = {}
    for r in rows:
        d = dict(r)
        buckets.setdefault(d.get("special_key") or d["inner_path"], []).append(d)
    out = []
    for i, key in enumerate(sorted(buckets), 1):
        cands = buckets[key]
        for c in cands:
            c["_level"] = 2                 # 同一个 series_key 前缀，直接算剧名匹配
            c["_score"] = 1.0
            c["_why"] = "特别篇"
        if lang_pref:
            cands.sort(key=lambda c: (c["lang"] not in lang_pref, c["inner_path"]))
        else:
            cands.sort(key=lambda c: c["inner_path"])
        out.append((i, cands))
    return out


_MERGE_SEASON_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*[季集话話部]")


def _merge_name(x):
    """合并分组时用的「剧名指纹」。

    只去掉季集标记和空白标点，**保留方括号里的内容** ——
    因为剧名常常就写在括号里：`【行尸走肉】精校字幕合集`。
    用 _norm_title（会把括号整段删掉）当指纹的话，一堆不同的剧
    都会变成 `精校字幕合集`，然后被误并成同一部。
    """
    x = _MERGE_SEASON_RE.sub("", x or "")
    return re.sub(r"[\s\-_·:：,，、.。~～!！?？]+", "", x)


def _merge_series_groups(by_series, cn_by_series):
    """把「同一部剧的不同 series_key 写法」并成一组。

    搜「实习医生格蕾」会同时命中三个 series_key：
        greys.anatomy / grey.s.anatomy / 实习医生格蕾
    它们其实是同一部剧。不合并的话主分组可能落在只有 3 季的那个上，
    用户就只看到 3 季（这正是之前的 bug）。

    两种判据（任一命中就合并）：
      · series_key 去掉标点后相同 —— greys.anatomy == grey.s.anatomy
      · 归档中文名的众数相同 —— 三个写法背后都是「实习医生格蕾」
    """
    items = list(by_series)
    tags = {}
    for sk in items:
        t = {"k:" + re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", sk.lower())}
        # 中文名只用来「补充」同一 pattern 的判断：只有当两个 series_key
        # 既去标点相同、又共用同一个中文名时才并。单靠中文名并过 ——
        # 「【行尸走肉】精校字幕合集」和「【天堂执法者】精校字幕合集」
        # 去掉方括号后都是「精校字幕合集」，不同的剧会被并成一部。
        c = cn_by_series.get(sk)
        if c:
            name, _n = c.most_common(1)[0]
            fp = _merge_name(name)
            if len(fp) >= 3:
                t.add("n:" + fp)
        tags[sk] = t

    parent = {sk: sk for sk in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # 只用 k 标签（去标点后的 series_key）合并：这是安全判据。
    # 中文名标签太容易把不同的剧并到一起，只留着做诊断。
    seen = {}
    for sk in items:
        for tag in tags[sk]:
            if not tag.startswith("k:"):
                continue
            if tag in seen:
                ra, rb = find(seen[tag]), find(sk)
                if ra != rb:
                    parent[rb] = ra
            else:
                seen[tag] = sk
    merged = {}
    for sk in items:
        merged.setdefault(find(sk), []).append(sk)
    return list(merged.values())


def recommend_season(con, video, season, lang_pref=None):
    """挑一个「完整」的整季包推荐给用户。

    完整 = 这一季的**每一集**都有字幕。

    1) 有单一版本就完整的 —— 推荐那个版本（用户的整季通常是同一版本）。
    2) 每个版本都不全 —— 退而推荐「每集各取一条」拼出来的补全包，
       并在界面上写清各集版本可能不同。

    返回 {"kind": "version"|"composed"|"none", "family", "label",
          "langs": {lang: {"n","bytes","missing"}}, "total", "missing"}
    """
    groups = season_groups(con, video, season, lang_pref)
    if not groups:
        return {"kind": "none", "total": 0, "langs": {}, "missing": []}
    pairs = groups[0]["pairs"]
    total = len(pairs)
    all_eps = {ep for ep, _cs in pairs}
    if total == 0:
        return {"kind": "none", "total": 0, "langs": {}, "missing": []}

    def stat(eps_map):
        return {"n": len(eps_map),
                "bytes": sum((c["size"] or 0) for c in eps_map.values() if (c["size"] or 0) > 0),
                "missing": sorted(all_eps - set(eps_map))}

    # 1) 单一版本就完整
    best = None
    for v in season_versions(con, video, season, lang_pref):
        full = {lg: d for lg, d in v["langs"].items() if len(d["eps"]) == total}
        if not full:
            continue
        # 完整的语言越多，说明这一版越齐全；同分再看文件数，最后按名字保证稳定。
        # 刻意不参与排序的因素：画质。字幕是文本，跟画面分辨率无关，
        # 720p 的字幕一样能配 1080p 的片源，甚至可能质量更好
        # （有些 720p 发布是精校过的）。所以「完整」优先于「画质」。
        key = (-len(full), -v["n_files"], v["family"])
        if best is None or key < best[0]:
            best = (key, v, full)
    if best:
        _k, v, full = best
        return {"kind": "version", "family": v["family"], "label": v["short"],
                "langs": {lg: stat(d["eps"]) for lg, d in full.items()},
                "total": total, "missing": []}

    # 2) 都不完整 -> 按语言把各版本拼起来
    by_lang = {}
    for ep, cands in pairs:
        for c in cands:
            d = by_lang.setdefault(c["lang"], {})
            d.setdefault(ep, c)          # cands 已按得分降序
    langs = {lg: stat(d) for lg, d in by_lang.items()}
    covered = set()
    for d in by_lang.values():
        covered |= set(d)
    return {"kind": "composed", "family": None, "label": "补全包",
            "langs": langs, "total": total, "missing": sorted(all_eps - covered)}


def _count_versions(pairs):
    """一季里「能覆盖多集」的发布版本数 —— 跟 season_versions 同一套判据。"""
    total = len(pairs)
    fam = {}
    for ep, cands in pairs:
        for c in cands:
            f = release_family(c["release_key"]) or (c["series_key"] or "?")
            fam.setdefault(f, set()).add(ep)
    floor = min(2, total)
    return sum(1 for eps in fam.values() if len(eps) >= floor)


def series_overview(con, video, lang_pref=None):
    """按剧名检索 -> 按季汇总，主分组在前。"""
    v = dict(video)
    v["season"] = None
    v["episode"] = None
    cands = _cached(_cache_key("ov", v, lang_pref), lambda: _augment_by_series(
        con, v,
        find_candidates(con, v, lang_pref=lang_pref, ignore_episode=True, sql_cap=60000),
        lang_pref, ignore_episode=True))

    by_series, cn_by_series = {}, {}
    for c in cands:
        if c["season"] is None or c["season"] == 0:
            continue          # 特别篇单独追加，不参与按季分组
        sk = c["series_key"] or ""
        by_series.setdefault(sk, {}).setdefault(c["season"], {}) \
                 .setdefault(c["episode"], []).append(c)
        if c.get("cnname"):
            cn_by_series.setdefault(sk, Counter())[c["cnname"]] += 1

    groups = []
    for sks in _merge_series_groups(by_series, cn_by_series):
        # 代表 key 取条目最多的那个：链出去的查询才覆盖得全
        rep = max(sks, key=lambda k: sum(len(cs) for se in by_series[k].values()
                                         for cs in se.values()))
        seasons_out = []
        for se in sorted({x for k in sks for x in by_series[k]}):
            eps = {}
            for k in sks:
                for ep, cs in by_series[k].get(se, {}).items():
                    eps.setdefault(ep, []).extend(cs)
            pairs = [(ep, sorted(cs, key=lambda z: (-z["_score"], z["inner_path"])))
                     for ep, cs in sorted(eps.items())]
            seasons_out.append((se, pairs, _count_versions(pairs)))
        flat = [c for _se, pairs, _pk in seasons_out for _ep, cs in pairs for c in cs]
        groups.append({"series_key": rep, "sks": set(sks),
                       "label": series_label([c.get("cnname") for c in flat],
                                             [c.get("enname") for c in flat], rep),
                       "seasons": seasons_out, "primary": False})

    if not groups:
        return []
    size = lambda g: sum(len(p) for _s, p, _k in g["seasons"])   # noqa: E731
    groups.sort(key=lambda g: -size(g))
    # 以前这里无条件优先「查询词和 series_key 完全相同」的那组。中文名搜索会踩坑：
    # 搜「实习医生格蕾」时，字面就叫「实习医生格蕾」的那组只有 3 季，
    # 而真正的正片 greys.anatomy 有 19 季 —— 结果用户只看到 3 季。
    # 现在只在精确命中的那组规模够大时才优先它，否则以内容最多的为准。
    exact = [g for g in groups if (v.get("series_key") or "") in g["sks"]]
    primary = groups[0]
    if exact and size(exact[0]) >= 0.5 * size(groups[0]):
        primary = exact[0]
    primary["primary"] = True

    # 特别篇（season=0）。它们的 series_key 未必和正片一致
    # （Downton.Abbey.Christmas.Special -> downton.abbey.christmas），
    # 所以用主分组的 series_key 去前缀匹配，找到后并进季列表，排在最后。
    sv = dict(v)
    sv["series_key"] = primary["series_key"]
    sp = _specials_pairs(con, sv, lang_pref)
    if sp:
        primary["seasons"] = primary["seasons"] + [
            (0, sp, _count_versions(sp))]

    return [primary] + [g for g in groups if g is not primary]


def season_groups(con, video, season=None, lang_pref=None):
    if season == 0:                       # 特别篇
        pairs = _specials_pairs(con, video, lang_pref)
        if not pairs:
            return []
        sk = video.get("series_key") or ""
        label = _group_label(pairs, sk)
        if label == sk.replace(".", " "):
            # 特别篇归档常常没有中文名（尤其中间那批没元数据的），
            # 这时借用正片的名字，免得标题显示成 "black mirror"。
            rows = con.execute(
                "SELECT a.cnname, a.enname FROM subs s"
                " LEFT JOIN archives a ON a.fullpath = s.fullpath"
                " WHERE s.series_key = ? AND s.season > 0"
                " AND (a.cnname IS NOT NULL OR a.enname IS NOT NULL) LIMIT 400",
                (sk,)).fetchall()
            if rows:
                label = series_label([r[0] for r in rows], [r[1] for r in rows], sk)
        return [{"series_key": sk, "label": label, "pairs": pairs,
                 "primary": True, "specials": True}]
    """整季检索，返回按剧集分组的列表（主组在第一个）。

    [{"series_key", "label", "pairs": [(ep, [候选...]), ...], "primary": bool}, ...]

    为什么要分组：像 "spartacus" 这种查询，既可能指《Spartacus》，
    也可能指《Spartacus: Blood and Sand》；"project.runway" 同理，
    还可能是衍生剧《Project Runway All Stars》。这两种情况在文件名字面上
    完全无法区分，所以不猜 —— 把主组列出来，其余分组一并展示让用户挑。
    """
    v = dict(video)
    v["season"] = season if season is not None else video.get("season")
    v["episode"] = None
    if v["season"] is None:
        return []
    cands = _cached(_cache_key("sg", v, lang_pref), lambda: _augment_by_series(
        con, v,
        find_candidates(con, v, lang_pref=lang_pref, ignore_episode=True, sql_cap=60000),
        lang_pref, ignore_episode=True))
    if getattr(cands, "truncated", False):
        print(f"⚠️  第 {v['season']} 季的候选行数触到了数据库上限，可能有遗漏。",
              file=sys.stderr)

    # 按 series_key 分桶 —— 这一条本身就保证了「不同剧不会混在同一张列表里」，
    # 所以不要再按匹配等级过滤：那会把《Spartacus: Blood and Sand》这类
    # 需要靠 L1 才够得着的分组直接丢掉，用户就再也看不到它了。
    buckets = {}
    for c in cands:
        if c["episode"] is None:
            continue
        buckets.setdefault(c["series_key"] or "", {}).setdefault(
            c["episode"], []).append(c)

    groups = []
    for sk, by_ep in buckets.items():
        pairs = sorted(by_ep.items())
        groups.append({"series_key": sk, "label": _group_label(pairs, sk),
                       "pairs": pairs, "primary": False})
    if not groups:
        return []
    groups.sort(key=lambda g: -len(g["pairs"]))

    # 主组规则：查询词正好等于某个 series_key 就用它，否则用集数最多的那个。
    # 「用户打的字就是这部剧的名字」是最不意外的一条规则。
    exact = [g for g in groups if g["series_key"] == (v.get("series_key") or "")]
    primary = exact[0] if exact else groups[0]
    order = [primary] + [g for g in groups if g is not primary]
    order[0]["primary"] = True
    return order


# 索引在服务运行期间是静态的，所以这几个查询可以安全缓存。
# 剧名总览要做跨语言补全（多跑几轮 SQL），首次约 1 秒，缓存后基本瞬时。
_CACHE = {}
_CACHE_MAX = 128


def _cache_key(kind, video, extra=None):
    return (kind,
            video.get("series_key") or "",
            video.get("title_key") or "",
            tuple(video.get("cjk") or []),
            video.get("season"),
            video.get("episode"),
            tuple(sorted(extra)) if extra else None)


def _cached(key, fn):
    if key in _CACHE:
        return _CACHE[key]
    out = fn()
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def _augment_by_series(con, video, cands, lang_pref=None, ignore_episode=False):
    """用第一轮命中的 series_key 再补一轮，补上「文件名是另一种语言」的条目。

    为什么需要：SQL 的检索阶梯是分语言的 ——
      · 搜中文「黑镜」走的是 `cnname LIKE '%黑镜%'`，只能捞到**打了中文名的归档**；
        而另一些归档文件名是英文（Black.Mirror.S03E04...）且没进元数据，就漏了。
      · 反过来，搜英文 `black.mirror` 走 `series_key = ...`，又会漏掉中文命名的。
    拿第一轮结果里出现过的 series_key 再查一次，两边就能合起来。
    """
    if not cands:
        return cands
    seen = {(c["fullpath"], c["inner_path"]) for c in cands}
    keys = Counter(c["series_key"] for c in cands if c.get("series_key"))
    out = list(cands)
    for sk, _n in keys.most_common(3):
        if not sk or sk == (video.get("series_key") or ""):
            continue
        v2 = dict(video)
        v2["series_key"] = sk
        v2["title_key"] = sk
        v2["title_toks"] = [t for t in sk.split(".") if t]
        for c in find_candidates(con, v2, lang_pref=lang_pref,
                                 ignore_episode=ignore_episode, sql_cap=60000):
            k = (c["fullpath"], c["inner_path"])
            if k not in seen:
                seen.add(k)
                out.append(c)
    out.sort(key=lambda r: (-r["_score"], r["inner_path"]))
    return CandidateList(out, truncated=False)


def find_season(con, video, season=None, lang_pref=None):
    """整季检索：返回主分组的 [(episode, [候选...]), ...]，按集号排序。"""
    groups = season_groups(con, video, season, lang_pref)
    return groups[0]["pairs"] if groups else []


# ---------------------------------------------------------------------------
# 落地：写出播放器能自动识别的字幕
# ---------------------------------------------------------------------------
PLAYER_LANG = {"chs": "zh", "cht": "zh-Hant", "chs_eng": "zh", "cht_eng": "zh-Hant",
               "eng": "en", "jpn": "ja", "": "zh"}


def subtitle_target_path(video_path: Path, cand, mode="sidecar"):
    """生成字幕目标路径。默认与视频同目录同名前缀，播放器自动加载。"""
    ext = "." + (cand["ext"] or "srt")
    lang = PLAYER_LANG.get(cand["lang"], "zh")
    if mode == "sidecar":
        return video_path.with_name(video_path.stem + f".{lang}" + ext)
    return video_path.parent / (video_path.stem + f".{lang}" + ext)


def materialize(con, cand, dest: Path):
    """把某个候选字幕从归档里解出来写到 dest。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fullpath = cand["fullpath"]
    inner = cand["inner_path"]
    coll = fullpath.split("/")[0]
    zmap = dict(open_archives(coll))
    z = zmap.get(coll)
    if z is None:
        raise RuntimeError(f"打不开归档容器 {coll}")
    blob = z.read(fullpath)
    if cand["is_bare"] or inner == os.path.basename(fullpath):
        data = blob
    else:
        ext = os.path.splitext(fullpath)[1].lower()
        target = None
        rows = _list_inner(fullpath, blob)
        if rows:
            # 找到匹配的内层成员
            for nm, _sz in rows:
                if fix_cp437(nm) == inner or nm == inner:
                    target = nm
                    break
        if target is None:
            target = inner
        if ext == ".zip":
            import zipfile
            iz = zipfile.ZipFile(io.BytesIO(blob))
            data = iz.read(target)
        else:
            tmpd = tempfile.mkdtemp()
            try:
                tmp = os.path.join(tmpd, "a" + ext)
                with open(tmp, "wb") as f:
                    f.write(blob)
                # 先按名字单独取。固实 rar 不支持按名字取
                # （bsdtar 报 "RAR solid archive support unavailable"），
                # 这时退化为整包解开再从目录里找。
                r = subprocess.run(["bsdtar", "-xf", tmp, "-C", tmpd, target],
                                   capture_output=True, timeout=300)
                got = os.path.join(tmpd, target)
                if r.returncode != 0 or not os.path.exists(got):
                    subprocess.run(["bsdtar", "-xf", tmp, "-C", tmpd],
                                   capture_output=True, timeout=900)
                if not os.path.exists(got):
                    want = os.path.basename(target)
                    hit = None
                    for root, _dirs, files in os.walk(tmpd):
                        for f in files:
                            if f == want or fix_cp437(f) == want:
                                hit = os.path.join(root, f)
                                break
                        if hit:
                            break
                    if hit is None:
                        raise RuntimeError(f"归档里取不到 {target}")
                    got = hit
                with open(got, "rb") as f:
                    data = f.read()
            finally:
                import shutil
                shutil.rmtree(tmpd, ignore_errors=True)
    with open(dest, "wb") as f:
        f.write(data)
    return len(data), data


def _score_text(t):
    """给一个解码结果打分：汉字多=好，半角片假名/生僻区/控制符=坏。

    关键场景：GBK 的「为什么」被当成 cp932 解出来会是半角片假名「ﾗﾖﾄｻ」，
    这类结果必须被压下去，否则字幕会变成乱码。
    """
    if not t:
        return -99.0
    n = len(t)
    cjk = kana = halfwidth = astral = ctrl = bad = 0
    for ch in t:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
        elif 0x3040 <= o <= 0x30FF:
            kana += 1
        elif 0xFF61 <= o <= 0xFF9F:
            halfwidth += 1
        elif o > 0xFFFF:
            astral += 1
        elif o < 32 and ch not in "\r\n\t":
            ctrl += 1
        elif ch == "\ufffd":
            bad += 1
    plain = n - cjk - kana - halfwidth - astral - ctrl - bad
    return (cjk * 2.0 + plain * 0.2 - halfwidth * 4.0 - astral * 2.0
            - ctrl * 3.0 - bad * 8.0) / n


# UTF-8 误判的典型症状：解出来全是希腊/西里尔/拉丁补充/IPA 字母。
# 例："为什么" 的 GBK 字节 CE AA CA B2 C3 B4 恰好是合法的 UTF-8，
#     会被解成 "Ϊʲô" —— 单看「UTF-8 能解开」是发现不了的。
_SUSPECT_RANGES = ((0x00A0, 0x00FF), (0x0250, 0x02FF),
                   (0x0370, 0x03FF), (0x0400, 0x04FF))


def _looks_like_mojibake(t: str) -> bool:
    """UTF-8 解出来既没有汉字也没有其它非拉丁文字，只有希腊/西里尔字母 —— 可疑。"""
    if not t:
        return False
    cjk = other = susp = ascii_ = 0
    for ch in t:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
        elif o < 128:
            ascii_ += 1
        elif any(a <= o <= b for a, b in _SUSPECT_RANGES):
            susp += 1
        else:
            other += 1
    if cjk or other:
        return False
    return susp > 0 and susp >= (susp + ascii_) * 0.05


def decode_subtitle(data: bytes):
    """把字幕字节解成文本，返回 (text, encoding)。

    有 BOM 就直接信；否则：
      · 严格 UTF-8 能解开，且结果不像乱码  -> 就用 UTF-8
      · 否则在候选编码里挑打分最高的
    charset-normalizer 装了就用（多一个候选），没装也能正常工作。
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"

    cands = {}
    try:
        t8 = data.decode("utf-8")
        if not _looks_like_mojibake(t8):
            return t8, "utf-8"
        cands["utf-8"] = t8            # 可疑，交给打分一起比
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5hkscs", "cp932"):
        try:
            cands[enc] = data.decode(enc)
        except UnicodeDecodeError:
            pass
    if _cn_from_bytes is not None:
        try:
            best = _cn_from_bytes(data).best()
            if best is not None and best.encoding:
                cands.setdefault(str(best.encoding), str(best))
        except Exception:
            pass
    if not cands:
        return data.decode("latin-1", "replace"), "latin-1"
    enc, txt = max(cands.items(), key=lambda kv: _score_text(kv[1]))
    return txt, enc


def normalize_text_bytes(data: bytes):
    """把字幕转成 UTF-8；返回 (bytes, encoding)。确实是 UTF-8 就原样返回。"""
    if data[:2] not in (b"\xff\xfe", b"\xfe\xff") and not data.startswith(b"\xef\xbb\xbf"):
        try:
            t8 = data.decode("utf-8")
            if not _looks_like_mojibake(t8):
                return data, "utf-8"
        except UnicodeDecodeError:
            pass
    text, enc = decode_subtitle(data)
    return text.encode("utf-8"), enc


# ---------------------------------------------------------------------------
# 整季
# ---------------------------------------------------------------------------
def parse_episode_filter(spec, available):
    """'1-5,8,10' -> {1,2,3,4,5,8,10}；spec 为空则全要。"""
    if not spec:
        return set(available)
    want = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            want.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            want.add(int(part))
    return want & set(available) if want else set(available)


def season_pick(con, video, season, lang_pref=None, episodes=None):
    """返回 [(episode, best_candidate), ...]，每集只取最佳一条。"""
    pairs = find_season(con, video, season, lang_pref)
    want = parse_episode_filter(episodes, [ep for ep, _ in pairs])
    return [(ep, cands[0]) for ep, cands in pairs if ep in want]


def build_season_zip(con, video, season, lang_pref=None, episodes=None,
                     variants=False, family=None):
    """把整季打进一个 zip，返回 (bytes, 清单)。

    family=None     每集放推荐的那一条（S01E01.zh.ass，播放器可直接识别）
    family="..."    只取该发布版本的条目 —— 每集一个文件，下完直接能用
    variants=True   每集放全部版本；同名时加 _2 / _3 后缀避免互相覆盖
    """
    import zipfile

    if family:
        vers = season_versions(con, video, season, lang_pref)
        v = next((x for x in vers if x["family"] == family), None)
        if v is None:
            return None, []
        # 同一版本下按语言再分：优先用请求的语言，否则用该版本里最全的
        want_langs = set(lang_pref) if lang_pref else set()
        cands_lang = [(lg, d["n"]) for lg, d in v["langs"].items()
                      if not want_langs or lg in want_langs]
        if not cands_lang:
            cands_lang = [(lg, d["n"]) for lg, d in v["langs"].items()]
        # 只挑一种语言：优先 chs_eng > chs > cht_eng > cht > eng，其次覆盖集数
        if not cands_lang:
            return None, []
        rank = {"chs_eng": 0, "chs": 1, "cht_eng": 2, "cht": 3, "eng": 4}
        cands_lang.sort(key=lambda t: (rank.get(t[0], 9), -t[1]))
        lg = cands_lang[0][0]
        eps = v["langs"][lg]["eps"]
        want = parse_episode_filter(episodes, sorted(eps))
        picks = [(ep, eps[ep]) for ep in sorted(eps) if ep in want]
    elif variants:
        groups = season_groups(con, video, season, lang_pref)
        pairs = groups[0]["pairs"] if groups else []
        want = parse_episode_filter(episodes, [e for e, _ in pairs])
        picks = [(ep, c) for ep, cands in pairs if ep in want for c in cands]
    else:
        picks = season_pick(con, video, season, lang_pref, episodes)
    if not picks:
        return None, []

    buf = io.BytesIO()
    manifest = []
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for ep, c in picks:
            ext = "." + (c["ext"] or "srt")
            lang = PLAYER_LANG.get(c["lang"], "zh")
            base = f"S{season:02d}E{ep:02d}.{lang}"
            fn = base + ext
            k = 2
            while fn in used:                     # 同一集同一语言有多个版本
                fn = f"{base}_{k}{ext}"
                k += 1
            used.add(fn)
            tmp = Path(tempfile.mkdtemp()) / fn
            try:
                n, data = materialize(con, c, tmp)
            except Exception as e:
                manifest.append((ep, fn, 0, f"失败: {e}"))
                continue
            data, enc = normalize_text_bytes(data)
            z.writestr(fn, data)
            manifest.append((ep, fn, len(data), enc))
    return buf.getvalue(), manifest


def cmd_season(args):
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    p = Path(args.target).expanduser()
    name = p.name if p.is_file() else args.target
    video = parse_video_name(name)
    season = args.season if args.season is not None else video["season"]
    if season is None:
        print("没能从输入里识别出季号。请显式指定，例如： ./run.sh match season \"权力的游戏\" -s 8")
        return 2

    pref = {a.strip() for a in args.lang.split(",") if a.strip()} if args.lang else None
    groups = season_groups(con, video, season, pref)
    if not groups:
        print(f"没找到《{name}》第 {season} 季的字幕。")
        return 1

    g = groups[0]
    pairs = g["pairs"]
    print(f"《{g['label']}》第 {season} 季 —— 共 {len(pairs)} 集有字幕\n")
    for ep, cands in pairs:
        b = cands[0]
        print(f"  S{season:02d}E{ep:02d}  {LANG_LABEL.get(b['lang'],'?'):<4} "
              f"{len(cands):>3} 个候选    {b['inner_path'].rsplit('/', 1)[-1][:62]}")
    if len(groups) > 1:
        print(f"\n  ⚠️  「{name}」还匹配到 {len(groups)-1} 个相近的剧集分组，"
              f"可能不是同一部剧：")
        for o in groups[1:6]:
            print(f"      ·《{o['label']}》 {len(o['pairs'])} 集   "
                  f"(series_key={o['series_key'] or '-'})")

    if not args.out and not args.zip:
        print(f"\n（只是预览。加 --out 目录 或 --zip 文件 才会真正导出）")
        return 0

    picks = [(ep, cands[0]) for ep, cands in pairs
             if ep in parse_episode_filter(args.episodes, [e for e, _ in pairs])]
    if args.zip:
        data, manifest = build_season_zip(con, video, season, pref, args.episodes)
        out = Path(args.zip).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        print(f"\n✅ 已打包 {len(manifest)} 集 -> {out}  ({len(data)/1024:.0f} KB)")
        return 0

    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for ep, c in picks:
        ext = "." + (c["ext"] or "srt")
        lang = PLAYER_LANG.get(c["lang"], "zh")
        dest = outdir / f"S{season:02d}E{ep:02d}.{lang}{ext}"
        try:
            n, data = materialize(con, c, dest)
            data, enc = normalize_text_bytes(data)
            if enc != "utf-8":
                dest.write_bytes(data)
            ok += 1
        except Exception as e:
            print(f"  !! S{season:02d}E{ep:02d}: {e}")
    print(f"\n✅ 已导出 {ok} 集到 {outdir}")
    print(f"   文件名形如 S{season:02d}E01.zh.ass，放进剧集目录即可被播放器识别。")
    con.close()
    return 0


# ---------------------------------------------------------------------------
# CLI: find
# ---------------------------------------------------------------------------
def cmd_find(args):
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    raw = args.target
    p = Path(raw).expanduser()
    is_file = p.is_file()
    name = p.name if is_file else raw
    video = parse_video_name(name)

    print(f"解析: {name}")
    print(f"  release_key = {video['release_key']}")
    print(f"  series_key  = {video['series_key']}")
    print(f"  title_key   = {video['title_key']}")
    print(f"  season/ep   = {video['season']}/{video['episode']}   year = {video['year']}")
    print()

    pref = None
    if args.lang:
        pref = {a.strip() for a in args.lang.split(",") if a.strip()}
    cands = find_candidates(con, video, limit=args.limit * 20, lang_pref=pref)

    if not cands:
        print("没有找到匹配的字幕。")
        print("提示：试试 `search-raw` 直接按片名关键字搜，或去掉文件名里的杂项。")
        return 1

    print(f"命中 {len(cands)} 条候选，前 {min(args.limit, len(cands))} 条：\n")
    for i, c in enumerate(cands[: args.limit]):
        tag = LANG_LABEL.get(c["lang"], c["lang"] or "?")
        print(f"[{i}] L{c['_level']:.0f} score={c['_score']:.0f}  {tag:<4} .{c['ext']:<3} "
              f"{(str(c['size']) + 'B') if (c['size'] or 0) > 0 else '   ?':>9}")
        title = c["cnname"] or (c["series_key"] or "").replace(".", " ").strip() or "-"
        print(f"    {title} | {c['enname'] or '-'}")
        print(f"    {c['inner_path']}")
        print(f"    依据: {c['_why']}")
        print(f"    来源: {c['fullpath']}")
        print()

    if args.dry_run:
        return 0

    best = cands[0]
    if is_file:
        if args.out:
            dest = Path(args.out).expanduser()
            if dest.is_dir() or args.out.endswith("/"):
                dest = subtitle_target_path(p, best).name
                dest = Path(args.out).expanduser() / dest
        else:
            dest = subtitle_target_path(p, best)
    else:
        dest = Path(args.out or ".").expanduser() / (
            Path(best["inner_path"]).name)

    n, data = materialize(con, best, dest)
    data, enc = normalize_text_bytes(data)
    if enc != "utf-8":
        with open(dest, "wb") as f:
            f.write(data)
    print(f"✅ 已写出: {dest}  ({n} 字节, 原编码 {enc} -> UTF-8)")
    print(f"   播放器通常会自动加载同目录同名的 .zh.srt / .zh.ass 字幕。")
    return 0


def cmd_serve(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    con = sqlite3.connect(args.db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    state = {"con": con}

    SEARCH_PER = 30          # 普通检索：每页条数
    SEASON_PER = 8           # 整季视图：每页集数

    PAGE = """<!doctype html><html lang="zh"__THEME__><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地语料检索</title>
<script>
(function(){
  var K='corpus-theme', saved=null;
  try{ saved=localStorage.getItem(K); }catch(e){}
  var fromUrl = document.documentElement.getAttribute('data-theme');   // ?theme=xxx 由服务端写入
  var t = saved || fromUrl
        || ((window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme', t);
  function label(){ var b=document.getElementById('themebtn');
    if(b){ var l=document.documentElement.getAttribute('data-theme')==='light';
           b.textContent = l ? '\\u263E' : '\\u2600';
           b.title = l ? '切换到暗色' : '切换到亮色'; } }
  window.__toggleTheme=function(){
    var n = document.documentElement.getAttribute('data-theme')==='light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', n);
    try{ localStorage.setItem(K, n); }catch(e){}
    label();
  };
  window.__toggleAll=function(){
    var ds=document.querySelectorAll('details.epblock'), i;
    var anyClosed=false;
    for(i=0;i<ds.length;i++){ if(!ds[i].open){ anyClosed=true; break; } }
    for(i=0;i<ds.length;i++){ ds[i].open=anyClosed; }
    var b=document.getElementById('expandall');
    if(b){ b.textContent = anyClosed ? '全部收起' : '全部展开'; }
  };
  document.addEventListener('DOMContentLoaded', label);
})();
</script>
<style>
:root{
  --bg:#14161a; --card:#1a1d23; --input:#1d2026; --line:#262a31; --line2:#2e333c;
  --fg:#e8eaed; --fg2:#8b94a3; --fg3:#7c8797; --fg4:#5f6a7a;
  --accent:#2f6fed; --accent-h:#4280f5; --link:#7fb0ff; --on-accent:#fff;
  --bd-bg:#243447; --bd-fg:#7fb0ff;
  --l3-bg:#1e3d2b; --l3-fg:#6fdc9a; --l1-bg:#3a3320; --l1-fg:#e8c46a;
  --bn1:#1d2a44; --bn2:#1a1d23; --bn-line:#2b4468;
  --star:#e8c46a; --shadow:none; --hdr:rgba(20,22,26,.93);
  color-scheme:dark;
}
:root[data-theme="light"]{
  --bg:#f5f6f8; --card:#fff; --input:#fff; --line:#e4e7ec; --line2:#d0d5dd;
  --fg:#101828; --fg2:#667085; --fg3:#667085; --fg4:#98a2b3;
  --accent:#2f6fed; --accent-h:#1d5fd8; --link:#1a5fd6; --on-accent:#fff;
  --bd-bg:#e8f0fe; --bd-fg:#1a5fd6;
  --l3-bg:#dcfce7; --l3-fg:#15803d; --l1-bg:#fef3c7; --l1-fg:#a16207;
  --bn1:#e8f0fe; --bn2:#fff; --bn-line:#c7d9f8;
  --star:#d97706; --shadow:0 1px 2px rgba(16,24,40,.06); --hdr:rgba(245,246,248,.93);
  color-scheme:light;
}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;
     background:var(--bg);color:var(--fg);transition:background .18s,color .18s}
header{padding:20px 24px 14px;border-bottom:1px solid var(--line);position:sticky;top:0;
       background:var(--hdr);backdrop-filter:blur(12px);z-index:5}
.hrow{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
h1{margin:0;font-size:17px;font-weight:600;letter-spacing:.3px}
h1 span{color:var(--fg3);font-weight:400;font-size:13px;margin-left:8px}
button.theme{background:var(--card);color:var(--fg);border:1px solid var(--line2);
  width:38px;height:38px;border-radius:10px;font-size:17px;line-height:1;cursor:pointer;
  padding:0;flex:0 0 auto;transition:border-color .15s,background .15s}
button.theme:hover{border-color:var(--accent);background:var(--card)}
form{display:flex;gap:8px;flex-wrap:wrap}
input[type=search],select{background:var(--input);border:1px solid var(--line2);color:var(--fg);
  padding:9px 12px;border-radius:9px;font-size:14px;outline:none}
input[type=search]{flex:1;min-width:220px}
input[type=search]:focus,select:focus{border-color:var(--accent)}
button.go{background:var(--accent);color:var(--on-accent);border:0;padding:9px 18px;
  border-radius:9px;font-size:14px;font-weight:600;cursor:pointer}
button.go:hover{background:var(--accent-h)}
main{padding:18px 24px 60px}
.hint{color:var(--fg3);font-size:13px;padding:8px 0}
.hint a{color:var(--link)}

/* 普通检索结果 */
.row{display:flex;gap:14px;align-items:flex-start;padding:13px 15px;margin-bottom:8px;
  background:var(--card);border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow)}
.row:hover{border-color:var(--line2)}
.badge{flex:0 0 auto;font-size:12px;font-weight:700;padding:3px 8px;border-radius:6px;
  background:var(--bd-bg);color:var(--bd-fg);min-width:44px;text-align:center}
.badge.l3{background:var(--l3-bg);color:var(--l3-fg)}
.badge.l2{background:var(--bd-bg);color:var(--bd-fg)}
.badge.l1{background:var(--l1-bg);color:var(--l1-fg)}
.meta{flex:1;min-width:0}
.title{font-weight:600;margin-bottom:2px}
.title em{font-style:normal;color:var(--fg2);font-weight:400;margin-left:8px;font-size:13px}
.path{color:var(--fg3);font-size:12.5px;font-family:ui-monospace,Menlo,monospace;
  word-break:break-all;margin-top:3px}
.why{color:var(--fg4);font-size:12px;margin-top:4px}
.act{flex:0 0 auto;display:flex;gap:6px;align-items:center}
a.dl{text-decoration:none;background:var(--accent);color:var(--on-accent);padding:7px 14px;
  border-radius:8px;font-size:13px;font-weight:600;white-space:nowrap}
a.dl:hover{background:var(--accent-h)}
a.dl.alt{background:var(--input);color:var(--fg2);border:1px solid var(--line2)}
a.dl.alt:hover{background:var(--card);color:var(--fg)}

/* 整季视图 */
.banner{display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap;
  padding:14px 16px;margin-bottom:14px;border-radius:11px;
  background:linear-gradient(100deg,var(--bn1),var(--bn2));border:1px solid var(--bn-line)}
.banner b{font-size:15px}
.banner small{display:block;color:var(--fg2);margin-top:3px;font-weight:400}
.banner .acts{display:flex;gap:8px;flex-wrap:wrap}
a.pack{text-decoration:none;background:var(--accent);color:var(--on-accent);padding:9px 18px;
  border-radius:9px;font-size:14px;font-weight:700;white-space:nowrap}
a.pack:hover{background:var(--accent-h)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 12px;
  font-size:13px;color:var(--fg3)}
.toolbar a{color:var(--link);text-decoration:none}
.toolbar a:hover{text-decoration:underline}

.epblock{margin-bottom:14px;border:1px solid var(--line);border-radius:11px;
  background:var(--card);overflow:hidden;box-shadow:var(--shadow)}
.ephead{display:flex;gap:10px;align-items:center;padding:10px 14px;
  background:var(--input);border-bottom:1px solid var(--line)}
.ephead .no{font-weight:700;color:var(--link);font-family:ui-monospace,Menlo,monospace;
  font-size:14px;min-width:64px}
.ephead .cnt{color:var(--fg3);font-size:12.5px;flex:1}
.ver{display:flex;gap:10px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);
  font-size:13px}
.ver:last-child{border-bottom:0}
.ver:hover{background:var(--input)}
.ver.best{background:linear-gradient(90deg,var(--bn1),transparent 60%)}
.ver .star{color:var(--star);flex:0 0 auto;width:14px;text-align:center;font-size:12px}
.ver .vpath{flex:1;min-width:0;color:var(--fg2);font-family:ui-monospace,Menlo,monospace;
  font-size:12.5px;word-break:break-all}
.ver .vsize{color:var(--fg4);font-size:12px;white-space:nowrap;flex:0 0 auto}
.ver .badge{min-width:40px;font-size:11.5px;padding:2px 6px}
.ver .act a.dl{padding:5px 11px;font-size:12.5px}

/* ---- 视图切换 ---- */
.tabs{display:flex;gap:2px;margin:0 0 18px;border-bottom:1px solid var(--line)}
.tab{text-decoration:none;color:var(--fg3);padding:9px 18px;border-radius:9px 9px 0 0;
  font-size:14px;border:1px solid transparent;border-bottom:0;margin-bottom:-1px}
.tab:hover{color:var(--fg);background:var(--input)}
.tab.on{background:var(--card);border-color:var(--line);color:var(--fg);font-weight:600}

/* ---- 面包屑 / 标题 ---- */
.crumb{margin:0 0 12px;font-size:13px}
.crumb a{color:var(--link);text-decoration:none}
.crumb a:hover{text-decoration:underline}
h1 a{color:inherit;text-decoration:none}
h1 a:hover{color:var(--link)}

/* ---- 标题区 ---- */
.head{margin:0 0 16px}
.head h2{margin:0;font-size:19px;font-weight:600;letter-spacing:.2px;line-height:1.3}
.head h2 .sep{color:var(--fg4);font-weight:400;margin:0 9px}
.head .sub{color:var(--fg3);font-size:13px;margin-top:5px}

.note{color:var(--fg3);font-size:13px;line-height:1.65;margin:0 0 14px;
  padding:11px 15px;background:var(--input);border:1px solid var(--line);border-radius:9px}
.altrow{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.empty{color:var(--fg3);font-size:13px;padding:28px 0;text-align:center}

/* ---- 按钮 ---- */
.btn{display:inline-block;text-decoration:none;padding:7px 15px;border-radius:8px;
  font-size:13px;font-weight:600;white-space:nowrap;background:var(--input);
  color:var(--fg2);border:1px solid var(--line2);
  transition:background .14s,border-color .14s,color .14s}
.btn:hover{border-color:var(--accent);color:var(--fg)}
.btn.primary{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.btn.primary:hover{background:var(--accent-h);border-color:var(--accent-h)}
.btn.small{padding:6px 12px;font-size:12.5px}
.btn .n{opacity:.55;margin-left:5px;font-weight:400}

/* ---- 季列表 ---- */
.seasrow{display:flex;align-items:center;gap:16px;padding:15px 18px;margin-bottom:10px;
  background:var(--card);border:1px solid var(--line);border-radius:11px;
  box-shadow:var(--shadow);transition:border-color .14s}
.seasrow:hover{border-color:var(--line2)}
.sleft{flex:1;min-width:0;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.sno{font-weight:600;font-size:15px;color:var(--fg);min-width:66px}
.sinfo{color:var(--fg3);font-size:13px}
.sright{display:flex;gap:8px;flex:0 0 auto}

/* ---- 推荐包 ---- */
.reco{background:linear-gradient(100deg,var(--bn1),var(--bn2));
  border:1px solid var(--star);border-radius:11px;padding:14px 16px;margin-bottom:16px}
.recohead{font-size:14px;font-weight:700;margin-bottom:5px}
.recohead .star{color:var(--star);margin-right:5px}
.recohead .ok{color:var(--l3-fg);font-weight:600;margin-left:8px}
.recohead .bad{color:var(--l1-fg);font-weight:600;margin-left:8px}
.reconote{color:var(--fg2);font-size:12.5px;line-height:1.6}
.reco .langrow{padding:11px 0 0}

/* ---- 整季下载：版本卡片 ---- */
.verbox{background:var(--card);border:1px solid var(--line);border-radius:11px;
  margin-bottom:10px;overflow:hidden;box-shadow:var(--shadow)}
.verhead{display:flex;align-items:center;gap:12px;padding:11px 16px;
  background:var(--input);border-bottom:1px solid var(--line)}
.vno{flex:0 0 auto;width:22px;height:22px;line-height:22px;text-align:center;border-radius:50%;
  background:var(--bd-bg);color:var(--bd-fg);font-size:12px;font-weight:700}
.vname{flex:1;min-width:0;font-family:ui-monospace,Menlo,monospace;font-size:13px;
  font-weight:600;word-break:break-all}
.vcov{color:var(--fg3);font-size:12.5px;white-space:nowrap}
.warn{color:var(--l1-fg);margin-left:6px}
.langrow{display:flex;gap:8px;flex-wrap:wrap;padding:13px 16px}

/* ---- 分集下载 ---- */
.epblock{margin-bottom:12px;border:1px solid var(--line);border-radius:11px;
  background:var(--card);overflow:hidden;box-shadow:var(--shadow)}
/* 用原生 <details> 折叠：默认收起，用户先扫集号再展开自己要看的那集 */
.epblock>summary{cursor:pointer;list-style:none;display:flex;gap:10px;align-items:center;
  padding:11px 14px;background:var(--input);border-bottom:1px solid transparent}
.epblock[open]>summary{border-bottom-color:var(--line)}
.epblock>summary::-webkit-details-marker{display:none}
.epblock>summary::before{content:"▸";color:var(--fg4);font-size:11px;flex:0 0 auto;
  transition:transform .15s}
.epblock[open]>summary::before{content:"▾"}
.epblock>summary:hover{background:var(--card)}
.ephead .no{font-weight:700;color:var(--link);font-family:ui-monospace,Menlo,monospace;
  font-size:14px;min-width:64px;flex:0 0 auto}
.ephead .cnt{color:var(--fg3);font-size:12.5px;flex:0 0 auto}
.ephead .prev{flex:1;min-width:0;color:var(--fg4);font-size:12px;
  font-family:ui-monospace,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;text-align:right}
.ver{display:flex;gap:10px;align-items:center;padding:9px 14px;
  border-bottom:1px solid var(--line);font-size:13px}
.ver:last-child{border-bottom:0}
.ver:hover{background:var(--input)}
.ver.best{background:linear-gradient(90deg,var(--bn1),transparent 62%)}
.ver .star{color:var(--star);flex:0 0 auto;width:14px;text-align:center;font-size:12px}
.ver .vpath{flex:1;min-width:0;color:var(--fg2);font-family:ui-monospace,Menlo,monospace;
  font-size:12.5px;word-break:break-all}
.ver .vsize{color:var(--fg4);font-size:12px;white-space:nowrap;flex:0 0 auto}
.ver .badge{min-width:40px;font-size:11.5px;padding:2px 6px}
.ver .act a.dl{padding:5px 11px;font-size:12.5px}

/* 分页 */
.pager{display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:center;
  padding:18px 0 6px}
.pg{display:inline-block;min-width:34px;text-align:center;padding:6px 10px;border-radius:8px;
  text-decoration:none;color:var(--fg2);background:var(--card);border:1px solid var(--line);
  font-size:13px}
.pg:hover{border-color:var(--accent);color:var(--fg)}
.pg.cur{background:var(--accent);color:var(--on-accent);border-color:var(--accent);font-weight:700}
.pgd{color:var(--fg4);padding:0 2px}
</style></head><body>
<header>
  <div class="hrow">
    <h1><a href="/" title="回到首页，清空检索">本地语料检索</a> <span>__COUNT__ 条记录已索引</span></h1>
    <button id="themebtn" class="theme" onclick="__toggleTheme()" aria-label="切换明暗主题"></button>
  </div>
  <form method="get" action="/">
    <input type="search" name="q" placeholder="片名 / 文件名关键词；带季号可看整季，例如 某某剧 S08"
           value="__Q__" autofocus>
    <select name="lang">
      <option value="">语言不限</option>
      <option value="chs"__S1__>简体</option>
      <option value="chs_eng"__S2__>简英双语</option>
      <option value="cht"__S3__>繁体</option>
      <option value="cht_eng"__S4__>繁英双语</option>
      <option value="eng"__S5__>英文</option>
    </select>
    <button class="go" type="submit">搜索</button>
  </form>
</header><main>__BODY__</main></body></html>"""

    # ------------------------------------------------------------------
    def _page_bar(base, page, pages):
        """底部分页条。base 是除 page 外的查询参数字典。"""
        if pages <= 1:
            return ""

        def link(p, label, cls=""):
            d = dict(base)
            d["page"] = p
            c = f' class="pg {cls}"' if cls else ' class="pg"'
            return f'<a{c} href="/?{urllib.parse.urlencode(d)}">{label}</a>'

        out = ['<div class="pager">']
        if page > 1:
            out.append(link(page - 1, "‹ 上一页"))
        lo, hi = max(1, page - 2), min(pages, page + 2)
        if lo > 1:
            out.append(link(1, "1"))
            if lo > 2:
                out.append('<span class="pgd">…</span>')
        for p in range(lo, hi + 1):
            out.append(f'<span class="pg cur">{p}</span>' if p == page else link(p, str(p)))
        if hi < pages:
            if hi < pages - 1:
                out.append('<span class="pgd">…</span>')
            out.append(link(pages, str(pages)))
        if page < pages:
            out.append(link(page + 1, "下一页 ›"))
        out.append("</div>")
        return "".join(out)

    def _shell(q, lang, body, theme=None):
        page = PAGE
        page = page.replace("__THEME__",
                            f' data-theme="{theme}"' if theme in ("light", "dark") else "")
        page = page.replace("__COUNT__", "{:,}".format(state["n"]))
        page = page.replace("__Q__", html.escape(q or ""))
        for i, v in enumerate(["chs", "chs_eng", "cht", "cht_eng", "eng"], 1):
            page = page.replace(f"__S{i}__", " selected" if lang == v else "")
        page = page.replace("__BODY__", "".join(body))
        return page.encode("utf-8")

    def _ver_row(c, season=None, ep=None):
        """整季视图里的「一个版本」行。"""
        tag = LANG_LABEL.get(c["lang"], c["lang"] or "?")
        qs = urllib.parse.urlencode({"fp": c["fullpath"], "ip": c["inner_path"]})
        size = fmt_size(c["size"])
        name = c["inner_path"].rsplit("/", 1)[-1]
        return (f'<div class="ver">'
                f'<span class="badge l{int(c["_level"])}">{tag}</span>'
                f'<span class="vpath" title="{html.escape(c["inner_path"])}">'
                f'{html.escape(name)}</span>'
                f'<span class="vsize">{size}</span>'
                f'<span class="act"><a class="dl" href="/get?{qs}&amp;conv=1">下载</a></span></div>')

    # ------------------------------------------------------------------
    def render(q, lang, rows, note="", empty_hint=True, page=1, per=SEARCH_PER,
               theme=None):
        total = len(rows)
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        chunk = rows[(page - 1) * per: page * per]

        body = []
        if note:
            body.append(f'<div class="hint">{note}</div>')
        if total > per:
            body.append(f'<div class="hint">共 <b>{total}</b> 条结果 · '
                        f'第 {page}/{pages} 页</div>')

        for r in chunk:
            lvl = int(r["_level"])
            tag = LANG_LABEL.get(r["lang"], r["lang"] or "?")
            qs = urllib.parse.urlencode({"fp": r["fullpath"], "ip": r["inner_path"]})
            size = fmt_size(r["size"])
            body.append(
                f'<div class="row"><div class="badge l{lvl}">{tag}</div>'
                f'<div class="meta"><div class="title">{html.escape(r["cnname"] or "-")}'
                f'<em>{html.escape(r["enname"] or "")}</em></div>'
                f'<div class="path">{html.escape(r["inner_path"])}</div>'
                f'<div class="why">{html.escape(r["_why"])} · '
                f'{size} · {r["ext"]}</div></div>'
                f'<div class="act"><a class="dl" href="/get?{qs}">下载</a>'
                f'<a class="dl alt" href="/get?{qs}&amp;conv=1">转UTF-8</a></div></div>')

        if not chunk and not note and empty_hint:
            body.append('<div class="hint">没有结果。试试只输入片名，或换用英文名。</div>')

        if pages > 1:
            body.append(_page_bar({"q": q, "lang": lang}, page, pages))
        return _shell(q, lang, body, theme)

    # ------------------------------------------------------------------
    def render_season(q, lang, video, season, groups, page=1, per=SEASON_PER,
                      show_all=True, theme=None):
        """分集下载：每一集的**全部版本**都罗列出来，按集分页。

        这里刻意不做任何推荐 —— 用户下载的整季通常是同一个版本，
        由工具替他挑「最优」反而会让每集版本不一致。要整季打包请到「整季下载」。
        """
        pairs = groups[0]["pairs"]
        label = groups[0]["label"]
        series_q = groups[0]["series_key"] or q
        n_ver = sum(len(cs) for _e, cs in pairs)
        total = sum((cs[0]["size"] or 0) for _e, cs in pairs if cs)

        pages = max(1, (len(pairs) + per - 1) // per)
        page = min(max(1, page), pages)
        chunk = pairs[(page - 1) * per: page * per]

        base = {"q": series_q, "season": season, "lang": lang, "per": per}
        if theme:
            base["theme"] = theme

        unit = "个特别篇" if season == 0 else "集"
        body = [_crumb(series_q, label, lang, theme),
                _tabs(series_q, lang, season, "episode", theme)]
        body.append(f'<div class="head"><h2>《{html.escape(label)}》'
                    f'<span class="sep">/</span>{season_title(season)}</h2>'
                    f'<div class="sub">共 {len(pairs)} {unit} · {n_ver} 个版本 · '
                    f'约 {total / 1e6:.1f} MB</div></div>')
        body.append('<div class="note">每一集有哪些版本就列哪些，按需挑一个下载 —— '
                    '工具不做推荐。集号默认收起，点一下展开该集全部版本；'
                    '要整季打包请到「整季下载」，那里每个版本一个包。</div>')

        sizes = []
        for n in (4, 8, 16, 32):
            d = dict(base)
            d["per"] = n
            d["page"] = 1
            sizes.append(f'<a class="pg" href="/?{urllib.parse.urlencode(d)}">{n} 集/页</a>')
        body.append(f'<div class="toolbar"><span>第 {page}/{pages} 页 · 每页 {per} 集 · '
                    f'点集号展开</span>'
                    f'<a href="#" id="expandall" onclick="__toggleAll();return false;">'
                    f'全部展开</a>{"".join(sizes)}</div>')

        for ep, cands in chunk:
            epno = ("SP%02d" % ep) if season == 0 else ("S%02dE%02d" % (season, ep))
            rows = "".join(_ver_row(c) for c in cands)
            first = cands[0]
            prev = first["inner_path"].rsplit("/", 1)[-1]
            body.append(
                f'<details class="epblock"><summary class="ephead">'
                f'<span class="no">{epno}</span>'
                f'<span class="cnt">{len(cands)} 个版本</span>'
                f'<span class="prev">{html.escape(prev[:70])} · '
                f'{fmt_size(first["size"])}</span>'
                f'</summary>{rows}</details>')

        body.append(_page_bar(base, page, pages))

        if len(groups) > 1:
            alts = []
            for o in groups[1:6]:
                aq = urllib.parse.urlencode({"q": o["series_key"], "season": season,
                                             "lang": lang})
                alts.append(f'<a class="pg" href="/?{aq}">《{html.escape(o["label"])}》'
                            f' {len(o["pairs"])} 集</a>')
            body.append(f'<div class="note">还匹配到 {len(groups) - 1} 个相近分组，'
                        f'可能不是同一部剧：<div class="altrow">{"".join(alts)}</div></div>')
        return _shell(q, lang, body, theme)

    def _tabs(q, lang, season, active, theme=None):
        """整季下载 / 分集下载 两个视图的切换条。"""
        base = {"q": q, "season": season, "lang": lang}
        if theme:
            base["theme"] = theme

        def one(view, label):
            d = dict(base)
            d["view"] = view
            cls = "tab on" if view == active else "tab"
            return f'<a class="{cls}" href="/?{urllib.parse.urlencode(d)}">{label}</a>'
        return f'<div class="tabs">{one("season", "整季下载")}{one("episode", "分集下载")}</div>'

    def _crumb(series_q, label, lang, theme=None):
        """回到「这部剧的全部季」。

        只在这部剧确实不止一季（或有特别篇）时才给 —— 单季剧点了会原地打转。
        """
        n = state["con"].execute(
            "SELECT count(DISTINCT season) FROM subs WHERE series_key=? OR series_key LIKE ?",
            (series_q, series_q + ".%")).fetchone()[0]
        if n < 2:
            return ""
        d = {"q": series_q}
        if lang:
            d["lang"] = lang
        if theme:
            d["theme"] = theme
        return (f'<div class="crumb"><a href="/?{urllib.parse.urlencode(d)}">'
                f'← 返回《{html.escape(label)}》全部季</a></div>')

    def render_series(q, lang, overview, theme=None):
        """剧名检索结果：按季列出，每季给「整季下载」「分集下载」两个入口。"""
        g = overview[0]
        n_ep = sum(len(p) for _s, p, _v in g["seasons"])
        n_f = sum(len(c) for _s, p, _v in g["seasons"] for _e, cs in p for c in cs)

        body = [f'<div class="head"><h2>《{html.escape(g["label"])}》</h2>'
                f'<div class="sub">共 {len(g["seasons"])} 季 · {n_ep} 集 · '
                f'{n_f} 个文件</div></div>']

        for se, pairs, vers in g["seasons"]:
            nf = sum(len(c) for _e, c in pairs)
            sq = urllib.parse.urlencode({"q": g["series_key"] or q, "season": se,
                                         "lang": lang})
            unit = "个特别篇" if se == 0 else "集"
            body.append(
                f'<div class="seasrow"><div class="sleft">'
                f'<span class="sno">{season_title(se)}</span>'
                f'<span class="sinfo">{len(pairs)} {unit} · {nf} 个文件 · '
                f'{vers} 个版本</span></div>'
                f'<div class="sright">'
                f'<a class="btn primary" href="/?{sq}&view=season">整季下载</a>'
                f'<a class="btn" href="/?{sq}&view=episode">分集下载</a>'
                f'</div></div>')

        if len(overview) > 1:
            alts = []
            for o in overview[1:6]:
                aq = urllib.parse.urlencode({"q": o["series_key"], "lang": lang})
                alts.append(f'<a class="btn small" href="/?{aq}">'
                            f'《{html.escape(o["label"])}》 {len(o["seasons"])} 季</a>')
            body.append(f'<div class="note">「{html.escape(q)}」还匹配到 '
                        f'{len(overview) - 1} 个相近的分组，可能不是同一部剧：'
                        f'<div class="altrow">{"".join(alts)}</div></div>')
        return _shell(q, lang, body, theme)

    def render_versions(q, lang, video, season, group, theme=None):
        """整季下载视图：一个发布版本 = 一个结果，按语言分别打包。"""
        series_q = group["series_key"] or q
        vers = season_versions(state["con"], video, season)
        if group.get("lang_pref"):
            pass

        body = [_crumb(series_q, group["label"], lang, theme),
                _tabs(series_q, lang, season, "season", theme)]
        n_ep = len(group["pairs"])
        unit = "个特别篇" if season == 0 else "集"
        body.append(f'<div class="head"><h2>《{html.escape(group["label"])}》'
                    f'<span class="sep">/</span>{season_title(season)}</h2>'
                    f'<div class="sub">共 {n_ep} {unit} · 按发布版本整理成 '
                    f'{len(vers)} 个包</div></div>')
        body.append('<div class="note">每个整季包里，每集只放一个字幕文件，'
                    '下载后可直接使用。点语言即开始打包下载。'
                    '<br><b>字幕是文本，与画面分辨率无关</b> —— 发布名里的 '
                    '720p / 1080p 指字幕对应的片源版本，不影响能否使用；'
                    '因此推荐以「覆盖是否完整」为准，而非画质。</div>')

        rec = recommend_season(state["con"], video, season)
        if rec["kind"] != "none":
            if rec["kind"] == "version":
                head = (f'<span class="star">★</span>推荐整季包'
                        f'<span class="ok">完整覆盖 {rec["total"]}/{rec["total"]} 集</span>')
                note = (f'单一发布版 <b>{html.escape(rec["label"])}</b> 就覆盖了全部 '
                        f'{rec["total"]} 集，整季为同一版本。')
            else:
                head = (f'<span class="star">★</span>推荐整季包（补全）'
                        + (f'<span class="ok">覆盖 {rec["total"]}/{rec["total"]} 集</span>'
                           if not rec["missing"]
                           else f'<span class="bad">仅覆盖 '
                                f'{rec["total"] - len(rec["missing"])}/{rec["total"]} 集</span>'))
                note = ('没有任何单一版本覆盖全部集数，此包由多个版本拼成，'
                        '每集各取一条，<b>各集的发布版本可能不同</b>。')
                if rec["missing"]:
                    note += (f'<br>以下集数所有版本都缺：'
                             f'{"、".join("E%02d" % e for e in rec["missing"])}')
            btns = []
            for lg, d in sorted(rec["langs"].items(), key=lambda t: LANG_RANK.get(t[0], 9)):
                if d["n"] < rec["total"] and rec["kind"] == "version":
                    continue
                tag = LANG_LABEL.get(lg, lg or "未标注")
                q2 = {"q": series_q, "season": season, "lang": lg}
                if rec["kind"] == "version":
                    q2["family"] = rec["family"]
                else:
                    q2["merge"] = 1
                btns.append(f'<a class="btn small" href="/season.zip?'
                            f'{urllib.parse.urlencode(q2)}">下载 {tag} '
                            f'<span class="n">{d["n"]} 集 · '
                            f'{fmt_size(d["bytes"]) if d["bytes"] else "大小未知"}'
                            f'</span></a>')
            if btns:
                body.append(f'<div class="reco"><div class="recohead">{head}</div>'
                            f'<div class="reconote">{note}</div>'
                            f'<div class="langrow">{"".join(btns)}</div></div>')

        if not vers:
            body.append('<div class="empty">这一季**没有**「同一个发布版本覆盖多集」的包<br>'
                        '—— 每一集来自不同的发布组，凑不出统一的版本。</div>')
            # 退路：按语言合并。明确写清每集版本可能不同，由用户自己决定要不要。
            langs = {}
            for _ep, cs in group["pairs"]:
                for c in cs:
                    langs.setdefault(c["lang"], 0)
                    langs[c["lang"]] += 1
            floor = min(2, len(group["pairs"]))
            btns = []
            for lg, n in sorted(langs.items(), key=lambda t: LANG_RANK.get(t[0], 9)):
                if n < floor:
                    continue
                tag = LANG_LABEL.get(lg, lg or "未标注")
                zq = urllib.parse.urlencode({"q": series_q, "season": season,
                                             "lang": lg, "merge": 1})
                btns.append(f'<a class="btn small" href="/season.zip?{zq}">'
                            f'下载 {tag} <span class="n">{n} 个文件</span></a>')
            if btns:
                body.append('<div class="note" style="border-color:var(--l1-fg)">'
                            '⚠️ 下面是**按语言合并**的整季包：每集取该语言下最先找到的一条，'
                            '所以各集的发布版本可能不一致。要每集自己挑，请用「分集下载」。'
                            f'<div class="langrow" style="padding:12px 0 0">{"".join(btns)}</div>'
                            '</div>')
            return _shell(q, lang, body, theme)

        for i, v in enumerate(vers, 1):
            partial = v["coverage"] < v["total"]
            cov = f'{v["coverage"]}/{v["total"]} 集'
            warn = f'<span class="warn">缺 {v["total"] - v["coverage"]} 集</span>' if partial else ''
            btns = []
            # 语言覆盖不足两个就不给按钮 —— 但整季只有一集时（比如单个特别篇）
            # 门槛要放宽到 1，否则一个按钮都出不来。
            floor = min(2, v["total"])
            for lg, d in sorted(v["langs"].items(), key=lambda t: LANG_RANK.get(t[0], 9)):
                if d["n"] < floor:
                    continue
                tag = LANG_LABEL.get(lg, lg or "未标注")
                d["bytes"] = sum((c["size"] or 0) for c in d["eps"].values()
                                 if (c["size"] or 0) > 0)
                zq = urllib.parse.urlencode({"q": series_q, "season": season,
                                             "lang": lg, "family": v["family"]})
                btns.append(f'<a class="btn small" href="/season.zip?{zq}">'
                            f'下载 {tag} <span class="n">{d["n"]} 集 · '
                            f'{fmt_size(d["bytes"]) if d["bytes"] else "大小未知"}'
                            f'</span></a>')
            if not btns:
                continue
            body.append(
                f'<div class="verbox"><div class="verhead">'
                f'<span class="vno">{i}</span>'
                f'<span class="vname">{html.escape(v["short"])}</span>'
                f'<span class="vcov">{cov}{warn}</span></div>'
                f'<div class="langrow">{"".join(btns)}</div></div>')

        return _shell(q, lang, body, theme)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, data, ctype, filename=None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if filename:
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _int(self, qs, key, default=1):
            try:
                return max(1, int(qs.get(key, [""])[0]))
            except (ValueError, TypeError):
                return default

        def do_GET(self):
            try:
                self._route()
            except sqlite3.Error as e:
                # 典型场景：一边重建索引一边开着服务。SQLite 读到被换掉的页会报
                # "database disk image is malformed"。
                # 注意不能用 send_error() —— 它把 reason 塞进 HTTP 状态行，
                # 而状态行只能放 latin-1，中文会抛 UnicodeEncodeError，
                # 结果连错误响应都发不出去，客户端只看到连接被断开。
                self._fail(503, "索引暂时不可用", str(e),
                           "如果刚在重建索引，等它跑完再刷新。"
                           "重建前建议先执行 ./stop.sh。")
            except BrokenPipeError:
                pass
            except Exception as e:
                # 兜底。未捕获的异常会让 socketserver 直接掐断连接，
                # 浏览器只显示「网页出错」，看不出发生了什么。
                # 宁可回一个 500 页面，把异常类型和信息带上。
                import traceback
                traceback.print_exc()
                self._fail(500, "检索时发生内部错误",
                           f"{type(e).__name__}: {e}",
                           "这属于程序缺陷。日志里有完整调用栈："
                           "storage/logs/server.log")

        def _fail(self, code, title, detail="", hint=""):
            """自己拼一个 UTF-8 的错误页，不走 send_error（状态行放不下中文）。"""
            try:
                body = (f'<!doctype html><html lang="zh"><meta charset="utf-8">'
                        f'<title>{code}</title>'
                        f'<body style="font:15px/1.6 -apple-system,sans-serif;'
                        f'padding:40px;max-width:640px;margin:auto">'
                        f'<h2>{html.escape(title)}</h2>'
                        f'<p style="color:#888">{html.escape(detail)}</p>'
                        f'<p>{html.escape(hint)}</p></body>')
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                pass

        def _route(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)

            if u.path == "/season.zip":
                q = (qs.get("q", [""])[0] or "").strip()
                lang = qs.get("lang", [""])[0]
                season = qs.get("season", [""])[0]
                allv = qs.get("all", ["0"])[0] == "1"
                fam = (qs.get("family", [""])[0] or "").strip() or None
                if qs.get("merge", ["0"])[0] == "1":
                    fam = None
                if not q or not season.isdigit():
                    self.send_error(400, "missing q/season")
                    return
                v = parse_video_name(q)
                pref = {lang} if lang else None
                data, manifest = build_season_zip(state["con"], v, int(season), pref,
                                                  variants=allv, family=fam)
                if not data:
                    self.send_error(404, "no files for this season")
                    return
                self._send(data, "application/zip", f"S{int(season):02d}.zip")
                return

            if u.path == "/get":
                fullpath = qs.get("fp", [""])[0]
                inner = qs.get("ip", [""])[0]
                conv = qs.get("conv", ["0"])[0] == "1"
                r = state["con"].execute(
                    "SELECT * FROM subs WHERE fullpath=? AND inner_path=?",
                    (fullpath, inner)).fetchone()
                if not r:
                    self.send_error(404)
                    return
                tmp = Path(tempfile.mkdtemp()) / Path(inner).name
                try:
                    materialize(state["con"], dict(r), tmp)
                except Exception as e:
                    self.send_error(500, str(e))
                    return
                data = tmp.read_bytes()
                fn = Path(inner).name
                if conv:
                    data, enc = normalize_text_bytes(data)
                    stem, ext = os.path.splitext(fn)
                    fn = f"{stem}.utf8{ext}"
                self._send(data, "application/octet-stream", fn)
                return

            q = (qs.get("q", [""])[0] or "").strip()
            lang = qs.get("lang", [""])[0]
            theme = (qs.get("theme", [""])[0] or "").strip().lower() or None
            if theme not in ("light", "dark"):
                theme = None
            if not q:
                self._send(_shell("", lang, [], theme), "text/html; charset=utf-8")
                return

            v = parse_video_name(q)
            pref = {lang} if lang else None
            page = self._int(qs, "page", 1)

            # 1) 指定了季号（来自 URL 的 season=，或查询词里自带）-> 整季视图
            season_param = (qs.get("season", [""])[0] or "").strip()
            want_season = int(season_param) if season_param.isdigit() else v["season"]
            if want_season is not None and v["episode"] is None:
                sv = dict(v)
                sv["season"] = want_season
                sv["episode"] = None
                groups = season_groups(state["con"], sv, want_season, pref)
                if groups:
                    view = (qs.get("view", ["season"])[0] or "season").lower()
                    if view in ("season", "pack"):
                        self._send(render_versions(q, lang, sv, want_season, groups[0], theme),
                                   "text/html; charset=utf-8")
                        return
                    show_all = qs.get("all", ["1"])[0] != "0"
                    per = min(self._int(qs, "per", SEASON_PER), 60)
                    self._send(render_season(q, lang, sv, want_season, groups,
                                             page=page, per=per, show_all=show_all,
                                             theme=theme),
                               "text/html; charset=utf-8")
                    return

            # 2) 只有剧名（没有季集）-> 按季总览
            if v["season"] is None and (v["series_key"] or v.get("cjk")):
                overview = series_overview(state["con"], v, pref)
                if len(overview) == 1 and len(overview[0]["seasons"]) == 1:
                    # 只有一季就别多一层了，直接进按集视图
                    se, pairs, _pk = overview[0]["seasons"][0]
                    one = dict(overview[0])
                    one["pairs"] = pairs
                    self._send(render_season(q, lang, v, se, [one],
                                             page=page, per=min(self._int(qs, "per", SEASON_PER), 60),
                                             show_all=qs.get("all", ["1"])[0] != "0",
                                             theme=theme),
                               "text/html; charset=utf-8")
                    return
                # 注意先判 overview 非空：搜一个库里没有的剧名时
                # series_overview 返回 []，直接取 [0] 会 IndexError，
                # 未捕获的话连接被直接掐断，浏览器只看到「网页报错」。
                if overview and len(overview[0]["seasons"]) >= 2:
                    self._send(render_series(q, lang, overview, theme),
                               "text/html; charset=utf-8")
                    return

            rows = find_candidates(state["con"], v, limit=200, lang_pref=pref)
            ep_txt = ""
            if v["season"] is not None:
                ep_txt = f" S{v['season']:02d}E{(v['episode'] or 0):02d}"
            note = (f"解析：“{html.escape(q)}” → 片名 token "
                    f"[{html.escape(v['title_key'])}]{ep_txt}，命中 {len(rows)} 条")
            if v["season"] is not None:
                zq = urllib.parse.urlencode({"q": v["series_key"] or q,
                                             "season": v["season"], "lang": lang})
                note += (f' · <a href="/?{zq}">查看第 {v["season"]} 季整季 →</a>')
            self._send(render(q, lang, rows, note, page=page, theme=theme),
                       "text/html; charset=utf-8")

    state["n"] = con.execute("SELECT count(*) FROM subs").fetchone()[0]
    srv = ThreadingHTTPServer((args.host, args.port), H)
    url = f"http://{args.host}:{args.port}/"
    print(f"检索页已启动: {url}")
    print(f"索引 {state['n']:,} 条记录。Ctrl-C 退出。")
    if args.open:
        subprocess.Popen(["open", url])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


# ---------------------------------------------------------------------------
# CLI: scan
# ---------------------------------------------------------------------------
def cmd_scan(args):
    root = Path(args.dir).expanduser()
    vids = [p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXT
            and not any(x in str(p) for x in args.exclude)]
    print(f"扫描 {root}：找到 {len(vids)} 个视频文件")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    hit = miss = skip = 0
    miss_list = []
    for i, v in enumerate(vids, 1):
        existing = [p for p in v.parent.glob(v.stem + ".*")
                    if p.suffix.lower() in SUB_EXT]
        if existing and not args.force:
            skip += 1
            continue
        video = parse_video_name(v.name)
        cands = find_candidates(con, video, limit=50)
        if not cands:
            miss += 1
            miss_list.append(str(v))
            continue
        hit += 1
        if args.apply:
            best = cands[0]
            dest = subtitle_target_path(v, best)
            try:
                n, data = materialize(con, best, dest)
                data, enc = normalize_text_bytes(data)
                if enc != "utf-8":
                    dest.write_bytes(data)
                print(f"  ✅ {v.name}  ->  {dest.name}  [{LANG_LABEL.get(best['lang'],'?')}]")
            except Exception as e:
                print(f"  !! {v.name}: {e}")
        else:
            print(f"  {'✓' if cands[0]['_level'] >= 2 else '~'} {v.name}  "
                  f"[{LANG_LABEL.get(cands[0]['lang'],'?')}] {cands[0]['inner_path'][:70]}")
    print(f"\n命中 {hit} / 缺失 {miss} / 已有字幕跳过 {skip}")
    if miss_list:
        out = INDEX_DIR / "scan_missing.txt"
        out.write_text("\n".join(miss_list), encoding="utf-8")
        print(f"未命中的清单 -> {out}")
        for m in miss_list[:15]:
            print(f"   缺: {m}")
    con.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build-subs", help="建立匹配索引（约 1 分钟）")
    p.add_argument("--force", action="store_true", help="服务在跑也强行重建（不推荐）")
    p.add_argument("--db", default=str(DB_PATH))

    p = sub.add_parser("find", help="给一个视频文件/名字找字幕")
    p.add_argument("target")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--lang", help="逗号分隔，如 chs,chs_eng")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--out", help="输出文件或目录")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("season", help="整季检索 / 整季导出")
    p.add_argument("target", help="剧名，例如 \"权力的游戏\"")
    p.add_argument("-s", "--season", type=int, help="季号（输入里已含季号可省略）")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--lang", help="逗号分隔，如 chs,chs_eng")
    p.add_argument("--episodes", help="只导部分集，如 1-5,8,10")
    p.add_argument("--out", help="导出到目录")
    p.add_argument("--zip", help="打包成一个 zip 文件")

    p = sub.add_parser("scan", help="批量扫描影音库")
    p.add_argument("dir")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--apply", action="store_true", help="真的写出字幕")
    p.add_argument("--force", action="store_true", help="已有字幕也覆盖")
    p.add_argument("--exclude", nargs="*", default=["Sample", "sample", "Extras"])

    p = sub.add_parser("serve", help="起本地网页检索")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--host", default=HOST)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--open", action=argparse.BooleanOptionalAction,
                   default=AUTO_OPEN_BROWSER, help="启动后自动打开浏览器")

    a = ap.parse_args()
    if a.cmd == "build-subs":
        if not getattr(a, "force", False) and not require_service_stopped():
            return 1
        build_subs(a.db)
    elif a.cmd == "find":
        sys.exit(cmd_find(a))
    elif a.cmd == "season":
        sys.exit(cmd_season(a))
    elif a.cmd == "scan":
        cmd_scan(a)
    elif a.cmd == "serve":
        cmd_serve(a)


if __name__ == "__main__":
    main()
