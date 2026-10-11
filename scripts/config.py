#!/usr/bin/env python3
"""工程配置：代码、语料、生成物三者解耦。

目录约定（相对本文件）：

    <root>/
      dataset/        语料压缩包（用户自备，不入库）
      storage/        所有生成物：索引 / 元数据 / 日志 / PID
      scripts/        代码
      config.json     配置

语料位置、归档命名、端口等都可在 config.json 里改，也可用环境变量临时覆盖：

    CORPUS_DIR=/path/to/corpus ./start.sh
    APP_PORT=9000 ./start.sh
    APP_STORAGE_DIR=/Volumes/disk/store ./start.sh

本工具对语料格式**没有假设**：语料由「若干压缩包」组成，压缩包内部
可以是字幕文本（.srt/.ass/.ssa）或任意纯文本（.txt/.sub）。
归档命名规则见下面 ARCHIVE_* 三个常量，全部来自配置。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.json"

DEFAULTS = {
    "corpus_dir": "dataset",
    # 语料归档命名：一个分卷 zip（split_archive + .001/.002/…）+ 若干独立归档
    "split_archive": "data.zip",
    "split_parts": 4,
    "extra_archives": ["data2.zip"],
    "host": "127.0.0.1",
    "port": 8777,
    "auto_open_browser": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError) as e:
            print(f"[config] 读取 {CONFIG_PATH} 失败，用默认值: {e}")
    if os.environ.get("CORPUS_DIR"):
        cfg["corpus_dir"] = os.environ["CORPUS_DIR"]
    if os.environ.get("APP_HOST"):
        cfg["host"] = os.environ["APP_HOST"]
    if os.environ.get("APP_PORT"):
        try:
            cfg["port"] = int(os.environ["APP_PORT"])
        except ValueError:
            pass
    if os.environ.get("APP_AUTO_OPEN") is not None:
        v = os.environ["APP_AUTO_OPEN"].strip().lower()
        cfg["auto_open_browser"] = v not in ("0", "false", "no", "off", "")
    return cfg


CONFIG = load_config()


def _resolve(p):
    p = Path(p).expanduser()
    return p if p.is_absolute() else (PROJECT_DIR / p).resolve()


CORPUS_DIR = _resolve(CONFIG["corpus_dir"])

# 所有「生成物」统一放在 storage/ 下，根目录只留代码和配置
STORAGE_DIR = _resolve(os.environ.get("APP_STORAGE_DIR") or "storage")
INDEX_DIR = STORAGE_DIR / "index"
DB_PATH = INDEX_DIR / "corpus.sqlite"
META_DIR = STORAGE_DIR / "meta"
LOG_DIR = STORAGE_DIR / "logs"
RUN_DIR = STORAGE_DIR / "run"

HOST = str(CONFIG.get("host", DEFAULTS["host"]))
PORT = int(CONFIG.get("port", DEFAULTS["port"]))
AUTO_OPEN_BROWSER = bool(CONFIG.get("auto_open_browser", True))

# 语料归档清单（全部由配置决定，代码里不写死任何具体文件名）
_split_name = str(CONFIG.get("split_archive") or DEFAULTS["split_archive"])
_split_parts = int(CONFIG.get("split_parts") or DEFAULTS["split_parts"])
ARCHIVE_PARTS = [CORPUS_DIR / f"{_split_name}.{i:03d}" for i in range(1, _split_parts + 1)]
ARCHIVE_EXTRA = [CORPUS_DIR / str(n) for n in (CONFIG.get("extra_archives") or [])]


def missing_corpus_files():
    """返回缺失的语料文件列表（空列表 = 齐全）。"""
    return [str(p) for p in (ARCHIVE_PARTS + ARCHIVE_EXTRA) if not p.exists()]


def corpus_ready():
    return not missing_corpus_files()
