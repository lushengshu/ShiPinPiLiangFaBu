#!/usr/bin/env python3
"""一键启动本地视频发布工具。

这个启动器专门处理 Finder 双击启动的场景：

* 已有服务时直接复用，不重复启动第二个服务器；
* 服务未启动时在后台启动 app.py，并等待接口真正可用；
* 启动成功后自动打开本地页面；
* 从 App 启动时保持常驻：macOS 会回收 App 启动的进程，App 退出后服务会被
  一并清掉，页面就会报 "Failed to fetch"；
* 启动失败时把诊断写入 .douyin_batch/server.log，并输出可读提示。

启动器不会接触抖音账号，也不会触发发布流程。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# WorkBuddy injects a Node.js shim via NODE_OPTIONS that breaks Playwright
# when the process runs outside the WorkBuddy sandbox.
for _env_key in ("NODE_OPTIONS", "NODE_PATH"):
    os.environ.pop(_env_key, None)


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".douyin_batch"
PID_FILE = STATE_DIR / "server.pid"
LOG_FILE = STATE_DIR / "server.log"
DEFAULT_PORT = 8765
STARTUP_TIMEOUT_SECONDS = 45.0


def configured_port() -> int:
    """读取环境变量端口；填写错误时回退到默认端口并由主流程校验。"""
    try:
        return int(os.environ.get("DOUYIN_PORT", str(DEFAULT_PORT)))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def local_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def probe_service(port: int) -> tuple[bool, str]:
    """检查目标端口是否是本工具，而不只是任意 HTTP 服务。"""
    url = f"http://127.0.0.1:{port}/api/state"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, f"端口 {port} 已被其他网页服务占用"
        return False, f"本地服务返回 HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False, "服务尚未响应"

    if isinstance(payload, dict) and isinstance(payload.get("items"), list) and isinstance(payload.get("publish"), dict):
        return True, "本地服务已在运行"
    return False, f"端口 {port} 已被其他程序占用"


def choose_python() -> str:
    bundled = ROOT / ".venv" / "bin" / "python"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return sys.executable or "python3"


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_pid() -> int | None:
    try:
        value = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def write_pid(pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PID_FILE.with_suffix(".tmp")
    temporary.write_text(str(pid), encoding="utf-8")
    temporary.replace(PID_FILE)


def clear_stale_pid() -> None:
    pid = read_pid()
    if pid is not None and not pid_is_alive(pid):
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def start_server(port: int) -> subprocess.Popen[bytes]:
    """启动本地服务子进程，并把 PID 写入状态目录。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    python_bin = choose_python()
    command = [python_bin, str(ROOT / "app.py"), "--no-browser", "--port", str(port)]
    log = LOG_FILE.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    finally:
        # 子进程已经继承了文件描述符，启动器不再持有它。
        log.close()
    write_pid(process.pid)
    return process


def wait_until_ready(
    port: int,
    process: subprocess.Popen[bytes],
    timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_reason = "服务尚未响应"
    while time.monotonic() < deadline:
        ready, reason = probe_service(port)
        if ready:
            return True, reason
        last_reason = reason
        if process.poll() is not None:
            return False, f"服务进程已退出（退出码 {process.returncode}）"
        time.sleep(0.25)
    return False, last_reason


def supervise_server(process: subprocess.Popen[bytes]) -> int:
    """App 常驻托管服务进程，App 退出时一并停止服务。

    macOS 会在 ``视频发布工具.app`` 退出时回收它启动的所有进程。如果启动器
    打开页面后立刻退出，服务会被系统一起清掉，浏览器下一次请求就会报
    "Failed to fetch"。因此 App 必须保持常驻；用户退出 App（Dock 右键退出
    或活动监视器）时服务随之停止，不会留下孤儿进程。
    """
    stopping = threading.Event()

    def _request_stop(_signum, _frame):
        stopping.set()
        if process.poll() is None:
            process.terminate()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, _request_stop)
        except (ValueError, OSError):
            pass

    print("服务已交给当前 App 托管；退出 App 即停止服务。", flush=True)
    while not stopping.is_set():
        code = process.poll()
        if code is not None:
            return code
        time.sleep(0.2)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return 0


def tail_log(lines: int = 16) -> str:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "（暂时没有日志）"
    return "\n".join(content[-lines:]) or "（暂时没有日志）"


def open_local_page(url: str) -> bool:
    """优先使用 macOS 的 open，兼容从 Finder/访达双击启动。"""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return bool(webbrowser.open(url, new=0, autoraise=True))
    except webbrowser.Error:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地视频发布工具")
    parser.add_argument("--port", type=int, default=configured_port())
    parser.add_argument("--no-open", action="store_true", help="只启动服务，不自动打开浏览器")
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="本次启动由 App 常驻托管服务，App 退出时一并停止服务",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        print(f"端口无效：{args.port}", file=sys.stderr)
        return 2

    url = local_url(args.port)
    clear_stale_pid()
    ready, reason = probe_service(args.port)
    if not ready and reason.startswith("端口") and "其他" in reason:
        print(f"无法启动：{reason}", file=sys.stderr)
        print(f"请关闭占用端口的程序，或用 DOUYIN_PORT=其他端口 重试。", file=sys.stderr)
        return 1

    process: subprocess.Popen[bytes] | None = None
    if not ready:
        print(f"正在启动本地服务（最多等待 {int(STARTUP_TIMEOUT_SECONDS)} 秒）...", flush=True)
        try:
            process = start_server(args.port)
        except OSError as exc:
            print(f"无法启动本地服务：{exc}", file=sys.stderr)
            print(f"详细日志：{LOG_FILE}", file=sys.stderr)
            return 1
        ready, reason = wait_until_ready(args.port, process)
        if not ready:
            print(f"本地工具启动失败：{reason}", file=sys.stderr)
            print(f"详细日志：{LOG_FILE}", file=sys.stderr)
            print(tail_log(), file=sys.stderr)
            if process.poll() is None:
                process.terminate()
            return 1

    print(f"视频发布工具已就绪：{url}")
    if process is not None:
        print(f"服务进程 PID：{process.pid}（日志：{LOG_FILE}）")
    else:
        existing_pid = read_pid()
        if existing_pid is not None and pid_is_alive(existing_pid):
            print(f"已复用现有服务进程 PID：{existing_pid}")
    if not args.no_open:
        if open_local_page(url):
            print("已自动打开本地工具页面。")
        else:
            print(f"浏览器未能自动打开，请手动访问：{url}", file=sys.stderr)
            return 1
    if args.supervise and process is not None:
        return supervise_server(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
