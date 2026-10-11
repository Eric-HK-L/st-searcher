#!/usr/bin/env bash
# ============================================================================
#  一键启动
#
#    ./start.sh                  检查环境 → 装依赖 → 起服务 → 健康检查
#    ./start.sh --build-index    索引不存在时先自动建索引（首次用这个）
#    ./start.sh --foreground     前台跑（Ctrl-C 停），方便看日志
#    APP_PORT=9000 ./start.sh  临时换端口
#
#  依赖策略：任何 pip 安装都只进本目录的 .venv，绝不碰全局环境。
# ============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

VENV="$PROJECT_DIR/.venv"
VENV_PY="$VENV/bin/python"
REQ="$PROJECT_DIR/requirements.txt"
STORAGE_DIR="$PROJECT_DIR/storage"
LOG_DIR="$STORAGE_DIR/logs"
mkdir -p "$LOG_DIR" "$STORAGE_DIR"

# ---- 硬性防呆：pip 只允许在虚拟环境里跑 ----------------------------------
export PIP_REQUIRE_VIRTUALENV=true   # 没在 venv 里时 pip 直接报错退出
export PIP_USER=false                # 禁止 --user（那是往全局用户目录装）
export PYTHONNOUSERSITE=1            # 让 python 不加载全局 site-packages
export PIP_DISABLE_PIP_VERSION_CHECK=1
unset PYTHONPATH 2>/dev/null || true

BUILD_INDEX=0
FOREGROUND=0
for a in "$@"; do
  case "$a" in
    --build-index)   BUILD_INDEX=1 ;;
    --foreground|-f) FOREGROUND=1 ;;
    -h|--help)       sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $a"; exit 2 ;;
  esac
done

hr()   { printf '%s\n' "------------------------------------------------------------"; }
step() { printf '\n▸ %s\n' "$1"; }
die()  { printf '  ❌ %s\n' "$1"; exit 1; }

hr
echo "  本地语料检索 —— 一键启动"
echo "  工程目录: $PROJECT_DIR"
hr

# ---------------------------------------------------------------------------
step "1/6 检查 Python"
# ---------------------------------------------------------------------------
PYBIN=""
for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PYBIN="$(command -v "$c")"; break
  fi
done
[ -n "$PYBIN" ] || die "找不到 Python 3.9+（macOS 可执行 xcode-select --install）"
echo "  ✅ $PYBIN  ($("$PYBIN" -V 2>&1))"

# ---------------------------------------------------------------------------
step "2/6 准备虚拟环境（依赖只装这里）"
# ---------------------------------------------------------------------------
if [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import sys' >/dev/null 2>&1; then
  echo "  ✅ 已存在: $VENV"
else
  echo "  创建 $VENV …"
  rm -rf "$VENV"
  "$PYBIN" -m venv "$VENV"
  echo "  ✅ 已创建"
fi

# 再确认一次「我们真的在 venv 里」，否则宁可报错也不要污染全局
if ! "$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)'; then
  die "虚拟环境异常：解释器不在 venv 内，已中止（不会往全局装任何东西）"
fi
echo "  ✅ 解释器: $("$VENV_PY" -c 'import sys; print(sys.executable)')"
echo "  ✅ 隔离路径: $("$VENV_PY" -c 'import sys; print(sys.prefix)')"

# ---------------------------------------------------------------------------
step "3/6 安装依赖（pip --require-virtualenv 已强制开启）"
# ---------------------------------------------------------------------------
if [ -f "$REQ" ]; then
  set +e
  "$VENV_PY" -m pip install --quiet --require-virtualenv -r "$REQ" 2>"$LOG_DIR/pip.err"
  PIP_RC=$?
  set -e
  if [ "$PIP_RC" = "0" ]; then
    echo "  ✅ requirements.txt 已满足"
  else
    echo "  ⚠️ 依赖安装失败（可能是离线）。项目标准库优先，可继续运行。"
    tail -5 "$LOG_DIR/pip.err" 2>/dev/null | sed 's/^/     /' || true
  fi
else
  echo "  ℹ️ 未发现 requirements.txt，跳过"
fi

# ---------------------------------------------------------------------------
step "4/6 检查运行环境"
# ---------------------------------------------------------------------------
set +e
"$VENV_PY" - <<'PY'
import importlib, sqlite3
ok = True
for m in ("sqlite3", "zipfile", "http.server", "concurrent.futures", "urllib.request"):
    try:
        importlib.import_module(m)
    except ImportError:
        print(f"  ❌ 标准库缺失: {m}"); ok = False
if ok:
    print(f"  ✅ 标准库齐全 (sqlite {sqlite3.sqlite_version})")
try:
    import charset_normalizer
    print(f"  ✅ charset-normalizer {charset_normalizer.__version__}（可选，已启用）")
except ImportError:
    print("  ℹ️ charset-normalizer 未安装（可选），编码检测走内置启发式")
raise SystemExit(0 if ok else 1)
PY
STDLIB_RC=$?
set -e
[ "$STDLIB_RC" = "0" ] || die "标准库检查未通过"

if command -v bsdtar >/dev/null 2>&1; then
  echo "  ✅ bsdtar: $(command -v bsdtar)   (用于解压 rar/7z)"
else
  echo "  ⚠️ 未找到 bsdtar —— rar/7z 无法解包；zip 与裸字幕不受影响"
  echo "     macOS 自带 /usr/bin/bsdtar；Linux: apt install libarchive-tools"
fi

# ---------------------------------------------------------------------------
step "5/6 检查语料、元数据与索引"
# ---------------------------------------------------------------------------
set +e
"$VENV_PY" - <<'PYEOF'
import sys
sys.path.insert(0, "scripts")
from config import CORPUS_DIR, DB_PATH, META_DIR, missing_corpus_files
print(f"  语料目录: {CORPUS_DIR}")
missing = missing_corpus_files()
if missing:
    print("  ❌ 缺失以下文件：")
    for p in missing:
        print("     ", p)
    raise SystemExit(1)
print("  ✅ 语料齐全 (data.zip.001~004 + data2.zip)")

need_meta = [f for f in ("records.tsv", "catalog.tsv")
             if not (META_DIR / f).exists()]
if need_meta:
    print(f"  ℹ️ 可选元数据未提供（{', '.join(need_meta)}）—— 不影响检索，只是结果里没有可读片名")
else:
    print(f"  ✅ 元数据就绪: {META_DIR}")

if DB_PATH.exists():
    print(f"  ✅ 索引就绪: {DB_PATH}  ({DB_PATH.stat().st_size / 1e6:.0f} MB)")
    raise SystemExit(0)
print(f"  ⚠️ 还没有索引: {DB_PATH}")
raise SystemExit(2)
PYEOF
DATA_RC=$?
set -e

if [ "$DATA_RC" = "1" ]; then
  die "语料不齐。请把压缩包放进 dataset/，或改 config.json 的 dataset_dir。"
fi

if [ "$DATA_RC" = "3" ]; then
  echo
  die "缺少元数据文件。请把 records.tsv 与 catalog.tsv 放进 $STORAGE_DIR/meta/ 后重试。"
  # 元数据是可选的「可读名字」层：放进去检索体验更好，不放也能建索引。
  # 本工具不附带、不下载任何第三方数据，请自行准备。
fi

if [ "$DATA_RC" = "2" ]; then
  if [ "$BUILD_INDEX" = "1" ]; then
    echo
    echo "  ▸ 构建索引（首次约 1 分钟）…"
    "$VENV_PY" scripts/indexer.py build-meta
    "$VENV_PY" scripts/indexer.py scan-inner --workers 12
    "$VENV_PY" scripts/searcher.py build-subs
  else
    echo
    die "索引不存在。首次运行请执行：  ./start.sh --build-index"
  fi
fi

# ---------------------------------------------------------------------------
step "6/6 启动服务"
# ---------------------------------------------------------------------------
if [ "$FOREGROUND" = "1" ]; then
  echo "  前台运行，Ctrl-C 停止"
  hr
  exec "$VENV_PY" scripts/searcher.py serve --no-open
fi

set +e
"$VENV_PY" scripts/daemon.py start
RC=$?
set -e

hr
if [ "$RC" = "0" ]; then
  URL="$("$VENV_PY" -c "
import sys; sys.path.insert(0,'scripts')
from config import HOST, PORT
print(f'http://{HOST}:{PORT}/')")"
  echo "  地址: $URL"
  echo "  停止: ./stop.sh          状态: ./run.sh status"
  echo "  日志: $LOG_DIR/server.log"
  if "$VENV_PY" -c "
import sys; sys.path.insert(0,'scripts')
from config import AUTO_OPEN_BROWSER
raise SystemExit(0 if AUTO_OPEN_BROWSER else 1)"; then
    command -v open >/dev/null 2>&1 && open "$URL" || true
  fi
fi
hr
exit "$RC"
