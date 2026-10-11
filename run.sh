#!/usr/bin/env bash
# 命令行入口。自动使用工程自带的 .venv，不依赖任何外部运行时。
#
#   ./run.sh status                          查看服务状态
#   ./run.sh match find "权力的游戏 S08E01"     给视频找字幕
#   ./run.sh match season "权力的游戏" -s 8     整季导出
#   ./run.sh match scan ~/Movies --apply      整库批量补字幕
#   ./run.sh index stats / verify / search / extract ...
#
# 启停服务请用 ./start.sh 和 ./stop.sh。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "找不到 python3。请先运行 ./start.sh 建立工作区环境。" >&2
  exit 1
fi

tool="${1:-}"
shift || true

case "$tool" in
  match)  exec "$PY" "$DIR/scripts/searcher.py" "$@" ;;
  test)   exec "$PY" "$DIR/tests/test_all.py" "$@" ;;
  index)  exec "$PY" "$DIR/scripts/indexer.py" "$@" ;;
  daemon) exec "$PY" "$DIR/scripts/daemon.py" "$@" ;;
  status) exec "$PY" "$DIR/scripts/daemon.py" status ;;
  ""|-h|--help|help)
    cat <<EOF
用法:
  ./start.sh              一键启动（含依赖安装与环境检查）
  ./stop.sh               一键停止（含所有相关进程）
  ./run.sh status         查看状态
  ./run.sh match <find|season|scan|serve|build-subs> [参数...]
  ./run.sh index <build-meta|scan-inner|verify|search|extract|stats> [参数...]

解释器: $PY
EOF
    ;;
  *) echo "未知工具: $tool（可用: match / index / test / status）" >&2; exit 2 ;;
esac
