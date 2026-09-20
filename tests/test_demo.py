"""Check the offline mode cannot touch real credentials or redeem real credits."""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core
from core import AppError
from demo import DEMO_AUTH, build_demo_engine, demo_root
from server import LocalServer


class DemoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 2_000_000_000
        self.clock = lambda: self.now
        patch = mock.patch.object(
            core,
            "curl_transport",
            side_effect=AssertionError("Unexpected real network request"),
        )
        self.network = patch.start()
        self.addCleanup(patch.stop)

    def create(self):
        return build_demo_engine(self.root, self.clock)

    def test_demo_never_reads_real_auth_and_persists_only_dummy_auth(self):
        real_auth = self.root / "auth.json"
        real_auth.write_text("REAL_CREDENTIAL_SENTINEL")
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            if path == real_auth:
                raise AssertionError("Real auth must never be read in demo mode")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", new=guarded):
            engine = self.create()
            state = engine.state()
            self.assertTrue(state["account"]["loaded"])
            self.assertEqual(state["credits"]["available_count"], 2)
            self.assertEqual(engine.root, self.root / "data" / "demo")
        self.assertEqual(real_auth.read_text(), "REAL_CREDENTIAL_SENTINEL")
        self.assertEqual(json.loads((engine.root / "auth.json").read_text()), DEMO_AUTH)
        self.assertNotIn(DEMO_AUTH["access_token"], json.dumps(state))
        self.network.assert_not_called()

    def test_consumption_survives_restart_and_original_request_is_idempotent(self):
        engine = self.create()
        state = engine.consume("demo-credit-1")
        self.assertEqual(state["credits"]["available_count"], 1)
        self.assertEqual(state["operations"][0]["status"], "succeeded")
        request_id = state["operations"][0]["id"]
        restored = self.create()
        self.assertEqual(restored.state()["credits"]["available_count"], 1)
        headers = {"Authorization": "Bearer " + DEMO_AUTH["access_token"]}
        status, response = restored.transport(
            "POST",
            "/rate-limit-reset-credits/consume",
            headers,
            {"credit_id": "demo-credit-1", "redeem_request_id": request_id},
        )
        self.assertEqual((status, response["code"]), (200, "already_redeemed"))
        restored.refresh()
        self.assertEqual(restored.state()["credits"]["available_count"], 1)
        self.assertEqual(restored.state()["usage"]["windows"][0]["used_percent"], 0)
        with self.assertRaises(AppError):
            restored.consume("demo-credit-1")
        self.network.assert_not_called()

    def test_nothing_to_reset_preserves_remaining_credit(self):
        engine = self.create()
        engine.consume("demo-credit-1")
        state = engine.consume("demo-credit-2")
        self.assertEqual(state["operations"][0]["status"], "nothing_to_reset")
        self.assertEqual(state["credits"]["available_count"], 1)

    def test_expired_mock_credits_are_not_recreated_on_restart(self):
        self.create()
        self.now += 172801
        engine = self.create()
        self.assertEqual(engine.state()["credits"]["available_count"], 0)
        self.assertEqual(
            {entry["status"] for entry in engine.state()["credits"]["items"]},
            {"expired"},
        )

    def test_demo_schedule_runs_through_real_engine_without_network(self):
        engine = self.create()
        from core import _iso

        engine.schedule("demo-credit-1", _iso(self.now + 30))
        self.now += 31
        engine.tick()
        self.assertEqual(engine.state()["operations"][0]["status"], "succeeded")
        self.assertEqual(engine.state()["credits"]["available_count"], 1)
        engine.tick()
        self.assertEqual(len(engine.state()["operations"]), 1)
        self.network.assert_not_called()

    def test_invalid_persisted_demo_state_fails_without_resetting_credits(self):
        engine = self.create()
        path = engine.root / "demo-upstream.json"
        path.write_text("{invalid")
        with self.assertRaises(AppError):
            self.create()
        self.assertEqual(path.read_text(), "{invalid")

    def test_demo_data_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            (self.root / "data").symlink_to(other, target_is_directory=True)
            with self.assertRaises(AppError):
                demo_root(self.root)

    def test_demo_server_marks_state_and_refuses_real_auth_upload_and_reload(self):
        engine = self.create()
        server = LocalServer(("127.0.0.1", 0), engine, ROOT / "web", demo=True)
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        worker.start()
        try:
            port = server.server_address[1]
            for path in ("/api/state", "/api/health"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertTrue(result.get("state", result)["demo"])
                finally:
                    connection.close()
            for path in ("/api/auth", "/api/auth/reload"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request(
                        "POST",
                        path,
                        json.dumps({"auth": {"access_token": "REAL_SENTINEL"}}),
                        {
                            "Content-Type": "application/json",
                            "X-Local-Token": server.csrf,
                        },
                    )
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 403)
                    self.assertIn("离线演示模式", result["error"])
                finally:
                    connection.close()
            self.assertEqual(
                json.loads((engine.root / "auth.json").read_text()), DEMO_AUTH
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
