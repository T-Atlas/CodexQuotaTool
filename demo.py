"""Durable offline upstream simulation using the same Engine and web interface."""

import copy
import json
import threading
import time
from pathlib import Path

from core import AppError, Engine, _atomic_json, _iso, _timestamp

DEMO_AUTH = {
    "access_token": "offline-demo-token-not-a-credential",
    "account_id": "offline-demo-account",
    "label": "离线演示账号（模拟数据）",
}


def demo_root(application_root):
    root = Path(application_root).resolve()
    # Isolation must not be redirected onto production state by symbolic links.
    for candidate in (root / "data", root / "data" / "demo"):
        if candidate.is_symlink():
            raise AppError(
                "演示数据目录不能是符号链接，请使用独立的 data/demo 目录。", 500
            )
    return root / "data" / "demo"


class DemoTransport:
    """Simulate only known endpoints; persist credit status and redemption IDs."""

    def __init__(self, root, clock=time.time):
        self.path = Path(root) / "demo-upstream.json"
        self.clock = clock
        self.lock = threading.RLock()
        if self.path.is_symlink():
            raise AppError("演示状态不能是符号链接。", 500)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(self.data, dict)
                    or self.data.get("format") != "offline-demo-v1"
                    or not isinstance(self.data.get("credits"), list)
                    or not isinstance(self.data.get("redemptions"), dict)
                    or not isinstance(self.data.get("rate_limit"), dict)
                ):
                    raise ValueError("invalid demo state")
            except (OSError, ValueError, TypeError):
                raise AppError(
                    "演示状态无法读取，请保留 data/demo 并检查 demo-upstream.json。",
                    500,
                ) from None
        else:
            now = self.clock()
            self.data = {
                "format": "offline-demo-v1",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 96,
                        "limit_window_seconds": 18000,
                        "reset_at": now + 3600,
                    },
                    "secondary_window": {
                        "used_percent": 92,
                        "limit_window_seconds": 604800,
                        "reset_at": now + 86400,
                    },
                },
                "credits": [
                    {
                        "id": f"demo-credit-{number}",
                        "reset_type": "codex_rate_limits",
                        "status": "available",
                        "granted_at": _iso(now - 86400),
                        "expires_at": _iso(now + duration),
                    }
                    for number, duration in enumerate((7200, 172800), start=1)
                ],
                "redemptions": {},
            }
            self.save()

    def save(self):
        _atomic_json(self.path, self.data)

    def available(self):
        now = self.clock()
        return [
            credit
            for credit in self.data["credits"]
            if credit.get("status") == "available"
            and (_timestamp(credit.get("expires_at")) or 0) > now
        ]

    def __call__(self, method, path, headers, body=None):
        with self.lock:
            if headers.get("Authorization") != "Bearer " + DEMO_AUTH["access_token"]:
                return 401, {"error": {"code": "demo_only"}}
            if method == "GET" and path == "/usage":
                return 200, {
                    "rate_limit": copy.deepcopy(self.data["rate_limit"]),
                    "rate_limit_reset_credits": {
                        "available_count": len(self.available())
                    },
                }
            if method == "GET" and path == "/rate-limit-reset-credits":
                credits = copy.deepcopy(self.data["credits"])
                for credit in credits:
                    if (
                        credit["status"] == "available"
                        and (_timestamp(credit["expires_at"]) or 0) <= self.clock()
                    ):
                        credit["status"] = "expired"
                count = len(self.available())
                return 200, {
                    "available_count": count,
                    "applicable_available_count": count,
                    "credits": credits,
                }
            if method != "POST" or path != "/rate-limit-reset-credits/consume":
                return 400, {"error": {"code": "unsupported_offline_demo_endpoint"}}
            request_id = (
                body.get("redeem_request_id") if isinstance(body, dict) else None
            )
            credit_id = body.get("credit_id") if isinstance(body, dict) else None
            if not isinstance(request_id, str) or not request_id:
                return 400, {"error": {"code": "missing_request_id"}}
            previous = self.data["redemptions"].get(request_id)
            if previous:
                response = dict(previous)
                if response["code"] == "reset":
                    response["code"] = "already_redeemed"
                return 200, response
            credit = next(
                (entry for entry in self.available() if entry["id"] == credit_id), None
            )
            windows = list(self.data["rate_limit"].values())
            if credit is None:
                response = {"code": "no_credit", "windows_reset": 0}
            elif not any(window["used_percent"] >= 90 for window in windows):
                response = {"code": "nothing_to_reset", "windows_reset": 0}
            else:
                credit["status"] = "consumed"
                for window in windows:
                    window["used_percent"] = 0
                    window["reset_at"] = self.clock() + window["limit_window_seconds"]
                response = {"code": "reset", "windows_reset": len(windows)}
            self.data["redemptions"][request_id] = dict(response)
            # Commit the upstream outcome before returning it, just like a remote service.
            self.save()
            return 200, response


def build_demo_engine(application_root, clock=time.time):
    root = demo_root(application_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    transport = DemoTransport(root, clock=clock)
    # Replace the demo-only auth path before Engine can read it. Production auth is never opened.
    _atomic_json(root / "auth.json", DEMO_AUTH)
    engine = Engine(root, transport=transport, clock=clock)
    engine.refresh()
    return engine
