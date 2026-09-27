#!/usr/bin/env python3
"""Loopback-only HTTP server and one-shot scheduler for CodexQuotaTool."""

import argparse
import fcntl
import json
import mimetypes
import os
import secrets
import signal
import threading
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from accounts import AccountManager
from core import AppError, _atomic_json

ROOT = Path(__file__).resolve().parent
MAX_BODY = 1024 * 1024
REQUEST_TIMEOUT = 10
MAX_JSON_DEPTH = 32


def valid_id(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def validate_json_depth(value, depth=0):
    if depth > MAX_JSON_DEPTH:
        raise ValueError("JSON nesting limit exceeded")
    children = value.values() if isinstance(value, dict) else value
    if isinstance(value, (dict, list)):
        for child in children:
            validate_json_depth(child, depth + 1)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, address, engine, web_root, stop_event=None, demo=False):
        self.engine = engine
        self.demo = demo
        self.web_root = Path(web_root)
        self.csrf = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(32)
        self.stop_event = stop_event or threading.Event()
        super().__init__(address, Handler)
        port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(REQUEST_TIMEOUT)
        return connection, address

    def public_state(self, profile_id=None):
        state = dict(
            self.engine.state(profile_id)
            if isinstance(self.engine, AccountManager)
            else self.engine.state()
        )
        state["csrf"] = self.csrf
        state["demo"] = self.demo
        return state

    def request_shutdown(self):
        self.stop_event.set()
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexQuotaTool/1"

    def log_message(self, *_args):
        pass

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        body = (
            json.dumps(payload, ensure_ascii=False).encode()
            if isinstance(payload, dict)
            else payload
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'",
        )
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, message, status=400):
        self._send(status, {"ok": False, "error": message})

    def _check_host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            self._error("仅接受本机页面的请求。", 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._error("不允许跨站请求。", 403)
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error("不允许跨站请求。", 403)
            return False
        return True

    def do_GET(self):
        if not self._check_host():
            return
        path = self.path.partition("?")[0]
        if path == "/api/health":
            self._send(
                200,
                {
                    "ok": True,
                    "app": "CodexQuotaTool",
                    "pid": os.getpid(),
                    "demo": self.server.demo,
                    "instance_id": self.server.instance_id,
                },
            )
            return
        if path == "/api/state":
            try:
                query = parse_qs(self.path.partition("?")[2], max_num_fields=4)
                ids = query.get("profile_id", [])
                if len(ids) > 1:
                    raise AppError("账号查询参数重复。")
                self._send(
                    200,
                    {
                        "ok": True,
                        "state": self.server.public_state(ids[0] if ids else None),
                    },
                )
            except AppError as error:
                self._error(str(error), error.status)
            except ValueError:
                self._error("无效的查询参数。")
            except Exception:
                self._error("读取状态失败，请查看本地状态文件或重新启动。", 500)
            return
        files = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/accounts.js": "accounts.js",
            "/motion.js": "motion.js",
            "/theme.js": "theme.js",
            "/style.css": "style.css",
            "/fonts/Geist-Variable.woff2": "fonts/Geist-Variable.woff2",
        }
        filename = files.get(path)
        if not filename:
            self._error("页面不存在。", 404)
            return
        try:
            body = (self.server.web_root / filename).read_bytes()
        except OSError:
            self._error("页面文件缺失。", 404)
            return
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        content_type = mime if mime.startswith("font/") else mime + "; charset=utf-8"
        self._send(200, body, content_type)

    def do_POST(self):
        if not self._check_host():
            return
        if self.server.stop_event.is_set():
            self._error("服务正在关闭，请等待关闭完成后重新启动。", 503)
            return
        token = self.headers.get("X-Local-Token", "")
        if not token.isascii() or not secrets.compare_digest(token, self.server.csrf):
            self._error("页面会话已失效，请刷新页面后再操作。", 403)
            return
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._error("请求必须使用 JSON。", 415)
            return
        if self.headers.get("Transfer-Encoding"):
            self._error("不支持该传输格式。", 400)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._error("请求必须包含唯一的 Content-Length。")
            return
        text_length = lengths[0].strip()
        if not text_length.isascii() or not text_length.isdecimal():
            self._error("无效请求长度。")
            return
        try:
            length = int(text_length)
        except ValueError:
            self._error("无效请求长度。")
            return
        if length > MAX_BODY:
            self._error("文件过大，请导入小于 1 MB 的 auth.json。", 413)
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request body")
            body = json.loads(raw or b"{}", parse_constant=reject_constant)
            if not isinstance(body, dict):
                raise ValueError()
            validate_json_depth(body)
        except (ValueError, RecursionError, TimeoutError, OSError):
            self._error("无效的 JSON 请求。")
            return
        path = self.path.partition("?")[0]
        engine = self.server.engine
        manager = engine if isinstance(engine, AccountManager) else None
        view_id = body.get("view_profile_id")
        extra = {}
        try:
            if (
                path in {"/api/auth", "/api/auth/reload", "/api/accounts/remove"}
                and self.server.demo
            ):
                raise AppError(
                    "离线演示模式使用固定模拟凭证，不能导入、读取或移除真实账号凭证。",
                    403,
                )
            if path == "/api/auth":
                document = body.get("auth")
                if not isinstance(document, dict):
                    raise AppError("请选择有效的 auth.json 文件。")
                imported = engine.import_auth(document)
                if manager:
                    view_id = imported
            elif path == "/api/auth/reload":
                imported = engine.reload_auth()
                if manager:
                    view_id = imported
            elif manager and path == "/api/accounts/select":
                view_id = body.get("profile_id")
                manager.select(view_id)
            elif manager and path == "/api/accounts/rename":
                manager.rename(body.get("profile_id"), body.get("name"))
            elif manager and path == "/api/accounts/remove":
                manager.remove(body.get("profile_id"))
            elif manager and path == "/api/refresh/all":
                extra["refresh_summary"] = manager.refresh_all()
            elif path == "/api/shutdown":
                self._send(200, {"ok": True})
                self.server.request_shutdown()
                return
            elif path in {
                "/api/refresh",
                "/api/consume",
                "/api/schedule",
                "/api/schedule/cancel",
                "/api/retry",
                "/api/reconcile",
            }:
                context = (
                    manager.profile(body.get("profile_id"))
                    if manager
                    else nullcontext(engine)
                )
                with context as target:
                    self._account_action(target, path, body)
            else:
                raise AppError("接口不存在。", 404)
            self._send(
                200, {"ok": True, "state": self.server.public_state(view_id), **extra}
            )
        except AppError as exc:
            self._error(str(exc), exc.status)
        except Exception:
            self._error("操作未能完成，请先刷新并核实记录，避免重复消耗。", 500)

    @staticmethod
    def _account_action(engine, path, body):
        if path == "/api/refresh":
            engine.refresh()
        elif path in {"/api/consume", "/api/schedule"}:
            credit_id = body.get("credit_id")
            if not valid_id(credit_id, 512):
                raise AppError("请选择一个有效的重置机会。")
            if path == "/api/consume":
                engine.consume(credit_id)
            else:
                run_at = body.get("run_at")
                if not valid_id(run_at, 100):
                    raise AppError("请选择有效的预约时间。")
                engine.schedule(credit_id, run_at)
        elif path == "/api/schedule/cancel":
            schedule_id = body.get("schedule_id")
            if not valid_id(schedule_id, 100):
                raise AppError("预约记录无效。")
            engine.cancel_schedule(schedule_id)
        else:
            operation_id = body.get("operation_id")
            if not valid_id(operation_id, 100):
                raise AppError("操作记录无效。")
            if path == "/api/retry":
                engine.retry(operation_id)
            else:
                engine.reconcile(operation_id)

    def do_OPTIONS(self):
        self._error("不允许跨站请求。", 403)


def scheduler_loop(engine, stop_event):
    while not stop_event.wait(1):
        try:
            engine.tick()
        except Exception:
            print(
                "Scheduler check failed; inspect the operation record in the UI.",
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    port = args.port if args.port is not None else (8785 if args.demo else 8765)
    if not 1024 <= port <= 65535:
        parser.error("port must be between 1024 and 65535")
    application_root = args.root.resolve()
    if args.demo:
        from demo import demo_root

        root = demo_root(application_root)
    else:
        root = application_root
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".server.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("CodexQuotaTool is already running in this folder.")
        if args.demo:
            from demo import build_demo_manager

            engine = build_demo_manager(application_root)
        else:
            engine = AccountManager(root)
        try:
            httpd = LocalServer(
                ("127.0.0.1", port),
                engine,
                ROOT / "web",
                demo=args.demo,
            )
        except OSError:
            variable = "CODEX_QUOTA_DEMO_PORT" if args.demo else "CODEX_QUOTA_PORT"
            raise SystemExit(
                f"Port {port} unavailable. Choose another port via {variable}."
            ) from None
        runtime = root / ".runtime.json"
        worker = threading.Thread(
            target=scheduler_loop,
            args=(engine, httpd.stop_event),
        )
        try:
            _atomic_json(
                runtime,
                {
                    "app": "CodexQuotaTool",
                    "root": str(root),
                    "port": port,
                    "pid": os.getpid(),
                    "demo": args.demo,
                    "instance_id": httpd.instance_id,
                },
            )
            worker.start()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: httpd.request_shutdown())
            print(f"CodexQuotaTool listening on http://127.0.0.1:{port}", flush=True)
            httpd.serve_forever(poll_interval=0.3)
        finally:
            httpd.stop_event.set()
            httpd.server_close()
            if worker.ident is not None:
                worker.join()
            if isinstance(engine, AccountManager):
                engine.close()
            runtime.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
