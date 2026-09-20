"""Private, single-account Codex quota queries and durable one-shot redemption."""

from __future__ import annotations

import base64
import copy
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _count(value):
    result = _number(value)
    return (
        int(result)
        if result is not None and result >= 0 and result.is_integer()
        else None
    )


def _timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = _number(value)
        if numeric is not None:
            result = numeric
            if result > 100_000_000_000:
                result /= 1000
        else:
            raw = str(value).strip()
            if not raw:
                return None
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                return None
            result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _iso(value):
    stamp = _timestamp(value)
    if stamp is None:
        return None
    try:
        return (
            datetime.fromtimestamp(stamp, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError):
        return None


def _claims(token):
    try:
        payload = str(token).split(".")[1]
        decoded = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        return decoded if isinstance(decoded, dict) else {}
    except (IndexError, ValueError, TypeError, UnicodeError):
        return {}


def _file_stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _atomic_json(path: Path, document, expected_stamp=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            if expected_stamp is not None:
                try:
                    unchanged = _file_stamp(path.lstat()) == expected_stamp
                except OSError:
                    unchanged = False
                if not unchanged:
                    raise AppError(
                        "auth.json 在保存期间发生变化，已保留外部凭证，请重新载入。",
                        409,
                    )
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return _file_stamp(os.fstat(handle.fileno()))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _normalize_auth(document):
    if not isinstance(document, dict):
        raise AppError("auth.json 必须是 JSON 对象。")
    tokens = document.get("tokens")
    if "tokens" in document and not isinstance(tokens, dict):
        raise AppError("auth.json 的 tokens 字段必须是 JSON 对象。")
    tokens = document if tokens is None else tokens
    access = tokens.get("access_token")
    id_token = tokens.get("id_token")
    if (
        not isinstance(access, str)
        or not access.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in access)
    ):
        raise AppError(
            "auth.json 缺少有效的 access_token；支持 Codex tokens 格式和平铺凭证格式。"
        )
    id_claims = _claims(id_token)
    access_claims = _claims(access)
    scopes = [
        claims.get("https://api.openai.com/auth")
        for claims in (id_claims, access_claims)
    ]
    claimed_ids = [
        scope.get("chatgpt_account_id")
        for scope in scopes
        if isinstance(scope, dict) and scope.get("chatgpt_account_id") is not None
    ]
    account_id = tokens.get("account_id") or next(iter(claimed_ids), None)
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in account_id)
    ):
        raise AppError(
            "auth.json 缺少账号 ID，且无法从 id_token 读取 chatgpt_account_id。"
        )
    supplied_ids = [
        source["account_id"] for source in (tokens, document) if "account_id" in source
    ]
    if any(value != account_id for value in claimed_ids + supplied_ids):
        raise AppError("凭证中的 account_id 与令牌账号不一致，已停止导入。")
    profile = id_claims.get("https://api.openai.com/profile") or {}
    profile = profile if isinstance(profile, dict) else {}
    label = (
        document.get("email")
        or id_claims.get("email")
        or profile.get("email")
        or document.get("label")
        or "已导入 Codex 账号"
    )
    expiry = (
        access_claims.get("exp") or tokens.get("expires_at") or document.get("expired")
    )
    refresh_token = tokens.get("refresh_token")
    if refresh_token is not None and (
        not isinstance(refresh_token, str)
        or any(ord(c) < 32 or ord(c) == 127 for c in refresh_token)
    ):
        raise AppError("auth.json 中的 refresh_token 格式无效。")
    return {
        "access_token": access.strip(),
        "refresh_token": refresh_token,
        "account_id": account_id.strip(),
        "label": str(label)[:160],
        "expires_at": _iso(expiry),
    }


def curl_transport(method, path, headers, body):
    """Return (HTTP status, decoded JSON); credentials travel only over stdin."""
    allowed = {
        "/usage",
        "/rate-limit-reset-credits",
        "/rate-limit-reset-credits/consume",
        "/oauth/token",
    }
    if path not in allowed or method not in {"GET", "POST"}:
        raise AppError("不支持的上游请求。")
    executable = shutil.which("curl")
    if not executable:
        raise AppError("未找到 curl，请安装 curl 后再运行。", 503)
    url = (
        "https://auth.openai.com/oauth/token"
        if path == "/oauth/token"
        else "https://chatgpt.com/backend-api/wham" + path
    )
    lines = [
        "silent",
        "show-error",
        "connect-timeout = 15",
        "max-time = 40",
        "max-redirs = 0",
        'proto = "=https"',
        f"request = {json.dumps(method)}",
        f"url = {json.dumps(url)}",
        'write-out = "\\n%{http_code}"',
    ]
    for key, value in headers.items():
        if any(c in str(key) + str(value) for c in "\r\n"):
            raise AppError("凭证包含非法请求头字符。")
        lines.append(f"header = {json.dumps(str(key) + ': ' + str(value))}")
    if body is not None:
        lines.append(f"data = {json.dumps(json.dumps(body, separators=(',', ':')))}")
    try:
        result = subprocess.run(
            [executable, "-q", "--config", "-"],
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise AppError(
            "上游连接未完成；若正在兑换，结果需要核对，不能另建兑换请求。", 502
        ) from None
    if result.returncode != 0:
        raise AppError("上游网络请求失败；请检查网络连接或代理配置。", 502)
    raw, _, raw_status = result.stdout.rpartition("\n")
    try:
        status = int(raw_status)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError):
        raise AppError(
            "上游返回了无法识别的响应；请检查网络或重新导入凭证。", 502
        ) from None
    return status, payload


class Engine:
    """Serializes mutations while allowing read-only state snapshots during I/O."""

    def __init__(self, root: Path, transport=None, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.transport = transport or curl_transport
        self.clock = clock
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._busy = False
        self._auth = None
        self._auth_document = None
        self._auth_error = None
        self._auth_stamp = None
        self._refresh_attempted = False
        self._data = {"usage": None, "credits": None, "schedules": [], "operations": []}
        state_file = self.root / "state.json"
        if state_file.exists():
            try:
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                self._validate_state(saved)
                self._data = saved
            except (OSError, ValueError, TypeError):
                raise AppError(
                    "state.json 无法读取。为避免重复兑换，请保留该文件并先修复状态。",
                    500,
                ) from None
        self._sync_auth()
        changed = False
        for operation in self._data["operations"]:
            if operation.get("status") == "pending":
                operation.update(
                    status="uncertain",
                    message="程序在兑换确认前退出。请核对状态或使用原请求 ID 重试。",
                )
                changed = True
        for scheduled in self._data["schedules"]:
            if scheduled.get("status") == "running":
                scheduled.update(
                    status="uncertain",
                    message="程序在预约兑换中退出；不会自动重复兑换，请核对关联请求。",
                )
                changed = True
        if changed:
            self._save()

    @staticmethod
    def _validate_state(saved):
        """Reject damaged records before they can authorize another redemption."""
        if not isinstance(saved, dict) or set(saved) != {
            "usage",
            "credits",
            "schedules",
            "operations",
        }:
            raise ValueError("invalid state fields")

        def text(value):
            return isinstance(value, str) and bool(value.strip())

        def timestamp(value):
            return isinstance(value, str) and _timestamp(value) is not None

        def identifier(value):
            try:
                return isinstance(value, str) and str(uuid.UUID(value)) == value
            except (ValueError, AttributeError):
                return False

        def count(value):
            return value is None or type(value) is int and value >= 0

        operations = saved["operations"]
        schedules = saved["schedules"]
        if not isinstance(operations, list) or not isinstance(schedules, list):
            raise ValueError("invalid records")
        operation_fields = {
            "id",
            "credit_id",
            "created_at",
            "status",
            "code",
            "message",
            "windows_reset",
            "verified",
            "_account_id",
        }
        operation_statuses = {
            "pending",
            "uncertain",
            "succeeded",
            "no_credit",
            "nothing_to_reset",
            "not_sent",
        }
        by_id = {}
        for item in operations:
            if (
                not isinstance(item, dict)
                or set(item) != operation_fields
                or not identifier(item["id"])
                or item["id"] in by_id
                or item["status"] not in operation_statuses
                or not all(
                    text(item[key]) for key in ("credit_id", "_account_id", "message")
                )
                or not timestamp(item["created_at"])
                or item["code"] is not None
                and not text(item["code"])
                or type(item["verified"]) is not bool
                or not count(item["windows_reset"])
            ):
                raise ValueError("invalid operation")
            by_id[item["id"]] = item

        schedule_fields = {
            "id",
            "credit_id",
            "run_at",
            "expires_at",
            "status",
            "message",
            "operation_id",
            "_account_id",
        }
        expected_operation = {
            "running": "pending",
            "uncertain": "uncertain",
            "completed": "succeeded",
        }
        schedule_ids = set()
        linked_operations = set()
        active_credits = set()
        for item in schedules:
            if (
                not isinstance(item, dict)
                or set(item) != schedule_fields
                or not identifier(item["id"])
                or item["id"] in schedule_ids
                or item["status"]
                not in {
                    "scheduled",
                    "running",
                    "uncertain",
                    "completed",
                    "cancelled",
                    "skipped",
                }
                or not all(
                    text(item[key]) for key in ("credit_id", "_account_id", "message")
                )
                or not timestamp(item["run_at"])
                or not timestamp(item["expires_at"])
                or _timestamp(item["run_at"]) >= _timestamp(item["expires_at"])
            ):
                raise ValueError("invalid schedule")
            schedule_ids.add(item["id"])
            operation_id = item["operation_id"]
            if operation_id is not None:
                if (
                    not identifier(operation_id)
                    or operation_id not in by_id
                    or operation_id in linked_operations
                ):
                    raise ValueError("invalid operation reference")
                operation = by_id[operation_id]
                if any(
                    operation[key] != item[key] for key in ("credit_id", "_account_id")
                ):
                    raise ValueError("operation account or credit mismatch")
                expected = expected_operation.get(item["status"])
                if expected is not None and operation["status"] != expected:
                    raise ValueError("operation status mismatch")
                if item["status"] == "skipped" and operation["status"] not in {
                    "no_credit",
                    "nothing_to_reset",
                    "not_sent",
                }:
                    raise ValueError("invalid skipped operation")
                if item["status"] in {"scheduled", "cancelled"}:
                    raise ValueError("unsubmitted schedule has operation")
                linked_operations.add(operation_id)
            elif item["status"] in expected_operation:
                raise ValueError("missing operation reference")
            if item["status"] in {"scheduled", "running", "uncertain"}:
                credit = (item["_account_id"], item["credit_id"])
                if credit in active_credits:
                    raise ValueError("duplicate active schedule")
                active_credits.add(credit)

        cache_fields = {"fetched_at", "error", "_account_id"}
        for kind in ("usage", "credits"):
            cache = saved[kind]
            if cache is None:
                continue
            required = cache_fields | (
                {"windows"}
                if kind == "usage"
                else {"items", "available_count", "applicable_available_count"}
            )
            if (
                not isinstance(cache, dict)
                or set(cache) != required
                or not text(cache["_account_id"])
                or cache["fetched_at"] is not None
                and not timestamp(cache["fetched_at"])
                or cache["error"] is not None
                and not isinstance(cache["error"], str)
            ):
                raise ValueError("invalid cached response")
            entries = cache["windows" if kind == "usage" else "items"]
            if not isinstance(entries, list):
                raise ValueError("invalid cached entries")
            if kind == "credits":
                if not count(cache["available_count"]) or not count(
                    cache["applicable_available_count"]
                ):
                    raise ValueError("invalid cached count")
                seen = set()
                for item in entries:
                    if (
                        not isinstance(item, dict)
                        or set(item)
                        != {
                            "id",
                            "status",
                            "expires_at",
                            "granted_at",
                            "_expiry_invalid",
                        }
                        or not text(item["id"])
                        or item["id"] in seen
                        or not text(item["status"])
                        or type(item["_expiry_invalid"]) is not bool
                        or any(
                            item[key] is not None and not timestamp(item[key])
                            for key in ("expires_at", "granted_at")
                        )
                    ):
                        raise ValueError("invalid cached credit")
                    seen.add(item["id"])
            else:
                for item in entries:
                    if (
                        not isinstance(item, dict)
                        or set(item)
                        != {"name", "used_percent", "reset_at", "window_seconds"}
                        or not text(item["name"])
                        or any(
                            item[key] is not None and _number(item[key]) is None
                            for key in ("used_percent", "window_seconds")
                        )
                        or item["reset_at"] is not None
                        and not timestamp(item["reset_at"])
                    ):
                        raise ValueError("invalid cached window")

    def _save(self):
        with self._lock:
            _atomic_json(self.root / "state.json", self._data)

    def _disk_auth_stamp(self):
        return _file_stamp((self.root / "auth.json").lstat())

    def _active_accounts(self):
        schedules = [
            item
            for item in self._data["schedules"]
            if item.get("status") in {"scheduled", "running", "uncertain"}
        ]
        operations = [
            item
            for item in self._data["operations"]
            if item.get("status") in {"pending", "uncertain"}
        ]
        return {item.get("_account_id") for item in schedules + operations}

    def _check_account_switch(self, account_id):
        if self._active_accounts() - {account_id}:
            raise AppError(
                "其他账号仍有预约或未确认的兑换，请恢复该账号凭证，取消预约或核对兑换后再更换账号。",
                409,
            )

    def _sync_auth(self, force=False):
        """Read only a bounded regular file; never perform upstream I/O here."""
        try:
            current_stamp = self._disk_auth_stamp()
            if not force and current_stamp == self._auth_stamp:
                self._auth_error = None
                return
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(self.root / "auth.json", flags), "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                    raise AppError("auth.json 必须是小于 1 MB 的普通文件。")
                raw = handle.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024 or _file_stamp(
                    os.fstat(handle.fileno())
                ) != _file_stamp(info):
                    raise AppError(
                        "auth.json 正在变化或超过 1 MB，请完成保存后重新载入。"
                    )
                document = json.loads(raw)
                normalized = _normalize_auth(document)
                self._check_account_switch(normalized["account_id"])
                os.fchmod(handle.fileno(), 0o600)
                stamp = _file_stamp(os.fstat(handle.fileno()))
            if self._disk_auth_stamp() != stamp:
                raise AppError("auth.json 在读取期间被替换，请重新载入。")
            self._auth_document = document
            self._auth = normalized
            self._auth_stamp = stamp
            self._auth_error = None
        except FileNotFoundError:
            self._auth_error = (
                "auth.json 已移走，请放回文件或重新导入。" if self._auth else None
            )
        except (OSError, ValueError, TypeError, AppError) as error:
            self._auth_error = (
                str(error)
                if isinstance(error, AppError)
                else "auth.json 无法读取，请重新导入。"
            )

    def _check_auth_file_unchanged(self):
        """A file replacement during I/O must not change credentials or be overwritten."""
        try:
            unchanged = self._disk_auth_stamp() == self._auth_stamp
        except OSError:
            unchanged = False
        if not unchanged:
            self._auth_error = (
                "auth.json 在操作期间发生变化，已停止后续请求；请重新载入凭证。"
            )
            raise AppError(self._auth_error, 409)

    @contextmanager
    def _action(self):
        if not self._action_lock.acquire(blocking=False):
            raise AppError("另一个操作正在进行，请稍后再试。", 409)
        with self._lock:
            self._busy = True
            self._refresh_attempted = False
        try:
            with self._lock:
                self._sync_auth()
            yield
        finally:
            with self._lock:
                self._busy = False
            self._action_lock.release()

    def _require_auth(self):
        if self._auth_error:
            raise AppError(self._auth_error, 409)
        if not self._auth:
            raise AppError(self._auth_error or "请先导入 auth.json。", 401)
        return self._auth

    def state(self):
        with self._lock:
            if not self._busy:
                self._sync_auth()
            account_id = self._auth["account_id"] if self._auth else None
            account = (
                {
                    "loaded": True,
                    "label": self._auth["label"],
                    "account_id": account_id,
                    "expires_at": self._auth["expires_at"],
                }
                if self._auth
                else {
                    "loaded": False,
                    "label": "未导入凭证",
                    "account_id": "",
                    "expires_at": None,
                }
            )
            if self._auth_error:
                account["error"] = self._auth_error

            def clean(value):
                if isinstance(value, dict):
                    return {
                        key: clean(item)
                        for key, item in value.items()
                        if not key.startswith("_")
                    }
                if isinstance(value, list):
                    return [clean(item) for item in value]
                return copy.deepcopy(value)

            def public(value):
                if not value or value.get("_account_id") != account_id:
                    return None
                return clean(value)

            schedules = [
                public(item)
                for item in self._data["schedules"]
                if item.get("_account_id") == account_id
            ]
            return {
                "account": account,
                "usage": public(self._data["usage"]),
                "credits": public(self._data["credits"]),
                "schedules": schedules,
                "operations": [
                    public(item)
                    for item in reversed(self._data["operations"])
                    if item.get("_account_id") == account_id
                ],
                "busy": self._busy,
            }

    def import_auth(self, document):
        normalized = _normalize_auth(document)
        with self._action():
            self._check_account_switch(normalized["account_id"])
            self._persist_auth(document, normalized)
            self._save()
        return self.state()

    def _persist_auth(self, document, normalized, expected_stamp=None):
        try:
            stamp = _atomic_json(
                self.root / "auth.json", document, expected_stamp=expected_stamp
            )
            if self._disk_auth_stamp() != stamp:
                raise AppError(
                    "auth.json 在保存期间被其他程序替换，已停止操作，请重新载入。", 409
                )
        except OSError:
            raise AppError(
                "凭证无法安全保存，已停止后续请求。请检查文件权限并重新导入。", 500
            ) from None
        with self._lock:
            self._auth = normalized
            self._auth_document = copy.deepcopy(document)
            self._auth_error = None
            self._auth_stamp = stamp

    def reload_auth(self):
        with self._action():
            with self._lock:
                self._sync_auth(force=True)
            self._require_auth()
        return self.state()

    def _headers(self):
        auth = self._require_auth()
        headers = {
            "Authorization": "Bearer " + auth["access_token"],
            "Chatgpt-Account-Id": auth["account_id"],
            "Accept": "application/json",
            "Content-Type": "application/json",
            "OpenAI-Beta": "codex-1",
            "Originator": "Codex Desktop",
            "User-Agent": "codex-tui/0.149.1",
        }
        return headers

    def _send(self, method, path, headers, body=None):
        self._check_auth_file_unchanged()
        self._require_auth()
        try:
            status, payload = self.transport(method, path, headers, body)
        except AppError:
            raise
        except Exception:
            raise AppError("上游网络请求失败，结果尚未确认。", 502) from None
        if type(status) is not int or not isinstance(payload, dict):
            raise AppError("上游响应格式无法识别。", 502)
        return status, payload

    def _refresh_access(self):
        auth = self._require_auth()
        if self._refresh_attempted or not auth.get("refresh_token"):
            raise AppError(
                "凭证已失效，且不能自动续期；请重新登录 Codex 并导入新的 auth.json。",
                401,
            )
        self._refresh_attempted = True
        status, payload = self._send(
            "POST",
            "/oauth/token",
            {"Content-Type": "application/json", "Accept": "application/json"},
            {
                "grant_type": "refresh_token",
                "refresh_token": auth["refresh_token"],
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            },
        )
        if (
            not 200 <= status < 300
            or not isinstance(payload.get("access_token"), str)
            or not payload["access_token"].strip()
        ):
            raise AppError(
                "凭证自动续期失败，请重新登录 Codex 并导入新的 auth.json。", 401
            )
        document = copy.deepcopy(self._auth_document)
        tokens = document.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else document
        tokens["access_token"] = payload["access_token"]
        for key in ("refresh_token", "id_token"):
            if key in payload:
                if not isinstance(payload[key], str) or not payload[key]:
                    raise AppError("上游续期响应不完整，请重新导入 auth.json。", 502)
                tokens[key] = payload[key]
        seconds = _number(payload.get("expires_in"))
        expiration = _claims(payload["access_token"]).get("exp")
        if expiration is None and seconds is not None and seconds > 0:
            expiration = self.clock() + seconds
        if expiration is not None:
            tokens["expires_at"] = _iso(expiration)
            if "expired" in document:
                document["expired"] = _iso(expiration)
        else:
            tokens.pop("expires_at", None)
            document.pop("expires_at", None)
            document.pop("expired", None)
        try:
            normalized = _normalize_auth(document)
        except AppError:
            raise AppError("续期响应的凭证格式或账号无效，已停止操作。", 502) from None
        if normalized["account_id"] != auth["account_id"]:
            raise AppError("续期响应的账号与当前账号不一致，已停止操作。", 502)
        # Rotated tokens must reach durable storage before any request uses them.
        self._check_auth_file_unchanged()
        self._persist_auth(document, normalized, expected_stamp=self._auth_stamp)

    def _request(self, method, path, body=None):
        auth = self._require_auth()
        expiry = _timestamp(auth.get("expires_at"))
        if method == "GET" and expiry is not None and expiry <= self.clock() + 30:
            self._refresh_access()
        status, payload = self._send(method, path, self._headers(), body)
        if method == "GET" and status == 401 and not self._refresh_attempted:
            self._refresh_access()
            status, payload = self._send(method, path, self._headers(), body)
        return status, payload

    @staticmethod
    def _http_error(status):
        if status in (401, 403):
            return AppError(
                "凭证失效或无访问权限，请重新登录 Codex 并导入新的 auth.json。", status
            )
        return AppError(f"上游请求返回 HTTP {status}，请稍后查询。", 502)

    def _fetch(self, kind):
        path = "/usage" if kind == "usage" else "/rate-limit-reset-credits"
        account_id = self._require_auth()["account_id"]
        try:
            status, payload = self._request("GET", path)
            if not 200 <= status < 300:
                raise self._http_error(status)
            result = self._usage(payload) if kind == "usage" else self._credits(payload)
            result.update(
                fetched_at=_iso(self.clock()), error=None, _account_id=account_id
            )
            with self._lock:
                self._data[kind] = result
                self._save()
            return result
        except AppError as error:
            with self._lock:
                previous = self._data.get(kind)
                if not previous or previous.get("_account_id") != account_id:
                    previous = (
                        {"windows": []}
                        if kind == "usage"
                        else {
                            "available_count": None,
                            "applicable_available_count": None,
                            "items": [],
                        }
                    )
                    previous.update(fetched_at=None, _account_id=account_id)
                previous["error"] = str(error)
                self._data[kind] = previous
                self._save()
            raise

    def _usage(self, payload):
        windows = []
        groups = [
            (payload.get("rate_limit"), ""),
            (payload.get("code_review_rate_limit"), "代码审查 · "),
        ]
        additional = payload.get("additional_rate_limits", [])
        if isinstance(additional, list):
            for entry in additional:
                if isinstance(entry, dict):
                    name = entry.get("limit_name") or "其他额度"
                    groups.append((entry.get("rate_limit"), str(name)[:80] + " · "))
        for limits, prefix in groups:
            if not isinstance(limits, dict):
                continue
            for key_window, window_name in [
                ("primary_window", "主窗口"),
                ("secondary_window", "次窗口"),
            ]:
                window = limits.get(key_window)
                if not isinstance(window, dict):
                    continue
                duration = _number(window.get("limit_window_seconds"))
                name = (
                    "5 小时"
                    if duration == 18000
                    else "每周"
                    if duration == 604800
                    else window_name
                )
                used = _number(window.get("used_percent"))
                reset = _timestamp(window.get("reset_at"))
                if reset is None:
                    seconds = _number(window.get("reset_after_seconds"))
                    if seconds is not None:
                        reset = self.clock() + max(0, seconds)
                windows.append(
                    {
                        "name": prefix + name,
                        "used_percent": used,
                        "reset_at": _iso(reset),
                        "window_seconds": duration,
                    }
                )
        if not windows:
            raise AppError("用量接口未返回可识别的额度窗口，不能确认当前用量。", 502)
        return {"windows": windows}

    def _credits(self, payload):
        raw_items = payload.get("credits")
        if not isinstance(raw_items, list):
            raise AppError(
                "重置机会接口缺少 credits 列表，无法安全选择待消耗机会。", 502
            )
        items = []
        seen = set()
        for item in raw_items:
            if (
                not isinstance(item, dict)
                or item.get("reset_type") != "codex_rate_limits"
            ):
                continue
            credit_id = item.get("id")
            if not isinstance(credit_id, str) or not credit_id:
                continue
            if credit_id in seen:
                raise AppError("上游返回重复的重置机会 ID，无法安全确定状态。", 502)
            seen.add(credit_id)
            raw_expiry = item.get("expires_at")
            expiry = _iso(raw_expiry)
            items.append(
                {
                    "id": credit_id,
                    "status": str(item.get("status", "unknown")),
                    "expires_at": expiry,
                    "granted_at": _iso(item.get("granted_at")),
                    "_expiry_invalid": raw_expiry is not None and expiry is None,
                }
            )
        available = _count(payload.get("available_count"))
        applicable = _count(payload.get("applicable_available_count"))
        return {
            "available_count": available,
            "applicable_available_count": applicable,
            "items": items,
        }

    def _refresh(self):
        errors = []
        for kind in ("usage", "credits"):
            try:
                self._fetch(kind)
            except AppError as error:
                errors.append(str(error))
        return errors

    def refresh(self):
        with self._action():
            self._require_auth()
            self._refresh()
        return self.state()

    def _unresolved(self):
        account_id = self._require_auth()["account_id"]
        return [
            item
            for item in self._data["operations"]
            if item.get("_account_id") == account_id
            and item.get("status") in {"pending", "uncertain"}
        ]

    def _assert_resolved(self):
        if self._unresolved():
            raise AppError(
                "存在结果未确认的兑换。请先核对或使用原请求 ID 重试，不能新建兑换。",
                409,
            )

    def _available_credit(self, credit_id, credits, require_expiry=False):
        if not isinstance(credit_id, str) or not credit_id:
            raise AppError("请选择一条具体重置机会。")
        credit = next(
            (item for item in credits["items"] if item["id"] == credit_id), None
        )
        if not credit or credit["status"] != "available":
            raise AppError("指定重置机会当前不可用；不会改用其他机会。", 409)
        if credit.get("_expiry_invalid"):
            raise AppError("指定重置机会的到期时间格式无效，无法安全兑换。", 409)
        account_id = self._require_auth()["account_id"]
        if any(
            operation.get("_account_id") == account_id
            and operation.get("credit_id") == credit_id
            and operation.get("status") == "succeeded"
            for operation in self._data["operations"]
        ):
            raise AppError(
                "此重置机会已有成功兑换记录，不能再次提交新兑换。请刷新查询。", 409
            )
        expiry = _timestamp(credit["expires_at"])
        if expiry is not None and expiry <= self.clock():
            raise AppError("指定重置机会已经到期。", 409)
        if require_expiry and expiry is None:
            raise AppError("该重置机会没有可验证的到期时间，无法预约。")
        return credit

    def _new_operation(self, credit_id, scheduled=None):
        operation = {
            "id": str(uuid.uuid4()),
            "credit_id": credit_id,
            "created_at": _iso(self.clock()),
            "status": "pending",
            "code": None,
            "message": "兑换请求已记录，正在等待上游确认。",
            "windows_reset": None,
            "verified": False,
            "_account_id": self._require_auth()["account_id"],
        }
        with self._lock:
            self._data["operations"].append(operation)
            if scheduled is not None:
                scheduled.update(
                    status="running",
                    operation_id=operation["id"],
                    message="预约正在兑换指定重置机会。",
                )
            self._save()
        return operation

    def _update_schedule(self, operation):
        for scheduled in self._data["schedules"]:
            if scheduled.get("operation_id") == operation["id"]:
                status = operation["status"]
                scheduled["status"] = (
                    "completed"
                    if status == "succeeded"
                    else "uncertain"
                    if status == "uncertain"
                    else "running"
                    if status == "pending"
                    else "skipped"
                )
                scheduled["message"] = operation["message"]

    @staticmethod
    def _response_code(payload):
        code = payload.get("code")
        return code if isinstance(code, str) and code.strip() else None

    def _post_operation(self, operation, expires_at=None, scheduled=None):
        try:
            if scheduled is not None:
                self._check_due(scheduled)
            if expires_at is not None and self.clock() >= _timestamp(expires_at):
                raise AppError("指定重置机会已经到期。")
        except AppError as error:
            with self._lock:
                operation.update(status="not_sent", message=f"兑换未提交：{error}")
                self._update_schedule(operation)
                self._save()
            return
        try:
            status, payload = self._request(
                "POST",
                "/rate-limit-reset-credits/consume",
                {
                    "credit_id": operation["credit_id"],
                    "redeem_request_id": operation["id"],
                },
            )
            code = self._response_code(payload)
            if 200 <= status < 300 and code in {"reset", "already_redeemed"}:
                operation.update(
                    status="succeeded",
                    code=code,
                    message="已消耗指定机会并重置额度。"
                    if code == "reset"
                    else "该兑换请求此前已成功，不会再次消耗。",
                )
                reset_windows = payload.get("windows_reset")
                operation["windows_reset"] = (
                    reset_windows
                    if isinstance(reset_windows, int)
                    and not isinstance(reset_windows, bool)
                    else None
                )
            elif 200 <= status < 300 and code in {"no_credit", "nothing_to_reset"}:
                operation.update(
                    status=code,
                    code=code,
                    message="上游确认没有可用机会，本次未重置。"
                    if code == "no_credit"
                    else "上游确认当前无需重置，本次未消耗机会。",
                )
            else:
                operation.update(
                    status="uncertain",
                    code=code,
                    message=(
                        str(self._http_error(status))
                        if not 200 <= status < 300
                        else "上游返回未知兑换结果，尚不能确认是否消耗。请核对或用原请求 ID 重试。"
                    ),
                )
        except AppError as error:
            operation.update(
                status="uncertain",
                code=None,
                message=f"兑换结果未确认：{error} 请核对或使用原请求 ID 重试。",
            )
        with self._lock:
            self._update_schedule(operation)
            self._save()
        if operation["status"] != "uncertain":
            errors = self._refresh()
            credits = self._data.get("credits")
            matched = next(
                (
                    item
                    for item in (credits or {}).get("items", [])
                    if item["id"] == operation["credit_id"]
                ),
                None,
            )
            operation["verified"] = not errors and (
                operation["status"] != "succeeded"
                or not matched
                or matched["status"] != "available"
            )
            if errors and operation["status"] == "succeeded":
                operation["message"] += (
                    " 兑换已确认成功，但最新用量或次数刷新失败；请仅刷新查询。"
                )
            elif operation["status"] == "succeeded" and not operation["verified"]:
                operation["message"] += (
                    " 上游明细尚显示该机会可用，请稍后刷新；不会重复兑换。"
                )
            with self._lock:
                self._update_schedule(operation)
                self._save()

    def consume(self, credit_id):
        with self._action():
            self._assert_resolved()
            credit = self._available_credit(credit_id, self._fetch("credits"))
            operation = self._new_operation(credit["id"])
            self._post_operation(operation, expires_at=credit["expires_at"])
            with self._lock:
                for scheduled in self._data["schedules"]:
                    if (
                        scheduled.get("status") == "scheduled"
                        and scheduled.get("_account_id") == self._auth["account_id"]
                        and scheduled.get("credit_id") == credit_id
                    ):
                        scheduled.update(
                            status="cancelled",
                            message="该机会已手动提交兑换，原预约已取消。",
                        )
                self._save()
        return self.state()

    def _find_operation(self, operation_id):
        account_id = self._require_auth()["account_id"]
        operation = next(
            (
                item
                for item in self._data["operations"]
                if item["id"] == operation_id and item.get("_account_id") == account_id
            ),
            None,
        )
        if not operation:
            raise AppError("未找到当前账号的兑换记录。", 404)
        return operation

    def retry(self, operation_id):
        with self._action():
            operation = self._find_operation(operation_id)
            if operation["status"] == "succeeded":
                pass
            elif operation["status"] != "uncertain":
                raise AppError("仅能对结果未确认的请求使用原请求 ID 重试。", 409)
            else:
                # Validate/refresh credentials without requiring the original credit to remain available.
                self._fetch("credits")
                operation.update(
                    status="pending", message="正在使用原请求 ID 核对兑换结果。"
                )
                with self._lock:
                    self._update_schedule(operation)
                    self._save()
                self._post_operation(operation)
        return self.state()

    def reconcile(self, operation_id):
        with self._action():
            operation = self._find_operation(operation_id)
            errors = self._refresh()
            credits = self._data.get("credits")
            item = next(
                (
                    entry
                    for entry in (credits or {}).get("items", [])
                    if entry["id"] == operation["credit_id"]
                ),
                None,
            )
            if operation["status"] == "uncertain":
                if (
                    credits
                    and not credits.get("error")
                    and item
                    and item["status"] in {"consumed", "redeemed", "used"}
                ):
                    operation.update(
                        status="succeeded",
                        code="reconciled_consumed",
                        verified=not errors,
                        message="查询确认指定重置机会已被消耗；无法判断由本请求还是其他客户端触发。",
                    )
                else:
                    operation["message"] = (
                        "已重新查询；仍无法确定该请求是否完成。请使用原请求 ID 重试，不能新建兑换。"
                    )
            elif operation["status"] == "succeeded":
                operation["verified"] = not errors and (
                    not item or item["status"] != "available"
                )
                if operation["verified"]:
                    operation["message"] = (
                        "兑换已确认成功，最新用量及重置机会查询已完成。"
                    )
            with self._lock:
                self._update_schedule(operation)
                self._save()
        return self.state()

    def schedule(self, credit_id, run_at):
        if (
            not isinstance(run_at, str)
            or not run_at
            or ("Z" not in run_at and "+" not in run_at[10:] and "-" not in run_at[10:])
        ):
            raise AppError("预约时间必须是包含时区的 ISO 时间。")
        run_stamp = _timestamp(run_at)
        if run_stamp is None:
            raise AppError("预约时间格式无效。")
        with self._action():
            self._assert_resolved()
            if any(
                current.get("_account_id") == self._auth["account_id"]
                and current.get("credit_id") == credit_id
                and current.get("status") in {"scheduled", "running", "uncertain"}
                for current in self._data["schedules"]
            ):
                raise AppError(
                    "此重置机会已有预约或未确认的预约兑换，请先取消或核对。", 409
                )
            credit = self._available_credit(
                credit_id, self._fetch("credits"), require_expiry=True
            )
            if run_stamp <= self.clock():
                raise AppError("预约时间必须晚于当前时间。")
            if run_stamp >= _timestamp(credit["expires_at"]):
                raise AppError("预约时间必须早于指定重置机会的到期时间。")
            with self._lock:
                scheduled = {
                    "id": str(uuid.uuid4()),
                    "credit_id": credit_id,
                    "run_at": _iso(run_stamp),
                    "expires_at": credit["expires_at"],
                    "status": "scheduled",
                    "message": "将在预约时刻核实并仅兑换这一次指定机会。",
                    "operation_id": None,
                    "_account_id": self._auth["account_id"],
                }
                self._data["schedules"].append(scheduled)
                self._save()
        return self.state()

    def cancel_schedule(self, schedule_id):
        with self._action():
            account_id = self._auth["account_id"] if self._auth else None
            candidates = [
                item
                for item in self._data["schedules"]
                if item.get("_account_id") == account_id
            ]
            scheduled = next(
                (item for item in candidates if item.get("id") == schedule_id), None
            )
            if not scheduled:
                raise AppError("未找到当前账号的预约。", 404)
            if scheduled and scheduled.get("status") in {"running", "uncertain"}:
                raise AppError(
                    "兑换请求已提交，无法通过取消预约撤回；请核对关联兑换结果。", 409
                )
            if scheduled and scheduled.get("status") == "scheduled":
                with self._lock:
                    scheduled.update(status="cancelled", message="预约已取消。")
                    self._save()
        return self.state()

    def tick(self):
        with self._lock:
            due = [
                item
                for item in self._data["schedules"]
                if item.get("status") == "scheduled"
                and self.clock() >= _timestamp(item["run_at"])
            ]
            if not due:
                return
        try:
            with self._action():
                # A replaced credential file or ambiguous redemption blocks the batch.
                if self._auth_error or self._auth is None:
                    return
                for scheduled in sorted(
                    due, key=lambda item: _timestamp(item["run_at"])
                ):
                    if scheduled.get("status") != "scheduled":
                        continue
                    if self._unresolved() or self._auth_error:
                        break
                    try:
                        auth = self._require_auth()
                        if scheduled.get("_account_id") != auth["account_id"]:
                            raise AppError("当前凭证账号与预约绑定账号不一致。")
                        self._check_due(scheduled)
                        credit = self._available_credit(
                            scheduled["credit_id"],
                            self._fetch("credits"),
                            require_expiry=True,
                        )
                        self._check_due(scheduled)
                        if _timestamp(credit["expires_at"]) <= self.clock():
                            raise AppError("指定重置机会已经到期。")
                    except AppError as error:
                        with self._lock:
                            scheduled.update(
                                status="skipped",
                                message=f"预约未执行：{error} 不会自动改用其他机会或重试。",
                            )
                            self._save()
                        continue
                    operation = self._new_operation(scheduled["credit_id"], scheduled)
                    self._post_operation(
                        operation, expires_at=credit["expires_at"], scheduled=scheduled
                    )
        except AppError as error:
            if error.status != 409:
                raise

    def _check_due(self, scheduled):
        now = self.clock()
        if now < _timestamp(scheduled["run_at"]):
            raise AppError("尚未到达预约时间，系统时钟可能发生了变化。")
        if now > _timestamp(scheduled["run_at"]) + 900:
            raise AppError("已超过预约时刻 15 分钟，不再补执行。")
        if now >= _timestamp(scheduled["expires_at"]):
            raise AppError("预约指定的重置机会已经到期。")
