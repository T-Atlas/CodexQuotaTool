#!/usr/bin/env python3
"""Start, inspect, and stop this project's local service."""

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def runtime_root(demo=False):
    if demo:
        from demo import demo_root

        return demo_root(ROOT)
    return ROOT.resolve()


def runtime(demo=False):
    root = runtime_root(demo)
    try:
        value = json.loads((root / ".runtime.json").read_text())
        if not isinstance(value, dict):
            return None
        if type(value.get("port")) is not int:
            return None
        if not 1024 <= value["port"] <= 65535:
            return None
        if type(value.get("pid")) is not int or value["pid"] <= 0:
            return None
        if value.get("app") != "CodexQuotaTool":
            return None
        if value.get("root") != str(root):
            return None
        if value.get("demo") is not demo:
            return None
        instance_id = value.get("instance_id")
        if not isinstance(instance_id, str) or len(instance_id) != 64:
            return None
        if any(char not in "0123456789abcdef" for char in instance_id):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def port_variable(demo):
    return "CODEX_QUOTA_DEMO_PORT" if demo else "CODEX_QUOTA_PORT"


def choose_port(demo=False):
    variable = port_variable(demo)
    configured = os.environ.get(variable)
    if configured:
        try:
            port = int(configured)
        except ValueError:
            raise SystemExit(f"{variable} 必须为整数。") from None
        if not 1024 <= port <= 65535:
            raise SystemExit(f"{variable} 必须在 1024 至 65535 之间。")
        return str(port)
    first_port = 8785 if demo else 8765
    for port in range(first_port, first_port + 21):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return str(port)
    raise SystemExit(f"默认端口均被占用，请通过 {variable} 指定其他端口。")


def request(info, path, data=None, token=None, timeout=3):
    base = f"http://127.0.0.1:{info['port']}"
    headers = {"Origin": base}
    if token:
        headers["X-Local-Token"] = token
    raw = None
    if data is not None:
        raw = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=raw, headers=headers)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def is_running(info, demo=False):
    if not info:
        return False
    try:
        data = request(info, "/api/health")
        return (
            isinstance(data, dict)
            and data.get("app") == "CodexQuotaTool"
            and data.get("pid") == info["pid"]
            and data.get("demo") is demo
            and data.get("instance_id") == info["instance_id"]
        )
    except (OSError, ValueError, KeyError):
        return False


def service_locked(demo=False):
    try:
        with (runtime_root(demo) / ".server.lock").open("r+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Codex 额度管家本地启动器")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=("start", "stop", "restart", "status", "open", "run", "logs", "test"),
    )
    parser.add_argument("--no-open", action="store_true", help="不打开浏览器")
    parser.add_argument("--demo", action="store_true", help="操作离线演示服务")
    return parser.parse_args(argv)


def stop_service(info):
    state = request(info, "/api/state")["state"]
    request(info, "/api/shutdown", {}, state["csrf"])


def wait_stopped(demo=False):
    # The lock stays held until HTTP requests and the scheduler have finished.
    for _ in range(500):
        if not service_locked(demo):
            return
        time.sleep(0.1)
    raise SystemExit("服务仍在结束当前请求；请稍后检查状态，未启动第二个实例。")


def start_service(demo, port):
    root = runtime_root(demo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [sys.executable, str(ROOT / "server.py"), "--port", port]
    if demo:
        command.append("--demo")
    with (root / "server.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    suffix = " --demo" if demo else ""
    for _ in range(60):
        info = runtime(demo)
        if is_running(info, demo):
            return info
        if process.poll() is not None:
            raise SystemExit(f"启动失败，请执行 ./run.sh logs{suffix} 查看原因。")
        time.sleep(0.1)
    raise SystemExit(f"启动尚未完成，请执行 ./run.sh status{suffix} 检查。")


def main():
    args = parse_arguments()
    os.umask(0o077)
    if args.command == "test":
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        raise SystemExit(subprocess.call(command, cwd=ROOT))
    if args.command == "logs":
        try:
            with (runtime_root(args.demo) / "server.log").open() as log:
                print("".join(deque(log, maxlen=40)), end="")
        except FileNotFoundError:
            print("暂无日志。")
        return

    info = runtime(args.demo)
    running = is_running(info, args.demo)
    label = "离线演示服务" if args.demo else "服务"
    suffix = " --demo" if args.demo else ""
    if args.command == "status":
        if running:
            print(f"{label}运行中：http://127.0.0.1:{info['port']}")
        elif service_locked(args.demo):
            print(f"{label}进程仍持有锁，但健康检查未通过；请检查日志。")
        else:
            print(f"{label}未运行。")
        return
    if not running and service_locked(args.demo):
        raise SystemExit(
            f"{label}仍在启动、关闭或无响应；未启动其他实例或终止进程。"
            f"请执行 ./run.sh logs{suffix} 检查。"
        )
    if args.command in {"stop", "restart"}:
        if running:
            stop_service(info)
            wait_stopped(args.demo)
        if args.command == "stop":
            print(f"{label}已停止。" if running else f"{label}未运行。")
            return
        running = False
    if args.command == "open":
        if not running:
            raise SystemExit(f"请先执行 ./run.sh start{suffix}")
        webbrowser.open(f"http://127.0.0.1:{info['port']}")
        return
    if args.command == "run":
        if running:
            raise SystemExit(f"{label}已在运行，请先执行 ./run.sh stop{suffix}")
        command = [
            sys.executable,
            str(ROOT / "server.py"),
            "--port",
            choose_port(args.demo),
        ]
        if args.demo:
            command.append("--demo")
        os.execv(sys.executable, command)
    if not running:
        configured = os.environ.get(port_variable(args.demo))
        if args.command == "restart" and info and not configured:
            port = str(info["port"])
        else:
            port = choose_port(args.demo)
        info = start_service(args.demo, port)
    print(f"{label}已启动：http://127.0.0.1:{info['port']}")
    if args.demo:
        print("演示数据完全隔离；所有查询和兑换均为本机模拟。")
    else:
        print("后台运行；关闭网页不会停止预约。Mac 需保持开机、唤醒并联网。")
    if not args.no_open:
        webbrowser.open(f"http://127.0.0.1:{info['port']}")


if __name__ == "__main__":
    main()
