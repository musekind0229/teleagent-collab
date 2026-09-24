"""Read-only desktop readiness gate. Never claims a lock or posts a session."""
from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from desktop_lock_isolation import install_desktop_lock_isolation
from framework.app_service import assess_win_gui_readiness, probe_win_gui_connection

ROOT = Path(__file__).resolve().parents[1]


def _load_collab_service():
    path = ROOT / "bin" / "collab-service.py"
    spec = importlib.util.spec_from_file_location("collab_service_bin_ready", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load collab-service")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Presence:
    source = "process_env"
    password_present = True
    session_key_present = True
    blocker = None
    openprocess_denied_count = 0
    environ_readable_without_secrets = 0


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.base = ""
        self.creds = None

    def call(self, method, path, body=None, workspace=None):
        self.calls.append((method, path, body))
        if method != "GET" or path != "/session/status" or body is not None:
            raise AssertionError(f"not read-only: {method} {path} {body!r}")
        return {}


class ReadyGateTests(unittest.TestCase):
    def setUp(self):
        install_desktop_lock_isolation(self)

    def _assess(self, status, **kwargs):
        calls = []

        def fetch(base):
            calls.append(("GET", "/session/status", base))
            if callable(status):
                return status(base)
            return status

        with mock.patch(
            "win_collab.desktop_lock.claim_desktop",
            side_effect=AssertionError("claim_desktop"),
        ), mock.patch(
            "teleagent_adapter.windows_stdin_wrap.ensure_stdin_wrap",
            side_effect=AssertionError("stdin_wrap"),
        ):
            result = assess_win_gui_readiness(
                simulated=True,
                simulate_status="ok",
                session_status_fn=fetch,
                lock_holder_fn=kwargs.get("lock_holder_fn", lambda base: None),
            )
        self.assertEqual(calls, [("GET", "/session/status", "http://127.0.0.1:4399")])
        self.assertEqual(result["gui"]["status"], "ok")
        return result

    def test_idle_empty_map_is_ready(self):
        result = self._assess({})
        self.assertTrue(result["ready"])
        self.assertTrue(result["dispatch_allowed"])
        self.assertEqual(result["occupancy"]["state"], "idle")
        self.assertEqual(result["session_count"], 0)
        self.assertEqual(result["session_types"], {})
        self.assertEqual(result["reasons"], [])
        self.assertIsNone(result["lock_holder"])
        self.assertNotIn("不要派工", json.dumps(result, ensure_ascii=False))

    def test_idle_sessions_are_ready(self):
        result = self._assess({"a": {"type": "idle"}, "b": {"type": "idle"}})
        self.assertTrue(result["ready"])
        self.assertEqual(result["occupancy"]["state"], "idle")
        self.assertEqual(result["session_count"], 2)
        self.assertEqual(result["session_types"], {"idle": 2})

    def test_busy_is_not_ready_and_says_do_not_dispatch(self):
        result = self._assess({"sess-1": {"type": "busy"}})
        self.assertFalse(result["ready"])
        self.assertFalse(result["dispatch_allowed"])
        self.assertEqual(result["occupancy"]["state"], "busy")
        self.assertEqual(result["gui"]["status"], "ok")
        self.assertEqual(result["session_types"], {"busy": 1})
        text = "\n".join(result["reasons"])
        self.assertIn("DO NOT DISPATCH", text)
        self.assertIn("不要派工", text)

    def test_running_counts_as_busy(self):
        result = self._assess({"sess-1": {"type": "running"}})
        self.assertEqual(result["occupancy"]["state"], "busy")
        self.assertFalse(result["dispatch_allowed"])
        self.assertEqual(result["session_types"], {"running": 1})
        self.assertIn("不要派工", "\n".join(result["reasons"]))

    def test_mixed_idle_and_busy_is_busy(self):
        result = self._assess({"a": {"type": "idle"}, "b": {"type": "busy"}})
        self.assertEqual(result["occupancy"]["state"], "busy")
        self.assertFalse(result["ready"])
        self.assertEqual(result["session_count"], 2)

    def test_unknown_shape_is_not_ready(self):
        result = self._assess({"error": "nope"})
        self.assertEqual(result["occupancy"]["state"], "unknown")
        self.assertFalse(result["ready"])
        self.assertFalse(result["dispatch_allowed"])
        text = "\n".join(result["reasons"])
        self.assertIn("DO NOT DISPATCH", text)
        self.assertIn("不要派工", text)

    def test_api_failure_is_unknown(self):
        def boom(_base):
            raise RuntimeError("TeleAgent GET /session/status returned HTTP 500")

        result = self._assess(boom)
        self.assertEqual(result["occupancy"]["state"], "unknown")
        self.assertEqual(result["session_count"], 0)
        self.assertFalse(result["ready"])
        self.assertFalse(result["dispatch_allowed"])
        text = "\n".join(result["reasons"])
        self.assertIn("不要派工", text)
        self.assertIn("DO NOT DISPATCH", text)
        self.assertIn("HTTP 500", text)

    def test_gui_not_ok_does_not_read_session(self):
        def fetch(_base):
            raise AssertionError("session read")

        with mock.patch(
            "win_collab.desktop_lock.claim_desktop",
            side_effect=AssertionError("claim_desktop"),
        ):
            result = assess_win_gui_readiness(
                simulated=True,
                simulate_status="not_running",
                session_status_fn=fetch,
                lock_holder_fn=lambda base: {
                    "controller_id": "other",
                    "pid": 4,
                    "state_dir": "C:/state",
                    "base_url": base,
                },
            )
        self.assertEqual(result["gui"]["status"], "not_running")
        self.assertEqual(result["occupancy"]["state"], "unknown")
        self.assertFalse(result["ready"])
        self.assertFalse(result["dispatch_allowed"])
        self.assertIn("不要派工", "\n".join(result["reasons"]))
        self.assertEqual(result["lock_holder"]["controller_id"], "other")

    def test_stale_lock_holder_does_not_block_idle_and_drops_secrets(self):
        result = self._assess(
            {},
            lock_holder_fn=lambda base: {
                "controller_id": "abc",
                "pid": 9,
                "state_dir": "C:/tmp/state",
                "base_url": base,
                "password": "super-secret-value",
                "OPENCODE_SERVER_PASSWORD": "nope",
            },
        )
        self.assertTrue(result["ready"])
        self.assertTrue(result["dispatch_allowed"])
        self.assertEqual(result["lock_holder"]["pid"], 9)
        self.assertNotIn("password", result["lock_holder"])
        self.assertNotIn("super-secret-value", json.dumps(result))

    def test_default_fetcher_is_get_only(self):
        fake = _FakeClient()

        def factory(base=None, creds=None):
            fake.base = base
            fake.creds = creds
            return fake

        with mock.patch(
            "win_collab.client.discover",
            return_value=("http://127.0.0.1:4399", {"u": "a", "p": "b", "k": "c"}),
        ), mock.patch(
            "win_collab.client.Client",
            side_effect=factory,
        ), mock.patch(
            "win_collab.desktop_lock.claim_desktop",
            side_effect=AssertionError("claim_desktop"),
        ), mock.patch(
            "teleagent_adapter.windows_stdin_wrap.ensure_stdin_wrap",
            side_effect=AssertionError("stdin_wrap"),
        ):
            result = assess_win_gui_readiness(
                simulated=True,
                simulate_status="ok",
                lock_holder_fn=lambda base: None,
            )
        self.assertEqual(fake.calls, [("GET", "/session/status", None)])
        self.assertTrue(result["ready"])
        self.assertEqual(fake.base, "http://127.0.0.1:4399")

    def test_creds_probe_forces_wrap_channel_off(self):
        seen = {}

        def fake_probe(**kwargs):
            seen.update(kwargs)
            return _Presence()

        with mock.patch(
            "teleagent_adapter.windows_process_environ.probe_windows_creds_presence",
            side_effect=fake_probe,
        ), mock.patch(
            "teleagent_adapter.windows_stdin_wrap.ensure_stdin_wrap",
            side_effect=AssertionError("stdin_wrap"),
        ), mock.patch(
            "win_collab.desktop_lock.claim_desktop",
            side_effect=AssertionError("claim_desktop"),
        ):
            result = assess_win_gui_readiness(
                port_open_fn=lambda host, port: True,
                session_status_fn=lambda base: {},
                lock_holder_fn=lambda base: None,
            )
        self.assertEqual(seen["environ"]["TELEAGENT_WIN_CREDS_CHANNEL"], "off")
        self.assertTrue(result["ready"])
        self.assertEqual(result["base_url"], "http://127.0.0.1:4399")

    def test_check_gui_stays_doctor_only(self):
        service = _load_collab_service()
        probe = {
            "ok": True,
            "status": "ok",
            "reason": "",
            "base_url": "http://127.0.0.1:4397",
            "simulated": False,
        }
        buf = io.StringIO()
        with mock.patch(
            "framework.app_service.probe_win_gui_connection",
            return_value=probe,
        ) as probed, mock.patch(
            "framework.app_service.assess_win_gui_readiness",
            side_effect=AssertionError("ready"),
        ), redirect_stdout(buf):
            code = service.main(["--check-gui"])
        self.assertEqual(code, 0)
        probed.assert_called_once_with()
        self.assertTrue(json.loads(buf.getvalue())["ok"])

    def test_ready_cli_exit_codes(self):
        service = _load_collab_service()
        idle = {
            "ready": True,
            "dispatch_allowed": True,
            "gui": {"status": "ok"},
            "occupancy": {"state": "idle"},
            "reasons": [],
        }
        busy = {
            "ready": False,
            "dispatch_allowed": False,
            "gui": {"status": "ok"},
            "occupancy": {"state": "busy"},
            "reasons": ["DO NOT DISPATCH", "不要派工"],
        }
        with mock.patch(
            "framework.app_service.assess_win_gui_readiness",
            return_value=idle,
        ), mock.patch.object(
            service,
            "CollabHttpServer",
            side_effect=AssertionError("server"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(service.main(["--ready"]), 0)
        self.assertTrue(json.loads(buf.getvalue())["dispatch_allowed"])
        with mock.patch(
            "framework.app_service.assess_win_gui_readiness",
            return_value=busy,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(service.main(["--ready"]), 1)
        text = buf.getvalue()
        self.assertIn("不要派工", text)

    def test_win_collab_ready_alias_does_not_open_store(self):
        import win_collab.__main__ as wm

        busy = {"ready": False, "dispatch_allowed": False, "reasons": ["不要派工"]}
        buf = io.StringIO()
        with mock.patch(
            "framework.app_service.assess_win_gui_readiness",
            return_value=busy,
        ) as assess, mock.patch.object(
            wm, "Store", side_effect=AssertionError("store")
        ), mock.patch.object(
            wm, "Client", side_effect=AssertionError("client")
        ), redirect_stdout(buf):
            code = wm.main(["ready"])
        self.assertEqual(code, 1)
        assess.assert_called_once_with()
        self.assertIn("不要派工", buf.getvalue())

    def test_probe_shape_unchanged_for_simulated_ok(self):
        result = probe_win_gui_connection(simulated=True, simulate_status="ok")
        self.assertEqual(
            set(result),
            {"ok", "status", "reason", "base_url", "simulated", "details"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["reason"], "")


if __name__ == "__main__":
    unittest.main()
