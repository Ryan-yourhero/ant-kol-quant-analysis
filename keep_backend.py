"""守护进程：确保 backend 8000 端口始终存活。

- 启动 uvicorn
- 每 5 秒检测端口；挂了立刻拉起
- 不依赖 PowerShell 进程组
"""
import subprocess
import sys
import time
import socket
import os
import signal

WORKDIR = r"E:\PM\PM\KOL-RICH"
LOG_OUT = r"E:\PM\PM\KOL-RICH\output\backend.out"
LOG_ERR = r"E:\PM\PM\KOL-RICH\output\backend.err"
PORT = 8000


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def start_backend():
    out = open(LOG_OUT, "ab", buffering=0)
    err = open(LOG_ERR, "ab", buffering=0)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app",
         "--host", "0.0.0.0", "--port", str(PORT), "--no-access-log"],
        cwd=WORKDIR,
        stdout=out,
        stderr=err,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def main():
    os.makedirs(os.path.dirname(LOG_OUT), exist_ok=True)
    proc = start_backend()
    print(f"backend started PID={proc.pid}")
    while True:
        time.sleep(5)
        if proc.poll() is not None:
            print(f"backend died, restart...")
            proc = start_backend()
            print(f"backend restarted PID={proc.pid}")
            continue
        if not port_open(PORT):
            print(f"backend port {PORT} not responding, killing & restart")
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                pass
            proc = start_backend()
            print(f"backend restarted PID={proc.pid}")


if __name__ == "__main__":
    main()