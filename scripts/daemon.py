#!/usr/bin/env python3
"""一键启动 / 停止的后台服务管理。

只管一件事：把 web 服务干净地拉起来、干净地关掉。

  start    起服务（后台，独立进程组），等健康检查通过再返回
  stop     停掉服务及其全部相关进程
  status   看看现在是什么状态
  restart  先 stop 再 start

用进程组（setsid）管理，所以 stop 时是整组一起停，不会有孤儿进程。
PID/状态记录在 run/server.pid，日志在 logs/server.log。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    CORPUS_DIR,
    DB_PATH,
    HOST,
    LOG_DIR,
    PORT,
    PROJECT_DIR,
    RUN_DIR,
    corpus_ready,
    missing_corpus_files,
)

SCRIPTS_DIR = PROJECT_DIR / "scripts"
SERVER = SCRIPTS_DIR / "searcher.py"
PID_FILE = RUN_DIR / "server.pid"
LOG_FILE = LOG_DIR / "server.log"

# stop 时要扫掉的进程模式。
#
# 必须用「本工程的绝对路径」而不是裸脚本名 —— 裸名会误伤：
# 机器上如果同时存在本工程的第二份拷贝（比如一个 clone），
# 在 A 里跑 stop.sh 会把 B 的服务一起杀掉。这是实测踩到的坑。
#
# 同时要给出两种写法：macOS 上 /tmp 是 /private/tmp 的软链接，
# PROJECT_DIR 会解析成 /private/tmp/...，而进程命令行里可能是 /tmp/...，
# 只认一种就会漏掉。
#
# 覆盖范围：start.sh → daemon 与 run.sh 都用绝对路径启动，都能扫到。
# 唯一漏网的是「手动 cd 进来再敲相对路径启动」的进程 —— 漏掉远比误杀好。
# 故意不含 daemon.py，免得把本次 stop 自己杀了。
_SWEEP_NAMES = ("searcher.py", "indexer.py")


def _sweep_patterns():
    bases = [str(SCRIPTS_DIR)]
    # 再补「按调用时目录原样」的写法。注意 os.getcwd() 也会解析软链接，
    # 所以要优先用 shell 维护的逻辑路径 $PWD。
    for cand in (os.environ.get("PWD"),):
        if cand:
            bases.append(str(Path(cand) / "scripts"))
    try:
        bases.append(str(Path(os.getcwd()) / "scripts"))
    except OSError:
        pass
    out = []
    for base in bases:
        for name in _SWEEP_NAMES:
            p = f"{base}/{name}"
            if p not in out:
                out.append(p)
    return out


SWEEP_PATTERNS = _sweep_patterns()


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state():
    if not PID_FILE.exists():
        return None
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def write_state(pid, pgid, host, port):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps({
        "pid": pid,
        "pgid": pgid,
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}/",
        "started": int(time.time()),
        "project": str(PROJECT_DIR),
        "python": sys.executable,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_state():
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def port_busy(host, port) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0


def health(host, port, timeout=2.0) -> bool:
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def tail(path, n=25):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def list_project_procs():
    """尽力列出本工程的进程；系统不让用 ps 时返回 None。"""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,pgid=,command="],
                             capture_output=True, timeout=10)
        if out.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    me = os.getpid()
    hits = []
    for line in out.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, pgid, cmd = parts
        if not pid.isdigit() or int(pid) == me:
            continue
        if any(pat in cmd for pat in SWEEP_PATTERNS) and "daemon.py" not in cmd:
            hits.append((int(pid), int(pgid), cmd[:110]))
    return hits


# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------
def cmd_start(args):
    host = args.host
    port = args.port

    st = read_state()
    if st and alive(st.get("pid", 0)):
        if health(st.get("host", host), st.get("port", port)):
            print(f"服务已经在运行 (pid {st['pid']})  {st.get('url')}")
            print("想重启就执行: ./stop.sh && ./start.sh")
            return 0
        print(f"发现残留进程 pid {st['pid']} 但端口没响应，先清理…")
        cmd_stop(args)

    if port_busy(host, port):
        print(f"❌ 端口 {host}:{port} 已被别的进程占用。")
        print(f"   改端口: 编辑 config.json，或执行 APP_PORT=9000 ./start.sh")
        return 1

    if not corpus_ready():
        print("❌ 语料文件缺失：")
        for p in missing_corpus_files():
            print("   ", p)
        print(f"   语料目录（可在 config.json 里改）: {CORPUS_DIR}")
        return 1

    if not DB_PATH.exists():
        print(f"❌ 还没有索引: {DB_PATH}")
        print("   先跑一次 ./start.sh --build-index，或手动：")
        print("     ./run.sh index build-meta && ./run.sh index scan-inner && ./run.sh match build-subs")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_FILE, "ab", buffering=0)
    logf.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())

    cmd = [sys.executable, str(SERVER), "serve", "--host", host, "--port", str(port),
           "--no-open"]
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_DIR), stdout=logf, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,   # 独立进程组
    )

    pgid = proc.pid
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pass
    write_state(proc.pid, pgid, host, port)

    print(f"启动中… pid={proc.pid} pgid={pgid}  日志: {LOG_FILE}")
    for i in range(40):                       # 最多等 20 秒
        if proc.poll() is not None:
            print(f"❌ 进程退出了（code={proc.returncode}）。日志末尾：")
            print(tail(LOG_FILE))
            clear_state()
            return 1
        if health(host, port):
            print(f"✅ 服务已就绪: http://{host}:{port}/")
            print(f"   停止: ./stop.sh      查看日志: tail -f {LOG_FILE}")
            return 0
        time.sleep(0.5)

    print("❌ 等待超时，服务没有响应。日志末尾：")
    print(tail(LOG_FILE))
    print("   pid 文件已保留，可用 ./stop.sh 收拾。")
    return 1


# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------
def _kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        print(f"   ⚠️ 没权限结束进程组 {pgid}")
        return False


def cmd_stop(args):
    stopped_any = False
    st = read_state()

    if st:
        pid, pgid = st.get("pid"), st.get("pgid") or st.get("pid")
        if alive(pid):
            print(f"结束主进程 pid={pid} pgid={pgid} …")
            _kill_group(pgid, signal.SIGTERM)
            stopped_any = True
            for _ in range(20):               # 最多等 10 秒
                if not alive(pid):
                    break
                time.sleep(0.5)
            if alive(pid):
                print("   还在，强制结束 (SIGKILL)")
                _kill_group(pgid, signal.SIGKILL)
                time.sleep(0.5)
            print(f"   主进程 {'已停止' if not alive(pid) else '仍未停止'}")
        else:
            print(f"pid 文件里的进程 {pid} 已经不在了（残留状态，清理掉）")
        clear_state()
    else:
        print("没有 pid 文件。")

    # 扫掉本工程残留的其它进程（比如你在另一个终端跑着的批量扫描）
    procs = list_project_procs()
    if procs:
        print(f"发现 {len(procs)} 个本工程的残留进程：")
        for pid, pgid, cmd in procs:
            print(f"   pid={pid:<7} {cmd}")
        for pid, pgid, _cmd in procs:
            _kill_group(pgid if pgid else pid, signal.SIGTERM)
        time.sleep(1.0)
        for pid, pgid, _cmd in procs:
            if alive(pid):
                _kill_group(pgid if pgid else pid, signal.SIGKILL)
        stopped_any = True

    # 系统不给用 ps 时的兜底：pkill 按命令行匹配
    for pat in SWEEP_PATTERNS:
        try:
            r = subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=10)
            if r.returncode == 0:
                stopped_any = True
        except (OSError, subprocess.SubprocessError):
            pass

    # 端口应该已经放开
    host = (st or {}).get("host", HOST)
    port = (st or {}).get("port", PORT)
    time.sleep(0.4)
    if port_busy(host, port):
        print(f"⚠️ 端口 {host}:{port} 仍然被占用，可能有不是本工程启动的进程。")
        print(f"   用 lsof -i :{port} 看看是谁。")
    else:
        print(f"端口 {host}:{port} 已释放。")

    print("✅ 已停止。" if stopped_any else "✅ 没有需要停止的进程。")
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def cmd_status(args):
    st = read_state()
    if not st:
        print("状态: 未运行（没有 pid 文件）")
        procs = list_project_procs()
        if procs:
            print(f"但有 {len(procs)} 个本工程进程在跑：")
            for pid, pgid, cmd in procs:
                print(f"   pid={pid:<7} {cmd}")
            print("可用 ./stop.sh 清理。")
        return 1

    pid = st.get("pid")
    running = alive(pid)
    host, port = st.get("host", HOST), st.get("port", PORT)
    ok = health(host, port) if running else False
    started = st.get("started")
    uptime = ""
    if started:
        secs = int(time.time()) - int(started)
        uptime = f"  已运行 {secs // 3600}h{secs % 3600 // 60}m{secs % 60}s"

    if running and ok:
        print(f"状态: ✅ 运行中  {st.get('url')}")
        print(f"   pid={pid} pgid={st.get('pgid')}{uptime}")
        return 0
    if running and not ok:
        print(f"状态: ⚠️ 进程在 (pid={pid}) 但 HTTP 无响应{uptime}")
        print(f"   看日志: tail -f {LOG_FILE}")
        return 2
    print(f"状态: ❌ 未运行（pid {pid} 已不存在，pid 文件是残留）")
    print("   执行 ./stop.sh 清理，然后 ./start.sh")
    return 1


def cmd_restart(args):
    cmd_stop(args)
    time.sleep(0.5)
    return cmd_start(args)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="本地语料检索服务 启停管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("start", cmd_start), ("stop", cmd_stop),
                     ("status", cmd_status), ("restart", cmd_restart)):
        p = sub.add_parser(name)
        p.add_argument("--host", default=HOST)
        p.add_argument("--port", type=int, default=PORT)
        p.set_defaults(func=fn)

    a = ap.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
