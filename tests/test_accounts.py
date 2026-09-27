import http.client
import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import Clock, FakeTransport, jwt

from accounts import AccountManager
from core import AppError, Engine, _atomic_json, _iso
from server import LocalServer


def auth(name, token=None):
    return {
        "access_token": token or f"fake-multi-access-{name}",
        "account_id": f"account-{name}",
        "label": f"账号 {name}",
        "id_token": jwt(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": f"account-{name}",
                    "chatgpt_user_id": f"test-user-{name}",
                }
            }
        ),
    }


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.transports = {}

    def create(self):
        def transport(entry, directory):
            return self.transports.setdefault(entry["account_id"], FakeTransport())

        manager = AccountManager(
            self.root, clock=self.clock, transport_factory=transport
        )
        self.addCleanup(manager.close)
        return manager

    def add_two(self, manager):
        return manager.import_auth(auth("a")), manager.import_auth(auth("b"))

    def test_independent_quotas_credentials_selection_and_restart(self):
        manager = self.create()
        a, b = self.add_two(manager)
        self.transports["account-b"].usage["rate_limit"]["primary_window"][
            "used_percent"
        ] = 25
        self.assertEqual(
            manager.refresh_all(), {"total": 2, "succeeded": 2, "failed": 0}
        )
        state = manager.state()
        self.assertEqual(state["active_profile_id"], b)
        values = {
            p["id"]: p["usage"]["windows"][0]["used_percent"] for p in state["profiles"]
        }
        self.assertEqual(values, {a: 97, b: 25})
        self.assertNotIn("fake-multi-access", json.dumps(state))
        self.assertNotIn("access_token", json.dumps(state))
        manager.rename(a, "工作账号")
        self.assertEqual(manager.import_auth(auth("a", "fake-rotated-access-a")), a)
        self.assertEqual(len(manager.state()["profiles"]), 2)
        manager.close()
        restored = self.create()
        self.assertEqual(restored.state()["active_profile_id"], a)
        self.assertEqual(restored.state()["profile_name"], "工作账号")
        for key in (a, b):
            directory = restored.store / key
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for name in ("auth.json", "state.json"):
                self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(restored.index_path.stat().st_mode), 0o600)

    def test_captured_target_ignores_selection_and_duplicate_credit_ids(self):
        manager = self.create()
        a, b = self.add_two(manager)
        with manager.profile(a) as target:
            manager.select(b)
            target.consume("credit-1")
        self.assertEqual(len(self.transports["account-a"].consumes()), 1)
        self.assertEqual(self.transports["account-b"].consumes(), [])
        headers = self.transports["account-a"].consumes()[0][2]
        self.assertEqual(headers["Chatgpt-Account-Id"], "account-a")
        self.assertEqual(headers["Authorization"], "Bearer fake-multi-access-a")
        self.assertEqual(manager.state()["account"]["account_id"], "account-b")
        self.assertEqual(manager.state(a)["operations"][0]["status"], "succeeded")

    def test_different_users_in_one_workspace_keep_separate_profiles(self):
        transports = {}
        manager = AccountManager(
            self.root,
            clock=self.clock,
            transport_factory=lambda entry, directory: transports.setdefault(
                entry["id"], FakeTransport()
            ),
        )
        self.addCleanup(manager.close)

        def document(user):
            return {
                "account_id": "shared-workspace",
                "access_token": jwt(
                    {
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "shared-workspace",
                            "chatgpt_user_id": user,
                        }
                    }
                ),
            }

        first, second = document("test-user-a"), document("test-user-b")
        a, b = manager.import_auth(first), manager.import_auth(second)
        self.assertNotEqual(a, b)
        self.assertEqual(manager.import_auth(first), a)
        self.assertEqual(len(manager.state()["profiles"]), 2)
        with manager.profile(a) as engine:
            engine.consume("credit-1")
        self.assertEqual(len(transports[a].consumes()), 1)
        self.assertEqual(transports[b].consumes(), [])
        _atomic_json(manager.store / a / "auth.json", second)
        with manager.profile(a) as engine, self.assertRaises(AppError):
            engine.refresh()
        self.assertIn("登录身份", manager.state(a)["account"]["error"])

    def test_unidentified_credentials_do_not_overwrite_each_other(self):
        manager = self.create()
        first = {
            "account_id": "shared-workspace",
            "access_token": "fake-opaque-access-a",
        }
        second = {
            "account_id": "shared-workspace",
            "access_token": "fake-opaque-access-b",
        }
        a, b = manager.import_auth(first), manager.import_auth(second)
        self.assertNotEqual(a, b)
        self.assertEqual(manager.import_auth(first), a)
        self.assertEqual(
            json.loads((manager.store / b / "auth.json").read_text()), second
        )

    def test_removed_legacy_account_is_not_reimported_implicitly(self):
        old = Engine(self.root, FakeTransport(), self.clock)
        old.import_auth(auth("a"))
        manager = self.create()
        manager.remove(manager.state()["active_profile_id"])
        manager.close()
        restored = self.create()
        self.assertEqual(restored.state()["profiles"], [])
        self.assertTrue((self.root / "auth.json").is_file())
        restored.reload_auth()
        self.assertEqual(len(restored.state()["profiles"]), 1)

    def test_all_schedules_run_once_after_switch_and_restart(self):
        manager = self.create()
        a, b = self.add_two(manager)
        for key in (a, b):
            with manager.profile(key) as engine:
                engine.schedule("credit-1", _iso(self.clock.now + 5))
        manager.select(b)
        manager.close()
        restored = self.create()
        self.clock.now += 6
        restored.tick()
        restored.tick()
        for key, owner in ((a, "account-a"), (b, "account-b")):
            self.assertEqual(len(self.transports[owner].consumes()), 1)
            self.assertEqual(restored.state(key)["schedules"][0]["status"], "completed")

    def test_uncertain_account_does_not_block_another_account(self):
        manager = self.create()
        a, b = self.add_two(manager)
        self.transports["account-a"].queue(
            "POST", "/rate-limit-reset-credits/consume", AppError("模拟连接中断", 502)
        )
        with manager.profile(a) as engine:
            engine.consume("credit-1")
        with self.assertRaises(AppError):
            manager.remove(a)
        manager.select(b)
        with manager.profile(b) as engine:
            engine.consume("credit-1")
        self.assertEqual(manager.state(a)["operations"][0]["status"], "uncertain")
        self.assertEqual(manager.state(b)["operations"][0]["status"], "succeeded")

    def test_missing_credentials_keep_pending_work_visible_and_cancellable(self):
        manager = self.create()
        a, b = self.add_two(manager)
        with manager.profile(a) as engine:
            engine.schedule("credit-1", _iso(self.clock.now + 5))
        manager.close()
        (manager.store / a / "auth.json").unlink()
        restored = self.create()
        state = restored.state(a)
        self.assertFalse(state["account"]["loaded"])
        self.assertEqual(len(state["schedules"]), 1)
        self.assertFalse(
            next(p for p in state["profiles"] if p["id"] == a)["can_remove"]
        )
        with self.assertRaises(AppError):
            restored.remove(a)
        with restored.profile(a) as engine:
            engine.cancel_schedule(state["schedules"][0]["id"])
        restored.remove(a)
        self.assertEqual(restored.state()["active_profile_id"], b)

    def test_replaced_profile_cannot_query_with_another_accounts_token(self):
        manager = self.create()
        a, _ = self.add_two(manager)
        _atomic_json(manager.store / a / "auth.json", auth("b"))
        with manager.profile(a) as engine, self.assertRaises(AppError):
            engine.refresh()
        self.assertEqual(self.transports["account-b"].calls, [])
        self.assertEqual(manager.state(a)["account"]["account_id"], "account-a")
        self.assertIn("不匹配", manager.state(a)["account"]["error"])

    def test_removal_checks_active_requests_and_rolls_back_if_index_save_fails(self):
        manager = self.create()
        a, b = self.add_two(manager)
        original = (manager.store / a / "auth.json").read_bytes()
        with manager.profile(a), self.assertRaises(AppError):
            manager.remove(a)
        with (
            patch("accounts._atomic_json", side_effect=OSError("fake disk failure")),
            self.assertRaises(AppError),
        ):
            manager.remove(a)
        self.assertEqual((manager.store / a / "auth.json").read_bytes(), original)
        self.assertEqual(len(manager.state()["profiles"]), 2)
        manager.remove(a)
        self.assertFalse((manager.store / a).exists())
        self.assertTrue((manager.store / b / "auth.json").exists())

    def test_failed_new_import_does_not_leave_an_unregistered_credential(self):
        manager = self.create()
        with (
            patch("accounts._atomic_json", side_effect=OSError("fake disk failure")),
            self.assertRaises(AppError),
        ):
            manager.import_auth(auth("a"))
        self.assertEqual(manager.state()["profiles"], [])
        self.assertEqual(list(manager.store.glob("*/auth.json")), [])

    def test_removal_does_not_reactivate_an_account_after_index_publish_error(self):
        manager = self.create()
        a, b = self.add_two(manager)
        original = (manager.store / a / "auth.json").read_bytes()

        def publish_then_fail(path, document, **kwargs):
            _atomic_json(path, document, **kwargs)
            raise OSError("fake directory sync failure")

        with (
            patch("accounts._atomic_json", side_effect=publish_then_fail),
            self.assertRaises(AppError),
        ):
            manager.remove(a)
        with self.assertRaises(AppError):
            with manager.profile(a) as engine:
                engine.consume("credit-1")
        with self.assertRaises(AppError):
            manager.import_auth(auth("a"))
        self.assertEqual(self.transports["account-a"].consumes(), [])
        self.assertFalse((manager.store / a).exists())
        self.assertEqual(
            (manager.store / (".removed-" + a) / "auth.json").read_bytes(), original
        )
        manager.close()
        restored = self.create()
        self.assertEqual([p["id"] for p in restored.state()["profiles"]], [b])

    def test_import_preserves_credentials_if_index_publish_precedes_io_error(self):
        manager = self.create()

        def publish_then_fail(path, document, **kwargs):
            _atomic_json(path, document, **kwargs)
            raise OSError("fake directory sync failure")

        with (
            patch("accounts._atomic_json", side_effect=publish_then_fail),
            self.assertRaises(AppError),
        ):
            manager.import_auth(auth("a"))
        manager.close()
        restored = self.create()
        self.assertEqual(len(restored.state()["profiles"]), 1)
        self.assertTrue(restored.state()["account"]["loaded"])

    def test_legacy_migration_preserves_sources_and_each_accounts_ledger(self):
        upstream = FakeTransport()
        self.transports.update({"account-a": upstream, "account-b": FakeTransport()})
        old = Engine(self.root, upstream, self.clock)
        old.import_auth(auth("a"))
        old.consume("credit-1")
        old.import_auth(auth("b"))
        old.transport = self.transports["account-b"]
        old.schedule("credit-2", _iso(self.clock.now + 5))
        originals = {
            name: (self.root / name).read_bytes()
            for name in ("auth.json", "state.json")
        }
        manager = self.create()
        profiles = {
            p["account"]["account_id"]: p["id"] for p in manager.state()["profiles"]
        }
        self.assertEqual(len(profiles), 2)
        a, b = profiles["account-a"], profiles["account-b"]
        self.assertEqual(manager.state()["active_profile_id"], b)
        self.assertFalse(manager.state(a)["account"]["loaded"])
        self.assertEqual(manager.state(a)["operations"][0]["status"], "succeeded")
        self.assertEqual(manager.import_auth(auth("a")), a)
        self.clock.now += 6
        manager.tick()
        self.assertEqual(manager.state(b)["schedules"][0]["status"], "completed")
        for name, data in originals.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_migrated_pending_operation_becomes_uncertain_without_submission(self):
        upstream = FakeTransport()
        old = Engine(self.root, upstream, self.clock)
        old.import_auth(auth("a"))
        old._new_operation("credit-1")
        self.transports["account-a"] = upstream
        calls = len(upstream.calls)
        manager = self.create()
        self.assertEqual(manager.state()["operations"][0]["status"], "uncertain")
        self.assertEqual(len(upstream.calls), calls)
        self.assertFalse(manager.state()["profiles"][0]["can_remove"])

    def test_corrupt_profile_is_isolated_and_mutations_never_fall_back(self):
        manager = self.create()
        a, b = self.add_two(manager)
        manager.close()
        (manager.store / a / "state.json").write_text("{}")
        restored = self.create()
        self.assertIn("error", restored.state(a)["account"])
        with restored.profile(b) as engine:
            engine.refresh()
        for value in (a, None, "../auth.json", "0" * 32, [], {}):
            with self.subTest(value=value), self.assertRaises(AppError):
                with restored.profile(value):
                    pass
        self.assertEqual(self.transports["account-a"].calls, [])

    def test_missing_ledger_and_symlink_credentials_fail_closed(self):
        manager = self.create()
        a, b = self.add_two(manager)
        manager.close()
        (manager.store / a / "state.json").unlink()
        source = manager.store / b / "auth.json"
        original = source.read_bytes()
        (manager.store / a / "auth.json").unlink()
        (manager.store / a / "auth.json").symlink_to(source)
        restored = self.create()
        self.assertIn("error", restored.state(a)["account"])
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(AppError):
            restored.import_auth(auth("a"))

    def test_parallel_refresh_exposes_progress_and_isolates_failure(self):
        manager = self.create()
        a, b = self.add_two(manager)
        entered = threading.Barrier(3)
        release = threading.Event()
        self.addCleanup(release.set)

        def wait(method, path, headers, body):
            if path == "/usage":
                entered.wait(timeout=3)
                release.wait(timeout=3)

        for transport in self.transports.values():
            transport.before_call = wait
        self.transports["account-a"].queue("GET", "/usage", (503, {}))
        result = []
        worker = threading.Thread(target=lambda: result.append(manager.refresh_all()))
        worker.start()
        entered.wait(timeout=3)
        try:
            state = manager.state()
            self.assertTrue(state["refreshing_all"])
            self.assertTrue(all(p["busy"] for p in state["profiles"]))
            with self.assertRaises(AppError):
                manager.refresh_all()
            with self.assertRaises(AppError):
                manager.remove(a)
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [{"total": 2, "succeeded": 1, "failed": 1}])
        self.assertIn("503", manager.state(a)["usage"]["error"])
        self.assertIsNone(manager.state(b)["usage"]["error"])

    def test_http_uses_explicit_target_and_never_serves_credential_files(self):
        manager = self.create()
        a, b = self.add_two(manager)
        server = LocalServer(("127.0.0.1", 0), manager, self.root)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()

        def request(method, path, data=None):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=3
            )
            try:
                connection.request(
                    method,
                    path,
                    json.dumps(data or {}) if method == "POST" else None,
                    {"Content-Type": "application/json", "X-Local-Token": server.csrf},
                )
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            self.assertEqual(
                request("POST", "/api/consume", {"credit_id": "credit-1"})[0], 400
            )
            status, result = request(
                "POST",
                "/api/consume",
                {"profile_id": a, "view_profile_id": b, "credit_id": "credit-1"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["state"]["active_profile_id"], b)
            self.assertEqual(len(self.transports["account-a"].consumes()), 1)
            self.assertEqual(self.transports["account-b"].consumes(), [])
            status, result = request("GET", f"/api/state?profile_id={a}")
            self.assertEqual(status, 200)
            self.assertEqual(result["state"]["active_profile_id"], a)
            self.assertNotIn("fake-multi-access", json.dumps(result))
            for path in (
                "/data/accounts/index.json",
                f"/data/accounts/{a}/auth.json",
                f"/data/accounts/{a}/state.json",
            ):
                self.assertEqual(request("GET", path)[0], 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
