"""Validate launcher ownership, lifecycle, and command parsing offline."""

import fcntl
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import manage


def metadata(directory, **updates):
    value = {
        "app": "CodexQuotaTool",
        "root": str(directory.resolve()),
        "port": 8765,
        "pid": 123,
        "demo": False,
        "instance_id": "a" * 64,
    }
    value.update(updates)
    return value


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patch = mock.patch.object(manage, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def write_runtime(self, value):
        (self.root / ".runtime.json").write_text(json.dumps(value))

    def test_missing_and_malformed_runtime_are_ignored(self):
        self.assertIsNone(manage.runtime())
        (self.root / ".runtime.json").write_text("{")
        self.assertIsNone(manage.runtime())

    def test_non_object_runtime_is_ignored(self):
        for value in (None, [], [1], "metadata", 123, True):
            with self.subTest(value=value):
                self.write_runtime(value)
                self.assertIsNone(manage.runtime())

    def test_invalid_ports_are_ignored(self):
        for port in (None, "8765", True, 0, -1, 1023, 65536, 8765.0, [], {}):
            with self.subTest(port=port):
                self.write_runtime(metadata(self.root, port=port))
                self.assertIsNone(manage.runtime())

    def test_invalid_process_ids_are_ignored(self):
        for pid in (None, "123", True, 0, -1, 1.0, [], {}):
            with self.subTest(pid=pid):
                self.write_runtime(metadata(self.root, pid=pid))
                self.assertIsNone(manage.runtime())

    def test_valid_runtime_and_port_boundaries_are_preserved(self):
        for port in (1024, 8765, 65535):
            with self.subTest(port=port):
                value = metadata(self.root, port=port)
                self.write_runtime(value)
                self.assertEqual(manage.runtime(), value)

    def test_runtime_requires_full_current_schema(self):
        value = metadata(self.root)
        for field in value:
            with self.subTest(field=field):
                incomplete = dict(value)
                del incomplete[field]
                self.write_runtime(incomplete)
                self.assertIsNone(manage.runtime())

    def test_runtime_rejects_other_directory_mode_or_instance_format(self):
        for updates in (
            {"root": "/other-project"},
            {"demo": True},
            {"demo": 0},
            {"app": "another-app"},
            {"instance_id": ""},
            {"instance_id": "g" * 64},
            {"instance_id": []},
        ):
            with self.subTest(updates=updates):
                self.write_runtime(metadata(self.root, **updates))
                self.assertIsNone(manage.runtime())

    def test_unreadable_runtime_is_ignored(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(manage.runtime())

    def test_health_requires_matching_instance_not_just_reused_pid(self):
        info = metadata(self.root)
        healthy = {
            "app": "CodexQuotaTool",
            "pid": 123,
            "demo": False,
            "instance_id": info["instance_id"],
        }
        for updates in (
            {"pid": 456},
            {"demo": True},
            {"instance_id": "b" * 64},
            {"instance_id": None},
        ):
            with (
                self.subTest(updates=updates),
                mock.patch.object(
                    manage,
                    "request",
                    return_value=dict(healthy, **updates),
                ),
            ):
                self.assertFalse(manage.is_running(info))
        with mock.patch.object(manage, "request", return_value=healthy):
            self.assertTrue(manage.is_running(info))

    def test_lock_check_does_not_create_files_and_detects_existing_instance(self):
        self.assertFalse(manage.service_locked())
        path = self.root / ".server.lock"
        self.assertFalse(path.exists())
        with path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(manage.service_locked())
        self.assertFalse(manage.service_locked())

    def test_demo_runtime_is_kept_in_its_own_data_root(self):
        root = manage.runtime_root(True)
        root.mkdir(parents=True)
        value = metadata(root, demo=True)
        (root / ".runtime.json").write_text(json.dumps(value))
        self.assertEqual(manage.runtime(True), value)
        self.assertIsNone(manage.runtime())


class ChoosePortTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(manage.os.environ, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_socket(self, occupied=False):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        if occupied:
            sock.bind.side_effect = OSError("Address in use")
        return sock

    def test_explicit_port_is_used_without_probing_another_port(self):
        for demo, variable in (
            (False, "CODEX_QUOTA_PORT"),
            (True, "CODEX_QUOTA_DEMO_PORT"),
        ):
            with (
                self.subTest(demo=demo),
                mock.patch.dict(
                    manage.os.environ,
                    {variable: "9000"},
                ),
                mock.patch.object(manage.socket, "socket") as socket_factory,
            ):
                self.assertEqual(manage.choose_port(demo), "9000")
                socket_factory.assert_not_called()

    def test_invalid_explicit_port_fails_without_default_search(self):
        for port in ("abc", "8765.0", " ", "0", "-1", "1023", "65536"):
            with (
                self.subTest(port=port),
                mock.patch.dict(
                    manage.os.environ,
                    {"CODEX_QUOTA_PORT": port},
                ),
                mock.patch.object(manage.socket, "socket") as socket_factory,
            ):
                with self.assertRaises(SystemExit):
                    manage.choose_port()
                socket_factory.assert_not_called()

    def test_defaults_choose_first_free_loopback_port_for_each_mode(self):
        for demo, port in ((False, 8765), (True, 8785)):
            sock = self.fake_socket()
            with (
                self.subTest(demo=demo),
                mock.patch.object(
                    manage.socket,
                    "socket",
                    return_value=sock,
                ),
            ):
                self.assertEqual(manage.choose_port(demo), str(port))
            sock.bind.assert_called_once_with(("127.0.0.1", port))
            sock.__exit__.assert_called_once()

    def test_occupied_defaults_are_skipped_and_probes_are_closed(self):
        probes = [self.fake_socket(True), self.fake_socket(True), self.fake_socket()]
        with mock.patch.object(manage.socket, "socket", side_effect=probes):
            self.assertEqual(manage.choose_port(), "8767")
        for port, sock in zip(range(8765, 8768), probes):
            sock.bind.assert_called_once_with(("127.0.0.1", port))
            sock.__exit__.assert_called_once()

    def test_exhausted_default_range_has_actionable_error(self):
        probes = [self.fake_socket(True) for _ in range(21)]
        with mock.patch.object(manage.socket, "socket", side_effect=probes):
            with self.assertRaisesRegex(SystemExit, "CODEX_QUOTA_PORT"):
                manage.choose_port()
        for sock in probes:
            sock.__exit__.assert_called_once()

    def test_production_port_never_configures_demo(self):
        sock = self.fake_socket()
        with (
            mock.patch.dict(manage.os.environ, {"CODEX_QUOTA_PORT": "9000"}),
            mock.patch.object(manage.socket, "socket", return_value=sock),
        ):
            self.assertEqual(manage.choose_port(True), "8785")
        sock.bind.assert_called_once_with(("127.0.0.1", 8785))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(manage.os.environ, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_cli_uses_one_command_set_with_separate_demo_flag(self):
        args = manage.parse_arguments(["restart", "--demo", "--no-open"])
        self.assertEqual(args.command, "restart")
        self.assertTrue(args.demo)
        self.assertTrue(args.no_open)
        self.assertEqual(manage.parse_arguments([]).command, "start")

    def test_restart_waits_for_shutdown_and_keeps_current_port(self):
        old = {"port": 8766, "pid": 11}
        new = {"port": 8766, "pid": 22}
        with (
            mock.patch.object(
                manage.sys, "argv", ["manage.py", "restart", "--no-open"]
            ),
            mock.patch.object(manage, "runtime", return_value=old),
            mock.patch.object(manage, "is_running", return_value=True),
            mock.patch.object(manage, "stop_service") as stop,
            mock.patch.object(manage, "wait_stopped") as wait,
            mock.patch.object(manage, "start_service", return_value=new) as start,
            mock.patch.object(manage, "choose_port") as choose,
            mock.patch.object(manage.webbrowser, "open") as browser,
            mock.patch("builtins.print"),
        ):
            manage.main()
        stop.assert_called_once_with(old)
        wait.assert_called_once_with(False)
        start.assert_called_once_with(False, "8766")
        choose.assert_not_called()
        browser.assert_not_called()

    def test_shutdown_wait_checks_lock_even_after_metadata_disappears(self):
        with (
            mock.patch.object(
                manage, "service_locked", side_effect=[True, False]
            ) as locked,
            mock.patch.object(manage.time, "sleep") as pause,
        ):
            manage.wait_stopped()
        self.assertEqual(locked.call_count, 2)
        pause.assert_called_once_with(0.1)

    def test_unhealthy_locked_service_does_not_start_second_instance(self):
        with (
            mock.patch.object(manage.sys, "argv", ["manage.py", "start"]),
            mock.patch.object(manage, "runtime", return_value=None),
            mock.patch.object(manage, "service_locked", return_value=True),
            mock.patch.object(manage, "start_service") as start,
        ):
            with self.assertRaisesRegex(SystemExit, "未启动其他实例"):
                manage.main()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
