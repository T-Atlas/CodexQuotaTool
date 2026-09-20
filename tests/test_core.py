import base64
import copy
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import AppError, Engine, _iso, curl_transport

NOW = 1_800_000_000


def jwt(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.before_call = None
        self.usage = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 97,
                    "limit_window_seconds": 18000,
                    "reset_at": NOW + 3600,
                },
                "secondary_window": {
                    "used_percent": 80,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 86400,
                },
            }
        }
        self.credits = {
            "available_count": 2,
            "applicable_available_count": 1,
            "credits": [
                {
                    "id": "credit-1",
                    "status": "available",
                    "reset_type": "codex_rate_limits",
                    "granted_at": _iso(NOW - 86400),
                    "expires_at": _iso(NOW + 7200),
                },
                {
                    "id": "credit-2",
                    "status": "available",
                    "reset_type": "codex_rate_limits",
                    "granted_at": _iso(NOW - 86400),
                    "expires_at": _iso(NOW + 172800),
                },
            ],
        }

    def queue(self, method, path, *responses):
        self.responses.setdefault((method, path), []).extend(responses)

    def __call__(self, method, path, headers, body):
        self.calls.append((method, path, copy.deepcopy(headers), copy.deepcopy(body)))
        if self.before_call:
            self.before_call(method, path, headers, body)
        queued = self.responses.get((method, path))
        if queued:
            result = queued.pop(0)
            if isinstance(result, BaseException):
                raise result
            return copy.deepcopy(result)
        if path == "/usage":
            return 200, copy.deepcopy(self.usage)
        if path == "/rate-limit-reset-credits":
            return 200, copy.deepcopy(self.credits)
        if path == "/rate-limit-reset-credits/consume":
            item = next(
                item
                for item in self.credits["credits"]
                if item["id"] == body["credit_id"]
            )
            item["status"] = "consumed"
            self.credits["available_count"] -= 1
            return 200, {"code": "reset", "windows_reset": 2}
        raise AssertionError("Unexpected route " + path)

    def consumes(self):
        return [
            call
            for call in self.calls
            if call[1] == "/rate-limit-reset-credits/consume"
        ]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.engine = Engine(self.root, self.transport, self.clock)
        self.auth = {
            "tokens": {
                "access_token": "private-access-token",
                "account_id": "account-1",
            }
        }
        self.engine.import_auth(self.auth)

    def tearDown(self):
        self.temp.cleanup()

    def test_state_never_queries_or_leaks_tokens(self):
        state = self.engine.state()
        self.assertTrue(state["account"]["loaded"])
        self.assertEqual(self.transport.calls, [])
        serialized = json.dumps(state)
        self.assertNotIn("private-access-token", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertEqual(stat.S_IMODE((self.root / "auth.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "state.json").stat().st_mode), 0o600)

    def test_flat_auth_and_jwt_account_extraction(self):
        self.engine.import_auth(
            {
                "access_token": "flat-secret",
                "id_token": jwt(
                    {
                        "email": "user@example.test",
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "account-2"
                        },
                    }
                ),
            }
        )
        self.assertEqual(self.engine.state()["account"]["account_id"], "account-2")
        self.assertEqual(self.engine.state()["account"]["label"], "user@example.test")

    def test_missing_account_id_is_rejected_without_overwriting_auth(self):
        before = (self.root / "auth.json").read_text()
        with self.assertRaises(AppError):
            self.engine.import_auth({"access_token": "new-token"})
        self.assertEqual((self.root / "auth.json").read_text(), before)

    def test_refresh_parses_windows_and_distinct_credit_counts(self):
        state = self.engine.refresh()
        windows = state["usage"]["windows"]
        self.assertEqual(windows[0]["name"], "5 小时")
        self.assertEqual(windows[0]["used_percent"], 97)
        self.assertEqual(windows[1]["reset_at"], _iso(NOW + 86400))
        self.assertEqual(state["credits"]["available_count"], 2)
        self.assertEqual(state["credits"]["applicable_available_count"], 1)
        self.assertNotIn("_expiry_invalid", json.dumps(state))

    def test_absent_applicable_count_stays_unknown(self):
        self.transport.credits.pop("applicable_available_count")
        state = self.engine.refresh()
        self.assertIsNone(state["credits"]["applicable_available_count"])

    def test_numeric_strings_are_normalized_without_bool_or_nan(self):
        primary = self.transport.usage["rate_limit"]["primary_window"]
        primary.update(
            used_percent="92.5", limit_window_seconds="18000", reset_at=str(NOW + 3600)
        )
        secondary = self.transport.usage["rate_limit"]["secondary_window"]
        secondary.update(used_percent=True, reset_after_seconds="86400")
        self.transport.credits["available_count"] = "2"
        self.transport.credits["applicable_available_count"] = "NaN"
        state = self.engine.refresh()
        self.assertEqual(state["usage"]["windows"][0]["used_percent"], 92.5)
        self.assertEqual(state["usage"]["windows"][0]["reset_at"], _iso(NOW + 3600))
        self.assertEqual(state["usage"]["windows"][1]["reset_at"], _iso(NOW + 86400))
        self.assertIsNone(state["usage"]["windows"][1]["used_percent"])
        self.assertEqual(state["credits"]["available_count"], 2)
        self.assertIsNone(state["credits"]["applicable_available_count"])

    def test_usage_rejects_200_error_object_and_reads_additional_limits(self):
        self.transport.usage = {"error": "unexpected"}
        self.assertIsNotNone(self.engine.refresh()["usage"]["error"])
        self.transport.usage = {
            "additional_rate_limits": [
                {
                    "limit_name": "model-a",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "limit_window_seconds": 18000,
                        }
                    },
                }
            ]
        }
        state = self.engine.refresh()
        self.assertIsNone(state["usage"]["error"])
        self.assertEqual(state["usage"]["windows"][0]["name"], "model-a · 5 小时")

    def test_partial_query_failure_preserves_other_result(self):
        self.transport.queue("GET", "/usage", AppError("network error", 502))
        state = self.engine.refresh()
        self.assertEqual(state["usage"]["error"], "network error")
        self.assertEqual(state["credits"]["available_count"], 2)
        self.assertIsNone(state["credits"]["error"])

    def test_stale_credit_data_is_labelled_and_cannot_authorize_consume(self):
        before = self.engine.refresh()["credits"]["fetched_at"]
        self.clock.now += 60
        self.transport.queue(
            "GET", "/rate-limit-reset-credits", AppError("offline", 502)
        )
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")
        self.assertEqual(self.engine.state()["credits"]["fetched_at"], before)
        self.assertEqual(self.engine.state()["credits"]["error"], "offline")
        self.assertEqual(self.transport.consumes(), [])

    def test_consume_persists_uuid_before_post_and_passes_selected_credit(self):
        def before(method, path, headers, body):
            if path.endswith("/consume"):
                persisted = json.loads((self.root / "state.json").read_text())
                operation = persisted["operations"][0]
                self.assertEqual(operation["status"], "pending")
                self.assertEqual(operation["id"], body["redeem_request_id"])
                self.assertEqual(body["credit_id"], "credit-1")
                self.assertTrue(self.engine.state()["busy"])

        self.transport.before_call = before
        state = self.engine.consume("credit-1")
        operation = state["operations"][0]
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["windows_reset"], 2)
        self.assertTrue(operation["verified"])
        self.assertFalse(state["busy"])

    def test_timeout_blocks_new_operation_retry_reuses_uuid_and_credit(self):
        self.transport.queue(
            "POST",
            "/rate-limit-reset-credits/consume",
            AppError("timeout", 502),
            (200, {"code": "already_redeemed", "windows_reset": 2}),
        )
        operation = self.engine.consume("credit-1")["operations"][0]
        self.assertEqual(operation["status"], "uncertain")
        with self.assertRaises(AppError):
            self.engine.consume("credit-2")
        retried = self.engine.retry(operation["id"])
        self.assertEqual(retried["operations"][0]["status"], "succeeded")
        self.assertEqual(
            self.transport.consumes()[0][3], self.transport.consumes()[1][3]
        )
        self.engine.retry(operation["id"])
        self.assertEqual(len(self.transport.consumes()), 2)

    def test_unknown_post_code_is_uncertain_even_for_http_200(self):
        self.transport.queue(
            "POST",
            "/rate-limit-reset-credits/consume",
            (200, {"code": "new_unknown_result"}),
        )
        operation = self.engine.consume("credit-1")["operations"][0]
        self.assertEqual(operation["status"], "uncertain")
        with self.assertRaises(AppError):
            self.engine.consume("credit-2")

    def test_known_no_consumption_outcomes_are_terminal(self):
        for code in ("no_credit", "nothing_to_reset"):
            self.transport.queue(
                "POST", "/rate-limit-reset-credits/consume", (200, {"code": code})
            )
            operation = self.engine.consume("credit-1")["operations"][0]
            self.assertEqual(operation["status"], code)
            with self.assertRaises(AppError):
                self.engine.retry(operation["id"])

    def test_success_survives_followup_query_failure_and_never_reposts(self):
        self.transport.queue("GET", "/usage", AppError("offline", 502))
        operation = self.engine.consume("credit-1")["operations"][0]
        self.assertEqual(operation["status"], "succeeded")
        self.assertFalse(operation["verified"])
        self.assertIn("兑换已确认成功", operation["message"])
        self.engine.retry(operation["id"])
        self.engine.reconcile(operation["id"])
        self.assertEqual(len(self.transport.consumes()), 1)
        self.assertTrue(self.engine.state()["operations"][0]["verified"])

    def test_success_blocks_new_uuid_for_same_credit_with_lagging_upstream(self):
        self.transport.queue(
            "POST",
            "/rate-limit-reset-credits/consume",
            (200, {"code": "reset", "windows_reset": 2}),
        )
        self.engine.consume("credit-1")
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")
        with self.assertRaises(AppError):
            self.engine.schedule("credit-1", _iso(NOW + 60))
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_malformed_expiry_is_not_treated_as_no_expiry(self):
        self.transport.credits["credits"][0]["expires_at"] = "not-a-time"
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")
        self.assertEqual(self.transport.consumes(), [])

    def test_non_codex_credit_is_not_consumable(self):
        self.transport.credits["credits"][0]["reset_type"] = "other_limits"
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")

    def test_schedule_executes_only_once_after_restart_at_due_time(self):
        scheduled = self.engine.schedule("credit-1", _iso(NOW + 600))["schedules"][-1]
        self.assertEqual(scheduled["status"], "scheduled")
        self.engine.tick()
        self.assertEqual(self.transport.consumes(), [])
        restarted = Engine(self.root, self.transport, self.clock)
        self.clock.now += 600
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(self.transport.consumes()), 1)
        state = restarted.state()
        self.assertEqual(state["schedules"][-1]["status"], "completed")
        self.assertEqual(
            state["schedules"][-1]["operation_id"], state["operations"][0]["id"]
        )

    def test_schedule_rejects_past_expiry_naive_time_and_duplicate(self):
        for when in (_iso(NOW), _iso(NOW + 7200), "2027-01-15T12:00:00"):
            with self.assertRaises(AppError):
                self.engine.schedule("credit-1", when)
        self.engine.schedule("credit-1", _iso(NOW + 600))
        with self.assertRaises(AppError):
            self.engine.schedule("credit-1", _iso(NOW + 900))

    def test_schedule_skips_when_late_unavailable_or_expired(self):
        for scenario in ("late", "unavailable", "expired"):
            with self.subTest(scenario=scenario):
                self.clock.now = NOW
                self.transport.credits["credits"][0]["status"] = "available"
                self.transport.credits["credits"][0]["expires_at"] = _iso(NOW + 7200)
                self.engine.schedule("credit-1", _iso(NOW + 60))
                self.clock.now += 60
                if scenario == "late":
                    self.clock.now += 901
                elif scenario == "unavailable":
                    self.transport.credits["credits"][0]["status"] = "consumed"
                else:
                    self.transport.credits["credits"][0]["expires_at"] = _iso(NOW + 30)
                self.engine.tick()
                self.assertEqual(
                    self.engine.state()["schedules"][-1]["status"], "skipped"
                )
                self.assertEqual(self.transport.consumes(), [])

    def test_failed_scheduled_check_does_not_retry_automatically(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.transport.queue(
            "GET", "/rate-limit-reset-credits", AppError("offline", 502)
        )
        self.clock.now += 60
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.state()["schedules"][-1]["status"], "skipped")
        self.assertEqual(self.transport.consumes(), [])

    def test_cancel_schedule_prevents_future_post(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.engine.cancel_schedule(self.engine.state()["schedules"][-1]["id"])
        self.clock.now += 60
        self.engine.tick()
        self.assertEqual(self.engine.state()["schedules"][-1]["status"], "cancelled")
        self.assertEqual(self.transport.consumes(), [])

    def test_restart_pending_and_running_becomes_uncertain_without_post(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.clock.now += 60
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", KeyboardInterrupt()
        )
        with self.assertRaises(KeyboardInterrupt):
            self.engine.tick()
        restarted = Engine(self.root, self.transport, self.clock)
        self.assertEqual(restarted.state()["operations"][0]["status"], "uncertain")
        self.assertEqual(restarted.state()["schedules"][-1]["status"], "uncertain")
        restarted.tick()
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_different_account_requires_cancelling_future_job_and_resolving_operation(
        self,
    ):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        with self.assertRaises(AppError):
            self.engine.import_auth(
                {"access_token": "other-token", "account_id": "account-2"}
            )
        self.engine.cancel_schedule(self.engine.state()["schedules"][-1]["id"])
        self.engine.import_auth(
            {"access_token": "other-token", "account_id": "account-2"}
        )
        self.clock.now += 60
        self.engine.tick()
        self.assertEqual(self.transport.consumes(), [])
        self.assertEqual(self.engine.state()["schedules"], [])
        self.engine.import_auth(self.auth)
        self.assertEqual(self.engine.state()["schedules"][-1]["status"], "cancelled")
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", AppError("uncertain")
        )
        self.engine.consume("credit-1")
        with self.assertRaises(AppError):
            self.engine.import_auth(
                {"access_token": "other-token", "account_id": "account-2"}
            )

    def test_external_auth_change_cannot_run_bound_job(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        (self.root / "auth.json").write_text(
            json.dumps({"access_token": "other", "account_id": "account-2"})
        )
        restarted = Engine(self.root, self.transport, self.clock)
        self.clock.now += 60
        restarted.tick()
        saved = json.loads((self.root / "state.json").read_text())
        self.assertEqual(saved["schedules"][-1]["status"], "scheduled")
        self.assertIn("其他账号", restarted.state()["account"]["error"])
        self.assertEqual(self.transport.consumes(), [])

    def test_multiple_schedules_survive_restart_and_each_run_once(self):
        first = self.engine.schedule("credit-1", _iso(NOW + 60))["schedules"][-1]
        second = self.engine.schedule("credit-2", _iso(NOW + 120))["schedules"][-1]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.engine.state()["schedules"]), 2)
        restarted = Engine(self.root, self.transport, self.clock)
        self.clock.now += 120
        restarted.tick()
        restarted.tick()
        state = restarted.state()
        self.assertEqual(
            [job["status"] for job in state["schedules"]], ["completed", "completed"]
        )
        self.assertNotIn("schedule", state)
        self.assertNotIn("schedule", json.loads((self.root / "state.json").read_text()))
        self.assertEqual(
            [call[3]["credit_id"] for call in self.transport.consumes()],
            ["credit-1", "credit-2"],
        )
        self.assertEqual(
            len({call[3]["redeem_request_id"] for call in self.transport.consumes()}), 2
        )

    def test_cancelling_one_schedule_keeps_the_other(self):
        first = self.engine.schedule("credit-1", _iso(NOW + 60))["schedules"][-1]
        self.engine.schedule("credit-2", _iso(NOW + 120))
        self.engine.cancel_schedule(first["id"])
        with self.assertRaises(AppError):
            self.engine.cancel_schedule("missing")
        self.clock.now += 120
        self.engine.tick()
        self.assertEqual(
            [job["status"] for job in self.engine.state()["schedules"]],
            ["cancelled", "completed"],
        )
        self.assertEqual(
            [call[3]["credit_id"] for call in self.transport.consumes()], ["credit-2"]
        )

    def test_manual_consume_cancels_only_matching_credit_schedule(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.engine.schedule("credit-2", _iso(NOW + 120))
        self.engine.consume("credit-1")
        self.assertEqual(
            [job["status"] for job in self.engine.state()["schedules"]],
            ["cancelled", "scheduled"],
        )
        self.clock.now += 120
        self.engine.tick()
        self.assertEqual(len(self.transport.consumes()), 2)

    def test_uncertain_schedule_stops_other_due_jobs_until_original_retry(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.engine.schedule("credit-2", _iso(NOW + 120))
        self.transport.queue(
            "POST",
            "/rate-limit-reset-credits/consume",
            AppError("unknown"),
            (200, {"code": "already_redeemed"}),
        )
        self.clock.now += 120
        self.engine.tick()
        self.engine.tick()
        state = self.engine.state()
        self.assertEqual(
            [job["status"] for job in state["schedules"]], ["uncertain", "scheduled"]
        )
        self.assertEqual(len(self.transport.consumes()), 1)
        with self.assertRaises(AppError):
            self.engine.schedule("credit-2", _iso(NOW + 240))
        self.engine.retry(state["operations"][0]["id"])
        self.engine.tick()
        self.assertEqual(
            [job["status"] for job in self.engine.state()["schedules"]],
            ["completed", "completed"],
        )
        self.assertEqual(
            self.transport.consumes()[0][3], self.transport.consumes()[1][3]
        )

    def test_saved_state_requires_current_schema(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        saved = json.loads((self.root / "state.json").read_text())
        saved["schedule"] = saved.pop("schedules")[0]
        (self.root / "state.json").write_text(json.dumps(saved))
        with self.assertRaises(AppError):
            Engine(self.root, self.transport, self.clock)
        self.assertEqual(self.transport.consumes(), [])

    def test_restart_preserves_other_jobs_when_running_job_becomes_uncertain(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.engine.schedule("credit-2", _iso(NOW + 120))
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", KeyboardInterrupt()
        )
        self.clock.now += 120
        with self.assertRaises(KeyboardInterrupt):
            self.engine.tick()
        restarted = Engine(self.root, self.transport, self.clock)
        restarted.tick()
        self.assertEqual(
            [job["status"] for job in restarted.state()["schedules"]],
            ["uncertain", "scheduled"],
        )
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_import_rejects_mismatched_account_in_either_jwt(self):
        before = (self.root / "auth.json").read_text()
        for name in ("access_token", "id_token"):
            document = copy.deepcopy(self.auth)
            document["tokens"][name] = jwt(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "other"}}
            )
            with self.subTest(token=name), self.assertRaises(AppError):
                self.engine.import_auth(document)
        with self.assertRaises(AppError):
            self.engine.import_auth(
                {
                    "access_token": jwt(
                        {"https://api.openai.com/auth": {"chatgpt_account_id": "one"}}
                    ),
                    "id_token": jwt(
                        {"https://api.openai.com/auth": {"chatgpt_account_id": "two"}}
                    ),
                }
            )
        self.assertEqual((self.root / "auth.json").read_text(), before)

    def test_non_2xx_success_or_terminal_codes_are_uncertain(self):
        for code in ("reset", "already_redeemed", "nothing_to_reset", "no_credit"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                transport = FakeTransport()
                engine = Engine(Path(directory), transport, self.clock)
                engine.import_auth(self.auth)
                transport.queue(
                    "POST", "/rate-limit-reset-credits/consume", (500, {"code": code})
                )
                self.assertEqual(
                    engine.consume("credit-1")["operations"][0]["status"], "uncertain"
                )

    def test_state_detects_external_same_account_token_and_never_queries(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.transport.calls.clear()
        (self.root / "auth.json").write_text(
            json.dumps({"access_token": "replacement", "account_id": "account-1"})
        )
        self.assertNotIn("error", self.engine.state()["account"])
        self.assertEqual(self.transport.calls, [])
        self.engine.refresh()
        self.assertTrue(
            all(
                call[2]["Authorization"] == "Bearer replacement"
                for call in self.transport.calls
            )
        )

    def test_refresh_loads_auth_file_dropped_after_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine(root, self.transport, self.clock)
            self.assertFalse(engine.state()["account"]["loaded"])
            (root / "auth.json").write_text(json.dumps(self.auth))
            self.assertTrue(engine.refresh()["account"]["loaded"])
            self.assertEqual(len(self.transport.calls), 2)

    def test_external_account_change_keeps_old_identity_and_blocks_until_restored(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.transport.calls.clear()
        wrong = {"access_token": "wrong", "account_id": "account-2"}
        (self.root / "auth.json").write_text(json.dumps(wrong))
        state = self.engine.state()
        self.assertEqual(state["account"]["account_id"], "account-1")
        self.assertIn("error", state["account"])
        with self.assertRaises(AppError):
            self.engine.refresh()
        self.clock.now += 60
        self.engine.tick()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(json.loads((self.root / "auth.json").read_text()), wrong)
        (self.root / "auth.json").write_text(json.dumps(self.auth))
        self.engine.reload_auth()
        self.engine.tick()
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_cancelling_active_jobs_allows_external_account_switch(self):
        scheduled = self.engine.schedule("credit-1", _iso(NOW + 60))["schedules"][-1]
        (self.root / "auth.json").write_text(
            json.dumps({"access_token": "other", "account_id": "account-2"})
        )
        self.assertIn("error", self.engine.state()["account"])
        state = self.engine.cancel_schedule(scheduled["id"])
        self.assertEqual(state["account"]["account_id"], "account-2")
        self.assertEqual(state["schedules"], [])

    def test_refresh_never_overwrites_auth_replaced_during_token_rotation(self):
        self.engine.import_auth(
            {
                "access_token": "old",
                "refresh_token": "old-refresh",
                "account_id": "account-1",
                "expires_at": _iso(NOW - 60),
            }
        )
        replacement = {"access_token": "external-token", "account_id": "account-1"}

        def replace_during_oauth(method, path, headers, body):
            if path == "/oauth/token":
                (self.root / "auth.json").write_text(json.dumps(replacement))
                self.assertTrue(self.engine.state()["busy"])
                self.assertEqual(self.engine._auth["access_token"], "old")

        self.transport.before_call = replace_during_oauth
        self.transport.queue(
            "POST",
            "/oauth/token",
            (200, {"access_token": "rotated", "refresh_token": "new-refresh"}),
        )
        state = self.engine.refresh()
        self.assertIn("操作期间发生变化", state["usage"]["error"])
        self.assertEqual(json.loads((self.root / "auth.json").read_text()), replacement)
        self.assertEqual([call[1] for call in self.transport.calls], ["/oauth/token"])
        self.engine.refresh()
        self.assertEqual(
            self.transport.calls[-1][2]["Authorization"], "Bearer external-token"
        )

    def test_oversized_auth_file_is_rejected_and_cannot_authorize_queries(self):
        (self.root / "auth.json").write_bytes(b" " * (1024 * 1024 + 1))
        self.assertIn("1 MB", self.engine.state()["account"]["error"])
        with self.assertRaises(AppError):
            self.engine.refresh()
        self.assertEqual(self.transport.calls, [])

    def test_reconcile_queries_only_and_keeps_ambiguity(self):
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", AppError("uncertain")
        )
        operation = self.engine.consume("credit-1")["operations"][0]
        state = self.engine.reconcile(operation["id"])
        self.assertEqual(state["operations"][0]["status"], "uncertain")
        self.transport.credits["credits"][0]["status"] = "consumed"
        state = self.engine.reconcile(operation["id"])
        self.assertEqual(state["operations"][0]["status"], "succeeded")
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_invalid_state_file_fails_closed(self):
        (self.root / "state.json").write_text("{broken")
        with self.assertRaises(AppError):
            Engine(self.root, self.transport, self.clock)

    def test_corrupt_operation_and_schedule_records_fail_closed(self):
        self.engine.schedule("credit-1", _iso(NOW + 60))
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", KeyboardInterrupt()
        )
        self.clock.now += 60
        with self.assertRaises(KeyboardInterrupt):
            self.engine.tick()
        original = json.loads((self.root / "state.json").read_text())
        mutations = {
            "missing account": lambda data: data["operations"][0].pop("_account_id"),
            "invalid operation status": lambda data: data["operations"][0].update(
                status="unknown"
            ),
            "invalid operation id": lambda data: data["operations"][0].update(
                id="not-a-uuid"
            ),
            "duplicate operation": lambda data: data["operations"].append(
                copy.deepcopy(data["operations"][0])
            ),
            "invalid schedule time": lambda data: data["schedules"][0].update(
                run_at="bad"
            ),
            "naive schedule time": lambda data: data["schedules"][0].update(
                run_at="2027-01-15T12:00:00"
            ),
            "missing operation": lambda data: data["schedules"][0].update(
                operation_id=None
            ),
            "foreign operation account": lambda data: data["operations"][0].update(
                _account_id="other"
            ),
            "foreign operation credit": lambda data: data["operations"][0].update(
                credit_id="other"
            ),
            "inconsistent status": lambda data: data["operations"][0].update(
                status="succeeded"
            ),
            "duplicate schedule": lambda data: data["schedules"].append(
                copy.deepcopy(data["schedules"][0])
            ),
            "malformed cached response": lambda data: data.update(credits=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                saved = copy.deepcopy(original)
                mutate(saved)
                encoded = json.dumps(saved)
                (self.root / "state.json").write_text(encoded)
                with self.assertRaises(AppError):
                    Engine(self.root, self.transport, self.clock)
                self.assertEqual((self.root / "state.json").read_text(), encoded)
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_final_submission_check_stops_expired_manual_credit(self):
        original_save = self.engine._save

        def delayed_save():
            original_save()
            if self.engine._data["operations"]:
                self.clock.now = NOW + 7200

        with patch.object(self.engine, "_save", side_effect=delayed_save):
            state = self.engine.consume("credit-1")
        operation = state["operations"][0]
        self.assertEqual(operation["status"], "not_sent")
        self.assertEqual(self.transport.consumes(), [])
        self.assertEqual(
            Engine(self.root, self.transport, self.clock).state()["operations"][0][
                "status"
            ],
            "not_sent",
        )
        with self.assertRaises(AppError):
            self.engine.retry(operation["id"])

    def test_final_submission_check_stops_schedule_after_slow_persistence(self):
        for new_time in (NOW + 60 + 901, NOW):
            with (
                self.subTest(new_time=new_time),
                tempfile.TemporaryDirectory() as directory,
            ):
                clock = Clock()
                transport = FakeTransport()
                engine = Engine(Path(directory), transport, clock)
                engine.import_auth(self.auth)
                engine.schedule("credit-1", _iso(NOW + 60))
                clock.now += 60
                original_save = engine._save

                def delayed_save():
                    original_save()
                    if engine._data["operations"]:
                        clock.now = new_time

                with patch.object(engine, "_save", side_effect=delayed_save):
                    engine.tick()
                state = engine.state()
                self.assertEqual(state["operations"][0]["status"], "not_sent")
                self.assertEqual(state["schedules"][0]["status"], "skipped")
                self.assertEqual(transport.consumes(), [])
                self.assertEqual(
                    Engine(Path(directory), transport, clock).state()["schedules"][0][
                        "status"
                    ],
                    "skipped",
                )

    def test_duplicate_upstream_credit_ids_cannot_authorize_consume(self):
        duplicate = copy.deepcopy(self.transport.credits["credits"][0])
        duplicate["status"] = "consumed"
        self.transport.credits["credits"].append(duplicate)
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")
        self.assertEqual(self.transport.consumes(), [])

    def test_auth_replaced_after_atomic_write_is_not_misidentified_as_loaded(self):
        original_atomic = __import__("core")._atomic_json
        replacement = {"access_token": "external", "account_id": "account-1"}

        def replaced_write(path, document, **kwargs):
            stamp = original_atomic(path, document, **kwargs)
            if path.name == "auth.json":
                path.write_text(json.dumps(replacement))
            return stamp

        with patch("core._atomic_json", side_effect=replaced_write):
            with self.assertRaises(AppError):
                self.engine.import_auth(
                    {"access_token": "imported", "account_id": "account-1"}
                )
        self.assertEqual(self.engine._auth["access_token"], "private-access-token")
        self.assertEqual(json.loads((self.root / "auth.json").read_text()), replacement)
        self.engine.refresh()
        self.assertTrue(
            all(
                call[2]["Authorization"] == "Bearer external"
                for call in self.transport.calls
            )
        )

    def test_rotation_preserves_auth_replaced_while_new_token_is_flushed(self):
        self.engine.import_auth(
            {
                "access_token": "old",
                "refresh_token": "refresh",
                "account_id": "account-1",
                "expires_at": _iso(NOW - 60),
            }
        )
        replacement = {"access_token": "external", "account_id": "account-1"}
        self.transport.queue(
            "POST",
            "/oauth/token",
            (200, {"access_token": "rotated", "expires_in": 3600}),
        )
        original_fsync = os.fsync
        replaced = False

        def replace_during_flush(descriptor):
            nonlocal replaced
            if not replaced:
                replaced = True
                (self.root / "auth.json").write_text(json.dumps(replacement))
            original_fsync(descriptor)

        with patch("core.os.fsync", side_effect=replace_during_flush):
            self.engine.refresh()
        self.assertEqual(json.loads((self.root / "auth.json").read_text()), replacement)
        self.assertEqual([call[1] for call in self.transport.calls], ["/oauth/token"])

    def test_nested_auth_requires_complete_tokens_object(self):
        for document in (
            {"tokens": None, "access_token": "token", "account_id": "account-1"},
            {"tokens": {"account_id": "account-1"}, "access_token": "token"},
            {
                "tokens": {"access_token": "token", "account_id": "account-1"},
                "account_id": "other",
            },
        ):
            with self.subTest(document=document), self.assertRaises(AppError):
                self.engine.import_auth(document)

    def test_result_requires_canonical_code_field(self):
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", (200, {"status": "reset"})
        )
        operation = self.engine.consume("credit-1")["operations"][0]
        self.assertEqual(operation["status"], "uncertain")
        self.assertEqual(len(self.transport.consumes()), 1)

    def test_read_state_during_network_is_nonblocking_and_other_mutations_rejected(
        self,
    ):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def before(method, path, headers, body):
            if path == "/usage":
                entered.set()
                release.wait(5)

        def run():
            try:
                self.engine.refresh()
            except BaseException as error:
                errors.append(error)

        self.transport.before_call = before
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.engine.state()["busy"])
            with self.assertRaises(AppError):
                self.engine.consume("credit-1")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_expired_access_refresh_persists_rotated_token_before_usage(self):
        self.engine.import_auth(
            {
                "tokens": {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "account_id": "account-1",
                    "expires_at": _iso(NOW - 60),
                }
            }
        )
        self.transport.queue(
            "POST",
            "/oauth/token",
            (
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            ),
        )

        def before(method, path, headers, body):
            if path != "/oauth/token":
                saved = json.loads((self.root / "auth.json").read_text())
                self.assertEqual(saved["tokens"]["refresh_token"], "new-refresh")
                self.assertEqual(headers["Authorization"], "Bearer new-access")

        self.transport.before_call = before
        state = self.engine.refresh()
        self.assertIsNone(state["usage"]["error"])
        self.assertNotIn("new-refresh", json.dumps(state))
        self.assertEqual(
            len([call for call in self.transport.calls if call[1] == "/oauth/token"]), 1
        )

    def test_401_refresh_retries_get_once_and_consumes_with_new_token(self):
        self.engine.import_auth(
            {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "account_id": "account-1",
            }
        )
        self.transport.queue(
            "GET", "/rate-limit-reset-credits", (401, {"error": "expired"})
        )
        self.transport.queue(
            "POST",
            "/oauth/token",
            (
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            ),
        )
        state = self.engine.consume("credit-1")
        self.assertEqual(state["operations"][0]["status"], "succeeded")
        self.assertEqual(
            self.transport.consumes()[0][2]["Authorization"], "Bearer new-access"
        )
        self.assertEqual(
            json.loads((self.root / "auth.json").read_text())["refresh_token"],
            "new-refresh",
        )

    def test_failed_or_unsaved_refresh_never_consumes(self):
        self.engine.import_auth(
            {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "account_id": "account-1",
                "expires_at": _iso(NOW - 60),
            }
        )
        self.transport.queue("POST", "/oauth/token", (400, {"error": "invalid_grant"}))
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")
        self.assertEqual(self.transport.consumes(), [])
        self.transport.queue(
            "POST",
            "/oauth/token",
            (
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            ),
        )
        original_write = __import__("core")._atomic_json

        def fail_auth(path, document, **kwargs):
            if path.name == "auth.json":
                raise PermissionError("read only")
            return original_write(path, document, **kwargs)

        with patch("core._atomic_json", side_effect=fail_auth):
            with self.assertRaises(AppError):
                self.engine.consume("credit-1")
        self.assertEqual(self.transport.consumes(), [])
        self.assertEqual(
            json.loads((self.root / "auth.json").read_text())["access_token"],
            "old-access",
        )

    def test_refresh_cannot_switch_accounts(self):
        self.engine.import_auth(
            {
                "access_token": "old",
                "refresh_token": "refresh",
                "account_id": "account-1",
                "expires_at": _iso(NOW - 60),
            }
        )
        other_token = jwt(
            {
                "https://api.openai.com/auth": {"chatgpt_account_id": "account-2"},
                "exp": NOW + 3600,
            }
        )
        self.transport.queue(
            "POST",
            "/oauth/token",
            (200, {"access_token": other_token, "refresh_token": "new"}),
        )
        with self.assertRaises(AppError):
            self.engine.consume("credit-1")
        self.assertEqual(self.engine.state()["account"]["account_id"], "account-1")
        self.assertEqual(self.transport.consumes(), [])

    def test_post_401_never_automatically_refreshes_or_reposts(self):
        self.engine.import_auth(
            {
                "access_token": "old",
                "refresh_token": "refresh",
                "account_id": "account-1",
            }
        )
        self.transport.queue(
            "POST", "/rate-limit-reset-credits/consume", (401, {"error": "expired"})
        )
        operation = self.engine.consume("credit-1")["operations"][0]
        self.assertEqual(operation["status"], "uncertain")
        self.assertEqual(len(self.transport.consumes()), 1)
        self.assertFalse(
            any(call[1] == "/oauth/token" for call in self.transport.calls)
        )

    def test_retry_refreshes_expired_auth_without_requiring_available_credit(self):
        self.engine.import_auth(
            {
                "access_token": "old",
                "refresh_token": "refresh",
                "account_id": "account-1",
                "expires_at": _iso(NOW + 60),
            }
        )
        self.transport.queue(
            "POST",
            "/rate-limit-reset-credits/consume",
            AppError("uncertain"),
            (200, {"code": "already_redeemed", "windows_reset": 2}),
        )
        operation = self.engine.consume("credit-1")["operations"][0]
        self.clock.now += 120
        self.transport.credits["credits"][0]["status"] = "consumed"
        self.transport.queue(
            "POST",
            "/oauth/token",
            (
                200,
                {
                    "access_token": "new",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            ),
        )
        state = self.engine.retry(operation["id"])
        self.assertEqual(state["operations"][0]["status"], "succeeded")
        self.assertEqual(
            self.transport.consumes()[0][3], self.transport.consumes()[1][3]
        )
        self.assertEqual(self.transport.consumes()[1][2]["Authorization"], "Bearer new")

    def test_default_transport_never_places_secrets_in_process_arguments(self):
        result = type(
            "Result",
            (),
            {"returncode": 0, "stdout": '{"available_count":0,"credits":[]}\n200'},
        )()
        with (
            patch("core.shutil.which", return_value="/usr/bin/curl"),
            patch("core.subprocess.run", return_value=result) as run,
        ):
            status, body = curl_transport(
                "GET",
                "/rate-limit-reset-credits",
                {"Authorization": "Bearer secret"},
                None,
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/curl", "-q", "--config", "-"])
        self.assertNotIn("secret", str(args))
        self.assertIn("secret", kwargs["input"])
        self.assertIn("max-redirs = 0", kwargs["input"])
        self.assertEqual(status, 200)
        self.assertEqual(body["available_count"], 0)


if __name__ == "__main__":
    unittest.main()
