#!/usr/bin/env bash
# ============================================================================
#  一键停止 —— 停掉服务以及所有相关进程
#
#    ./stop.sh           停止服务 + 清理本工程残留进程
#    ./stop.sh --status  只看状态，不停
#    ./stop.sh --restart 重启（等价于 ./start.sh 前先停）
#
#  停止范围：
#    1. run/server.pid 记录的进程组（SETSID 独立进程组，整组一起停）
#    2. 本工程 scripts/ 下所有还在跑的进程（比如你另开的批量扫描）
#    3. 兜底再 pkill 一遍
# ============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

VENV_PY="$PROJECT_DIR/.venv/bin/python"
export PIP_REQUIRE_VIRTUALENV=true
export PYTHONNOUSERSITE=1

hr() { printf '%s\n' "------------------------------------------------------------"; }

if [ ! -x "$VENV_PY" ]; then
  echo "  ⚠️ 没有虚拟环境（$VENV_PY）。先跑 ./start.sh 建立环境。"
  echo "     如果只是想清理进程，可以直接用 pkill："
  echo "     pkill -f '$PROJECT_DIR/scripts/'"
  exit 1
fi

case "${1:-}" in
  --status|-s) ACTION="status" ;;
  --restart|-r) ACTION="restart" ;;
  "") ACTION="stop" ;;
  -h|--help) sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "未知参数: $1"; exit 2 ;;
esac

hr
case "$ACTION" in
  status)
    echo "  本地语料检索 —— 状态"
    hr
    "$VENV_PY" scripts/daemon.py status
    ;;
  stop)
    echo "  本地语料检索 —— 一键停止"
    hr
    "$VENV_PY" scripts/daemon.py stop
    ;;
  restart)
    echo "  本地语料检索 —— 重启"
    hr
    "$VENV_PY" scripts/daemon.py restart
    ;;
esac
RC=$?
hr
exit "$RC"
