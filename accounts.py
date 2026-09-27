"""Isolated account stores and explicit account routing for the local console."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from core import AppError, Engine, _atomic_json, _file_stamp, _normalize_auth

PROFILE_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_PROFILES = 100


def _private_json(path, limit=1024 * 1024):
    """Read a stable regular file without following symlinks or leaking its text."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError()
            raw = handle.read(limit + 1)
            if len(raw) > limit or _file_stamp(before) != _file_stamp(
                os.fstat(handle.fileno())
            ):
                raise ValueError()
            document = json.loads(raw)
            if stat.S_IMODE(before.st_mode) != 0o600:
                os.fchmod(handle.fileno(), 0o600)
            stamp = _file_stamp(os.fstat(handle.fileno()))
        if _file_stamp(path.lstat()) != stamp:
            raise ValueError()
        return document, stamp
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise AppError(
            f"{path.name} 无法安全读取，请检查文件格式和权限。", 500
        ) from None


class AccountManager:
    def __init__(
        self, root, transport=None, clock=time.time, *, transport_factory=None
    ):
        self.root = Path(root)
        self.store = self.root / "data" / "accounts"
        self.index_path = self.store / "index.json"
        self.clock = clock
        self.transport = transport
        self.transport_factory = transport_factory
        self._lock = threading.RLock()
        self._engines = {}
        self._errors = {}
        self._leases = {}
        self._refreshing = False
        self._index_stamp = None
        self._legacy_error = None
        self._index = {"version": 1, "active_profile_id": None, "profiles": []}
        self._ensure_store()
        if self.index_path.exists() or self.index_path.is_symlink():
            self._index, self._index_stamp = _private_json(self.index_path)
            self._validate_index(self._index)
        else:
            self._migrate_legacy()
        for entry in self._index["profiles"]:
            try:
                self._engines[entry["id"]] = self._load_engine(entry)
            except AppError:
                self._errors[entry["id"]] = (
                    "此账号的本地数据无法读取，请修复后重启服务。"
                )
        self._pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="quota-account"
        )

    def _ensure_store(self):
        for path in (self.root / "data", self.store):
            if path.is_symlink() or path.exists() and not path.is_dir():
                raise AppError("账号存储目录不能是符号链接或普通文件。", 500)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    @staticmethod
    def _validate_index(document):
        def text(value, maximum):
            return (
                isinstance(value, str)
                and bool(value.strip())
                and len(value) <= maximum
                and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            )

        valid = isinstance(document, dict) and set(document) == {
            "version",
            "active_profile_id",
            "profiles",
        }
        if (
            not valid
            or type(document["version"]) is not int
            or document["version"] != 1
        ):
            raise AppError("账号列表格式无效，请保留 index.json 并修复后重启。", 500)
        profiles = document["profiles"]
        if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
            raise AppError("账号列表格式无效。", 500)
        ids, accounts = set(), set()
        for entry in profiles:
            if isinstance(entry, dict) and set(entry) == {"id", "account_id", "name"}:
                entry["principal_id"] = None
            if not isinstance(entry, dict) or set(entry) != {
                "id",
                "account_id",
                "principal_id",
                "name",
            }:
                raise AppError("账号列表格式无效。", 500)
            profile_id = entry["id"]
            if (
                not isinstance(profile_id, str)
                or not PROFILE_ID.fullmatch(profile_id)
                or profile_id in ids
                or not text(entry["account_id"], 512)
                or (
                    entry["principal_id"] is not None
                    and not text(entry["principal_id"], 512)
                )
                or (
                    entry["principal_id"] is not None
                    and (entry["account_id"], entry["principal_id"]) in accounts
                )
                or not isinstance(entry["name"], str)
                or (entry["name"] and not text(entry["name"], 80))
            ):
                raise AppError("账号列表包含无效或重复的账号。", 500)
            ids.add(profile_id)
            if entry["principal_id"] is not None:
                accounts.add((entry["account_id"], entry["principal_id"]))
        active = document["active_profile_id"]
        if (active is not None and not isinstance(active, str)) or active not in (
            ids if ids else {None}
        ):
            raise AppError("当前账号记录无效。", 500)

    def _commit(self, document):
        self._validate_index(document)
        self._ensure_store()
        try:
            stamp = _atomic_json(
                self.index_path, document, expected_stamp=self._index_stamp
            )
        except OSError:
            raise AppError("账号列表无法保存，请检查本地文件权限。", 500) from None
        self._index, self._index_stamp = document, stamp

    def _directory(self, profile_id):
        self._ensure_store()
        path = self.store / profile_id
        if path.is_symlink() or not path.is_dir():
            raise AppError("账号目录缺失或不是安全目录。", 500)
        for filename in ("auth.json", "state.json"):
            if (path / filename).is_symlink():
                raise AppError("账号文件不能是符号链接。", 500)
        return path

    def _discard_unpublished(self, directories):
        # A directory fsync can fail after index.json has already been replaced.
        # Never discard credentials that the on-disk index may now reference.
        try:
            saved, _ = _private_json(self.index_path)
            self._validate_index(saved)
            registered = {entry["id"] for entry in saved["profiles"]}
        except FileNotFoundError:
            registered = set()
        except AppError:
            return
        for directory in directories:
            if directory.name not in registered:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    pass

    def _load_engine(self, entry):
        path = self._directory(entry["id"])
        if (
            any(item["id"] == entry["id"] for item in self._index["profiles"])
            and not (path / "state.json").is_file()
        ):
            raise AppError("此账号的 state.json 缺失，请先恢复记录。", 500)
        transport = (
            self.transport_factory(entry, path)
            if self.transport_factory
            else self.transport
        )
        return Engine(
            path,
            transport,
            self.clock,
            account_id=entry["account_id"],
            principal_id=entry["principal_id"],
        )

    def _migrate_legacy(self):
        """Copy legacy data once; retain originals and all account-bound records."""
        auth = None
        account_id = None
        principal_id = None
        try:
            auth, _ = _private_json(self.root / "auth.json")
            normalized = _normalize_auth(auth)
            account_id = normalized["account_id"]
            principal_id = normalized["principal_id"]
        except FileNotFoundError:
            pass
        except AppError:
            auth = None
            self._legacy_error = "原 auth.json 无法导入，请选择有效凭证。"
        state = {"usage": None, "credits": None, "schedules": [], "operations": []}
        try:
            state, _ = _private_json(self.root / "state.json", 64 * 1024 * 1024)
            Engine._validate_state(state)
        except FileNotFoundError:
            pass
        except (AppError, ValueError, TypeError):
            raise AppError(
                "原 state.json 无法安全迁移，请保留文件并修复后重启。", 500
            ) from None
        account_ids = {
            item["_account_id"]
            for kind in ("schedules", "operations")
            for item in state[kind]
        }
        account_ids.update(
            state[kind]["_account_id"] for kind in ("usage", "credits") if state[kind]
        )
        if account_id:
            account_ids.add(account_id)
        document = copy.deepcopy(self._index)
        created = []
        try:
            for owner in sorted(account_ids):
                profile_id = uuid.uuid4().hex
                directory = self.store / profile_id
                directory.mkdir(mode=0o700)
                created.append(directory)
                entry = {
                    "id": profile_id,
                    "account_id": owner,
                    "principal_id": principal_id if owner == account_id else None,
                    "name": "",
                }
                document["profiles"].append(entry)
                per_account = {
                    kind: copy.deepcopy(state[kind])
                    if state[kind] and state[kind]["_account_id"] == owner
                    else None
                    for kind in ("usage", "credits")
                }
                per_account.update(
                    {
                        kind: [
                            copy.deepcopy(item)
                            for item in state[kind]
                            if item["_account_id"] == owner
                        ]
                        for kind in ("schedules", "operations")
                    }
                )
                _atomic_json(directory / "state.json", per_account)
                if owner == account_id:
                    _atomic_json(directory / "auth.json", auth)
                    document["active_profile_id"] = profile_id
            if document["profiles"] and document["active_profile_id"] is None:
                document["active_profile_id"] = document["profiles"][0]["id"]
            self._commit(document)
        except Exception:
            self._discard_unpublished(created)
            raise

    def _entry(self, profile_id):
        if not isinstance(profile_id, str) or not PROFILE_ID.fullmatch(profile_id):
            raise AppError("缺少有效账号标识，请刷新页面后重试。")
        entry = next(
            (entry for entry in self._index["profiles"] if entry["id"] == profile_id),
            None,
        )
        if entry is None:
            raise AppError("账号已移除，请刷新列表。", 404)
        return entry

    @contextmanager
    def profile(self, profile_id):
        """Capture an explicit engine and prevent removal until its action ends."""
        with self._lock:
            self._entry(profile_id)
            self._directory(profile_id)
            engine = self._engines.get(profile_id)
            if engine is None:
                raise AppError(self._errors[profile_id], 409)
            self._leases[profile_id] = self._leases.get(profile_id, 0) + 1
        try:
            yield engine
        finally:
            with self._lock:
                self._leases[profile_id] -= 1

    @staticmethod
    def _empty_state():
        return {
            "account": {
                "loaded": False,
                "label": "未导入凭证",
                "account_id": "",
                "expires_at": None,
            },
            "usage": None,
            "credits": None,
            "schedules": [],
            "operations": [],
            "busy": False,
        }

    def state(self, profile_id=None):
        with self._lock:
            selected = (
                profile_id
                if any(entry["id"] == profile_id for entry in self._index["profiles"])
                else self._index["active_profile_id"]
            )
            entries = copy.deepcopy(self._index["profiles"])
            engines, errors, leases = (
                dict(self._engines),
                dict(self._errors),
                dict(self._leases),
            )
            refreshing = self._refreshing
        active = self._empty_state()
        summaries = []
        for entry in entries:
            key = entry["id"]
            engine = engines.get(key)
            try:
                self._directory(key)
            except AppError as error:
                engine = None
                errors[key] = str(error)
            snapshot = engine.state() if engine else self._empty_state()
            snapshot["account"]["account_id"] = entry["account_id"]
            if key in errors:
                snapshot["account"]["error"] = errors[key]
            busy = snapshot["busy"] or leases.get(key, 0) > 0
            pending = sum(
                job["status"] in {"scheduled", "running"}
                for job in snapshot["schedules"]
            )
            unresolved = sum(
                op["status"] in {"pending", "uncertain"}
                for op in snapshot["operations"]
            )
            name = entry["name"] or snapshot["account"]["label"]
            summary = {
                "id": key,
                "name": name,
                "account": snapshot["account"],
                "usage": snapshot["usage"],
                "credits": {
                    field: snapshot["credits"][field]
                    for field in ("available_count", "fetched_at", "error")
                }
                if snapshot["credits"]
                else None,
                "busy": busy,
                "pending_count": pending,
                "unresolved_count": unresolved,
                "can_remove": bool(engine)
                and not busy
                and not engine.has_pending_work(),
            }
            summaries.append(summary)
            if key == selected:
                active = snapshot
                active["busy"] = busy
                active["profile_name"] = name
        active.update(
            profiles=summaries,
            active_profile_id=selected,
            refreshing_all=refreshing,
            import_error=self._legacy_error,
        )
        return active

    def _find_import(self, normalized):
        candidates = [
            entry
            for entry in self._index["profiles"]
            if entry["account_id"] == normalized["account_id"]
        ]
        for entry in candidates:
            if (
                entry["principal_id"] is not None
                and entry["principal_id"] == normalized["principal_id"]
            ):
                return entry
        for entry in candidates:
            engine = self._engines.get(entry["id"])
            if engine and engine.matches_credential(normalized):
                return entry
        for entry in candidates:
            if entry["principal_id"] is not None:
                continue
            engine = self._engines.get(entry["id"])
            if engine is None:
                raise AppError(
                    "此工作区有无法核实的旧账号记录，请先修复状态后再导入。", 409
                )
            if (
                not engine.state()["account"]["loaded"]
                and not engine.has_pending_work()
            ):
                return entry
        return None

    def import_auth(self, document):
        normalized = _normalize_auth(document)
        with self._lock:
            current = self._find_import(normalized)
            updated = copy.deepcopy(self._index)
            created = None
            if current is None:
                if len(updated["profiles"]) >= MAX_PROFILES:
                    raise AppError(
                        f"最多保存 {MAX_PROFILES} 个账号，请先移除不再使用的账号。"
                    )
                current = {
                    "id": uuid.uuid4().hex,
                    "account_id": normalized["account_id"],
                    "principal_id": normalized["principal_id"],
                    "name": "",
                }
                updated["profiles"].append(current)
                self._validate_index({**updated, "active_profile_id": current["id"]})
                created = self.store / current["id"]
                created.mkdir(mode=0o700)
            profile_id = current["id"]
            try:
                engine = self._engines.get(profile_id) or self._load_engine(current)
                engine.import_auth(document)
                next(
                    entry for entry in updated["profiles"] if entry["id"] == profile_id
                )["principal_id"] = normalized["principal_id"]
                updated["active_profile_id"] = profile_id
                self._commit(updated)
            except Exception:
                if created:
                    self._discard_unpublished([created])
                raise
            self._engines[profile_id] = engine
            self._errors.pop(profile_id, None)
            self._legacy_error = None
            return profile_id

    def reload_auth(self):
        try:
            document, _ = _private_json(self.root / "auth.json")
        except FileNotFoundError:
            raise AppError(
                "工具文件夹中没有 auth.json，请选择文件或粘贴 JSON。", 404
            ) from None
        return self.import_auth(document)

    def select(self, profile_id):
        with self._lock:
            self._entry(profile_id)
            updated = copy.deepcopy(self._index)
            updated["active_profile_id"] = profile_id
            self._commit(updated)

    def rename(self, profile_id, name):
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 80
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            raise AppError("账号名称需为 1–80 个字符，不能包含控制字符。")
        with self._lock:
            self._entry(profile_id)
            updated = copy.deepcopy(self._index)
            next(entry for entry in updated["profiles"] if entry["id"] == profile_id)[
                "name"
            ] = name.strip()
            self._commit(updated)

    def remove(self, profile_id):
        with self._lock:
            self._entry(profile_id)
            engine = self._engines.get(profile_id)
            if (
                engine is None
                or self._leases.get(profile_id)
                or engine.has_pending_work()
            ):
                raise AppError(
                    "账号仍在操作中、有待执行预约或待核实记录，请先处理后再移除。", 409
                )
            directory = self._directory(profile_id)
            retired = self.store / (".removed-" + profile_id)
            directory.rename(retired)
            updated = copy.deepcopy(self._index)
            updated["profiles"] = [
                entry for entry in updated["profiles"] if entry["id"] != profile_id
            ]
            if updated["active_profile_id"] == profile_id:
                updated["active_profile_id"] = (
                    updated["profiles"][0]["id"] if updated["profiles"] else None
                )
            try:
                self._commit(updated)
            except Exception:
                # Restore only when the saved index still references the original
                # directory. An fsync error can follow a successful replacement.
                try:
                    saved, _ = _private_json(self.index_path)
                except (AppError, FileNotFoundError):
                    saved = None
                if saved == self._index:
                    retired.rename(directory)
                else:
                    self._engines.pop(profile_id, None)
                    self._errors[profile_id] = (
                        "账号移除结果需要核实，已保留本地文件，请重启服务后检查。"
                    )
                    raise AppError(self._errors[profile_id], 500) from None
                raise
            self._engines.pop(profile_id)
            self._leases.pop(profile_id, None)
        try:
            shutil.rmtree(retired)
        except OSError:
            raise AppError(
                "账号已从列表移除，但本地文件清理未完成，请检查账号目录权限。", 500
            ) from None

    def refresh_all(self):
        with self._lock:
            if self._refreshing:
                raise AppError("正在刷新全部账号，请稍后再试。", 409)
            self._refreshing = True
            ids = [entry["id"] for entry in self._index["profiles"]]

        def refresh(profile_id):
            try:
                with self.profile(profile_id) as engine:
                    state = engine.refresh()
                    return not any(
                        state.get(kind, {}).get("error")
                        for kind in ("usage", "credits")
                        if state.get(kind)
                    )
            except AppError:
                return False

        try:
            results = list(self._pool.map(refresh, ids))
            return {
                "total": len(ids),
                "succeeded": sum(results),
                "failed": len(ids) - sum(results),
            }
        finally:
            with self._lock:
                self._refreshing = False

    def tick(self):
        with self._lock:
            ids = [
                key
                for key, engine in self._engines.items()
                if engine.has_due_schedules()
            ]

        def run(profile_id):
            try:
                with self.profile(profile_id) as engine:
                    engine.tick()
            except AppError as error:
                if error.status != 404:
                    raise

        failures = []
        for future in as_completed([self._pool.submit(run, key) for key in ids]):
            try:
                future.result()
            except Exception as error:
                failures.append(error)
        if failures:
            raise AppError("部分账号的预约处理失败，请核实相关记录。", 500)

    def close(self):
        self._pool.shutdown(wait=True)
