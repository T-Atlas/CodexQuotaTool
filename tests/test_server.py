"""Exercise the loopback HTTP boundary without credentials or upstream calls."""

import http.client
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server
from core import AppError
from server import MAX_BODY, LocalServer


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.state_failure = None

    def state(self):
        if self.state_failure:
            raise self.state_failure
        return {"auth": {"loaded": False}, "operations": []}

    def record(self, action, *args):
        self.calls.append((action, args))
        if self.failure:
            raise self.failure

    def import_auth(self, document):
        self.record("import_auth", document)

    def refresh(self):
        self.record("refresh")

    def consume(self, credit_id):
        self.record("consume", credit_id)

    def schedule(self, credit_id, run_at):
        self.record("schedule", credit_id, run_at)

    def cancel_schedule(self, schedule_id):
        self.record("cancel_schedule", schedule_id)

    def reload_auth(self):
        self.record("reload_auth")

    def retry(self, operation_id):
        self.record("retry", operation_id)

    def reconcile(self, operation_id):
        self.record("reconcile", operation_id)


class ServerTests(unittest.TestCase):
    def test_cancel_one_schedule_dispatches_only_selected_id(self):
        status, _, raw = self.post("/api/schedule/cancel", {"schedule_id": "job-one"})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.engine.calls, [("cancel_schedule", ("job-one",))])

    def test_cancel_rejects_non_string_schedule_id(self):
        for value in ([], {}, 123, "", " "):
            self.engine.calls.clear()
            self.assert_error(
                self.post("/api/schedule/cancel", {"schedule_id": value}), 400
            )
            self.assertEqual(self.engine.calls, [])

    def test_reload_auth_uses_local_file_method(self):
        status, _, raw = self.post("/api/auth/reload", {})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.engine.calls, [("reload_auth", ())])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.web_root = Path(self.temp.name) / "web"
        self.web_root.mkdir()
        self.files = {
            "index.html": b"<!doctype html><title>Quota</title>",
            "app.js": b"console.log('ready');",
            "style.css": b"body { color: black; }",
        }
        for name, data in self.files.items():
            (self.web_root / name).write_bytes(data)
        self.secret = "FAKE_SECRET_MUST_NEVER_BE_EXPOSED"
        for parent in (self.web_root, self.web_root.parent):
            (parent / "auth.json").write_text(self.secret)
        self.engine = FakeEngine()
        self.server = LocalServer(("127.0.0.1", 0), self.engine, self.web_root)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request(self, method="GET", path="/api/state", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path, data=None, headers=None, with_token=True, raw=None):
        supplied = {"Content-Type": "application/json"}
        if with_token:
            supplied["X-Local-Token"] = self.server.csrf
        supplied.update(headers or {})
        body = (
            raw
            if raw is not None
            else json.dumps(data if data is not None else {}).encode()
        )
        return self.request("POST", path, body, supplied)

    def assert_error(self, response, expected_status):
        status, headers, raw = response
        self.assertEqual(status, expected_status, raw)
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        self.assertIsInstance(payload["error"], str)
        self.assertNotIn(self.secret, raw.decode())
        return payload

    def test_health_identifies_service_without_credentials_or_csrf(self):
        status, headers, raw = self.request(path="/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app"], "CodexQuotaTool")
        self.assertIsInstance(payload["pid"], int)
        self.assertEqual(payload["instance_id"], self.server.instance_id)
        self.assertEqual(len(payload["instance_id"]), 64)
        self.assertNotIn("csrf", payload)
        self.assertNotIn(self.secret, raw.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_state_includes_session_token_and_security_headers(self):
        status, headers, raw = self.request()
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"]["csrf"], self.server.csrf)
        self.assertEqual(payload["state"]["operations"], [])
        self.assertGreaterEqual(len(self.server.csrf), 32)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_static_allowlist_serves_only_expected_assets(self):
        for route, filename in (
            ("/", "index.html"),
            ("/index.html", "index.html"),
            ("/app.js", "app.js"),
            ("/style.css", "style.css"),
            ("/app.js?v=1", "app.js"),
        ):
            with self.subTest(route=route):
                status, _, raw = self.request(path=route)
                self.assertEqual(status, 200)
                self.assertEqual(raw, self.files[filename])

    def test_auth_files_and_path_traversal_are_not_served(self):
        for path in (
            "/auth.json",
            "/../auth.json",
            "/%2e%2e/auth.json",
            "/..%2fauth.json",
            "/web/auth.json",
            "/.runtime.json",
            "/.server.lock",
            "/server.log",
            "/server.py",
            "/app.js/../auth.json",
        ):
            with self.subTest(path=path):
                self.assert_error(self.request(path=path), 404)

    def test_missing_static_asset_is_safe_404(self):
        (self.web_root / "app.js").unlink()
        self.assert_error(self.request(path="/app.js"), 404)

    def test_host_blocks_dns_rebinding_on_reads_and_writes(self):
        for host in (
            "attacker.example",
            f"attacker.example:{self.port}",
            "127.0.0.1",
            "",
            f"localhost:{self.port + 1}",
        ):
            with self.subTest(host=host):
                self.assert_error(self.request(headers={"Host": host}), 403)
                self.assert_error(
                    self.post(
                        "/api/consume", {"credit_id": "c1"}, headers={"Host": host}
                    ),
                    403,
                )
        self.assertEqual(self.engine.calls, [])

    def test_localhost_with_matching_port_is_allowed(self):
        status, _, _ = self.request(headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_origin_rejects_cross_site_reads_and_writes(self):
        for origin in (
            "https://attacker.example",
            "null",
            f"http://localhost:{self.port + 1}",
        ):
            with self.subTest(origin=origin):
                self.assert_error(self.request(headers={"Origin": origin}), 403)
                self.assert_error(
                    self.post("/api/refresh", headers={"Origin": origin}), 403
                )
        self.assertEqual(self.engine.calls, [])

    def test_matching_origin_is_accepted(self):
        status, _, _ = self.post(
            "/api/refresh", headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls, [("refresh", ())])

    def test_cross_site_fetch_metadata_is_rejected(self):
        self.assert_error(self.request(headers={"Sec-Fetch-Site": "cross-site"}), 403)
        self.assert_error(
            self.post("/api/refresh", headers={"Sec-Fetch-Site": "cross-site"}), 403
        )
        self.assertEqual(self.engine.calls, [])

    def test_preflight_is_rejected_without_cors_headers(self):
        response = self.request(
            "OPTIONS", "/api/consume", headers={"Origin": "https://attacker.example"}
        )
        self.assert_error(response, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response[1])

    def test_missing_or_incorrect_csrf_never_reaches_engine(self):
        self.assert_error(
            self.post("/api/consume", {"credit_id": "c1"}, with_token=False), 403
        )
        self.assert_error(
            self.post(
                "/api/consume", {"credit_id": "c1"}, headers={"X-Local-Token": "wrong"}
            ),
            403,
        )
        self.assertEqual(self.engine.calls, [])

    def test_non_ascii_csrf_is_rejected_without_dropping_connection(self):
        self.assert_error(
            self.post(
                "/api/consume", {"credit_id": "c1"}, headers={"X-Local-Token": "\u00e9"}
            ),
            403,
        )
        self.assertEqual(self.engine.calls, [])

    def test_non_json_content_type_is_rejected(self):
        self.assert_error(
            self.post("/api/refresh", headers={"Content-Type": "text/plain"}), 415
        )
        self.assertEqual(self.engine.calls, [])

    def test_json_charset_content_type_is_accepted(self):
        status, _, _ = self.post(
            "/api/refresh", headers={"Content-Type": "application/json; charset=utf-8"}
        )
        self.assertEqual(status, 200)

    def test_malformed_and_non_object_json_are_rejected(self):
        for raw in (b"{", b"[]", b"null", b'"text"', b"42", b"\xff"):
            with self.subTest(raw=raw):
                self.assert_error(self.post("/api/refresh", raw=raw), 400)
        self.assertEqual(self.engine.calls, [])

    def test_invalid_content_length_and_transfer_encoding_are_rejected(self):
        cases = (
            ({"Content-Length": "invalid"}, 400),
            ({"Content-Length": "-1"}, 400),
            ({"Content-Length": "2_0"}, 400),
            ({"Content-Length": "+2"}, 400),
            ({"Content-Length": str(MAX_BODY + 1)}, 413),
            ({"Transfer-Encoding": "chunked"}, 400),
        )
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assert_error(self.post("/api/refresh", headers=headers), expected)
        self.assertEqual(self.engine.calls, [])

    def test_import_auth_requires_object_and_dispatches_document(self):
        for invalid in (None, [], "credential"):
            with self.subTest(invalid=invalid):
                self.assert_error(self.post("/api/auth", {"auth": invalid}), 400)
        self.assertEqual(self.engine.calls, [])
        document = {"tokens": {"access_token": self.secret}}
        status, _, raw = self.post("/api/auth", {"auth": document})
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls, [("import_auth", (document,))])
        self.assertNotIn(self.secret, raw.decode())

    def test_consume_requires_credit_id_and_dispatches_exactly_once(self):
        for credit_id in (None, "", [], 123, "x" * 513):
            with self.subTest(credit_id=credit_id):
                self.assert_error(
                    self.post("/api/consume", {"credit_id": credit_id}), 400
                )
        self.assertEqual(self.engine.calls, [])
        status, _, raw = self.post("/api/consume", {"credit_id": "credit-123"})
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls, [("consume", ("credit-123",))])
        self.assertEqual(json.loads(raw)["state"]["csrf"], self.server.csrf)

    def test_schedule_passes_credit_and_timestamp_without_conversion(self):
        run_at = "2030-01-02T03:04:05+08:00"
        status, _, _ = self.post(
            "/api/schedule", {"credit_id": "credit-123", "run_at": run_at}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls, [("schedule", ("credit-123", run_at))])

    def test_schedule_rejects_invalid_argument_shapes(self):
        for data in (
            {"run_at": "2030-01-01T00:00:00Z"},
            {"credit_id": "c1"},
            {"credit_id": "c1", "run_at": 123},
            {"credit_id": "c1", "run_at": "x" * 101},
        ):
            with self.subTest(data=data):
                self.assert_error(self.post("/api/schedule", data), 400)
        self.assertEqual(self.engine.calls, [])

    def test_cancel_retry_and_reconcile_dispatch_exact_arguments(self):
        for path, action in (("/api/retry", "retry"), ("/api/reconcile", "reconcile")):
            with self.subTest(path=path):
                status, _, _ = self.post(path, {"operation_id": "op-123"})
                self.assertEqual(status, 200)
                self.assertEqual(self.engine.calls[-1], (action, ("op-123",)))
        status, _, _ = self.post("/api/schedule/cancel", {"schedule_id": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls[-1], ("cancel_schedule", ("s1",)))
        self.assertEqual(len(self.engine.calls), 3)

    def test_retry_and_reconcile_reject_invalid_argument_shapes(self):
        for path in ("/api/retry", "/api/reconcile"):
            for operation_id in (None, [], 123, "", " ", "x" * 101):
                with self.subTest(path=path, operation_id=operation_id):
                    self.assert_error(
                        self.post(path, {"operation_id": operation_id}), 400
                    )
        self.assertEqual(self.engine.calls, [])

    def test_app_error_returns_safe_message_and_declared_status(self):
        self.engine.failure = AppError("重置操作尚待核实。", status=409)
        payload = self.assert_error(self.post("/api/consume", {"credit_id": "c1"}), 409)
        self.assertEqual(payload["error"], "重置操作尚待核实。")
        self.assertEqual(self.engine.calls, [("consume", ("c1",))])

    def test_unexpected_exception_does_not_leak_details_or_credentials(self):
        self.engine.failure = RuntimeError(f"Authorization: Bearer {self.secret}")
        payload = self.assert_error(self.post("/api/refresh"), 500)
        self.assertNotIn("Authorization", payload["error"])
        self.assertNotIn("RuntimeError", payload["error"])

    def test_state_exception_does_not_leak_details_or_credentials(self):
        self.engine.state_failure = RuntimeError(self.secret)
        self.assert_error(self.request(), 500)

    def test_unknown_api_is_404_and_never_calls_engine(self):
        self.assert_error(self.post("/api/not-a-route"), 404)
        self.assertEqual(self.engine.calls, [])

    def raw_post(self, lengths, body=b"{}", truncate=False):
        headers = [
            "POST /api/refresh HTTP/1.1",
            f"Host: 127.0.0.1:{self.port}",
            "Content-Type: application/json",
            f"X-Local-Token: {self.server.csrf}",
        ]
        headers.extend(f"Content-Length: {value}" for value in lengths)
        wire = ("\r\n".join(headers) + "\r\n\r\n").encode() + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(wire)
            if truncate:
                conn.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(conn)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def test_duplicate_or_missing_content_length_never_dispatches(self):
        for lengths in ([], [2, 2], [2, 3]):
            with self.subTest(lengths=lengths):
                self.assert_error(self.raw_post(lengths), 400)
        self.assertEqual(self.engine.calls, [])

    def test_truncated_valid_json_body_never_dispatches(self):
        self.assert_error(self.raw_post([10], truncate=True), 400)
        self.assertEqual(self.engine.calls, [])

    def test_deep_json_and_nonstandard_constants_are_rejected(self):
        deep = b'{"nested":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
        for raw in (deep, b'{"value": NaN}', b'{"value": Infinity}'):
            with self.subTest(length=len(raw)):
                self.assert_error(self.post("/api/refresh", raw=raw), 400)
        self.assertEqual(self.engine.calls, [])

    def test_empty_ids_and_missing_schedule_id_never_dispatch(self):
        self.assert_error(self.post("/api/schedule/cancel"), 400)
        self.assert_error(self.post("/api/consume", {"credit_id": " "}), 400)
        self.assert_error(self.post("/api/retry", {"operation_id": ""}), 400)
        self.assertEqual(self.engine.calls, [])

    def test_new_mutation_is_rejected_while_service_shuts_down(self):
        self.server.stop_event.set()
        self.assert_error(self.post("/api/refresh"), 503)
        self.assertEqual(self.engine.calls, [])

    def test_idle_connection_times_out_before_reading_request_headers(self):
        with mock.patch.object(server, "REQUEST_TIMEOUT", 0.05):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
                self.assertEqual(conn.recv(1), b"")
        self.assertEqual(self.engine.calls, [])

    def test_duplicate_host_header_is_rejected(self):
        request = (
            f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Host: localhost:{self.port}\r\n\r\n"
        ).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn:
            conn.sendall(request)
            response = http.client.HTTPResponse(conn)
            response.begin()
            self.assertEqual(response.status, 403)
            response.read()


if __name__ == "__main__":
    unittest.main()
