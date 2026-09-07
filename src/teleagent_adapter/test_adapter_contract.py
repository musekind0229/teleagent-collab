#!/usr/bin/env python3
"""Contract tests for teleagent_adapter — all HTTP is *simulated* (mock), not live TeleAgent."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Allow `python3 -m teleagent_adapter.test_adapter_contract` from src/
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from teleagent_adapter import (  # noqa: E402
    AdapterError,
    AdapterStatus,
    LinuxLocalV1Adapter,
    WindowsBlockedAdapter,
    doctor,
    get_adapter,
)
from teleagent_adapter.base import (  # noqa: E402
    creds_refresh_policy,
    filter_by_session,
    reconnect_policy,
    resume_policy,
    session_id_of,
)


SIMULATED = True  # marker: these tests never hit a real TeleAgent


class FakeTransport:
    """In-memory fake for LinuxLocalV1Adapter.call — simulated."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.permissions: list[dict] = []
        self.questions: list[dict] = []
        self.replies: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, body=None, extra_headers=None, timeout=120, **kw):
        self.calls.append((method, path))
        if method == "POST" and path == "/session":
            sid = f"ses_sim_{len(self.sessions)+1}"
            self.sessions[sid] = {"id": sid, "directory": (body or {}).get("directory")}
            return 200, self.sessions[sid]
        if method == "POST" and path.endswith("/prompt_async"):
            return 204, None
        if method == "GET" and path == "/permission":
            return 200, list(self.permissions)
        if method == "GET" and path == "/question":
            return 200, list(self.questions)
        if method == "POST" and "/permission/" in path and path.endswith("/reply"):
            pid = path.split("/")[2]
            reply = (body or {}).get("reply")
            self.replies.append((pid, reply))
            self.permissions = [p for p in self.permissions if p.get("id") != pid]
            return 200, {"ok": True}
        if method == "GET" and path == "/session/status":
            return 200, {sid: {"type": "idle"} for sid in self.sessions}
        if method == "POST" and path.endswith("/abort"):
            return 200, {"ok": True}
        if method == "GET" and path.startswith("/session/"):
            sid = path.split("/", 2)[2]
            if sid in self.sessions:
                return 200, self.sessions[sid]
            return 404, {"error": "missing"}
        if method == "GET" and path == "/session":
            return 200, list(self.sessions.values())
        if method == "GET" and path == "/version":
            return 200, {"version": "1.2.27"}
        return 404, {"error": f"unhandled {method} {path}"}


def _sim_adapter() -> tuple[LinuxLocalV1Adapter, FakeTransport]:
    ad = LinuxLocalV1Adapter(
        base_url="http://127.0.0.1:4399",
        find_creds_fn=lambda: ("super-agent", "sim-pass", "sim-session-key"),
        lazy_creds=True,
    )
    transport = FakeTransport()
    ad.call = transport  # type: ignore[method-assign]
    return ad, transport


class TestFilterSession(unittest.TestCase):
    def test_session_id_of(self):
        self.assertEqual(session_id_of({"sessionID": "ses_a"}), "ses_a")
        self.assertEqual(session_id_of({"metadata": {"session_id": "ses_b"}}), "ses_b")

    def test_filter_by_session(self):
        items = [
            {"id": "1", "sessionID": "ses_a"},
            {"id": "2", "sessionID": "ses_b"},
            {"id": "3", "sessionID": "ses_a"},
        ]
        got = filter_by_session(items, "ses_a")
        self.assertEqual([x["id"] for x in got], ["1", "3"])


class TestLinuxSimulated(unittest.TestCase):
    """SIMULATED — mock transport, no live :4399."""

    def test_create_prompt_permissions_reply_cancel(self):
        self.assertTrue(SIMULATED)
        ad, tr = _sim_adapter()
        code, created = ad.create_session(title="t", directory="/tmp/ws")
        self.assertEqual(code, 200)
        sid = created["id"]
        code, _ = ad.prompt(sid, "hello", directory="/tmp/ws")
        self.assertEqual(code, 204)
        tr.permissions = [
            {"id": "p1", "sessionID": sid, "path": "/tmp/ws/a.py"},
            {"id": "p2", "sessionID": "ses_other", "path": "/etc/passwd"},
        ]
        code, perms = ad.list_permissions(session_id=sid)
        self.assertEqual(code, 200)
        self.assertEqual(len(perms), 1)
        self.assertEqual(perms[0]["id"], "p1")
        code, _ = ad.reply_permission("p1", "once")
        self.assertEqual(code, 200)
        self.assertEqual(tr.replies[-1], ("p1", "once"))
        # always coerced to once at adapter boundary
        tr.permissions.append({"id": "p3", "sessionID": sid})
        ad.reply_permission("p3", "always")
        self.assertEqual(tr.replies[-1], ("p3", "once"))
        code, st = ad.session_status(session_id=sid)
        self.assertEqual(code, 200)
        self.assertIn(sid, st)
        code, _ = ad.cancel(sid)
        self.assertEqual(code, 200)

    def test_resume(self):
        ad, tr = _sim_adapter()
        _, created = ad.create_session(title="t", directory="/tmp/ws")
        sid = created["id"]
        code, obj = ad.resume(sid)
        self.assertEqual(code, 200)
        self.assertEqual(obj["id"], sid)

    def test_list_questions_filtered(self):
        ad, tr = _sim_adapter()
        tr.questions = [
            {"id": "q1", "sessionID": "ses_x"},
            {"id": "q2", "sessionID": "ses_y"},
        ]
        _, qs = ad.list_questions(session_id="ses_x")
        self.assertEqual(len(qs), 1)


class TestWindowsBlocked(unittest.TestCase):
    def test_all_ops_blocked(self):
        w = WindowsBlockedAdapter(teleagent_version="2.4.1")
        with self.assertRaises(AdapterError) as cm:
            w.create_session(title="t", directory="C:\\ws")
        self.assertEqual(cm.exception.status, AdapterStatus.BLOCKED)
        hint = w.doctor_hint()
        self.assertEqual(hint["status"], "blocked")
        self.assertIn("2.4.1", hint["reason"])

    def test_factory_windows(self):
        ad = get_adapter(platform="win32")
        self.assertIsInstance(ad, WindowsBlockedAdapter)


class TestDoctorSimulated(unittest.TestCase):
    """SIMULATED doctor statuses — no real socket required when simulate_status set."""

    def test_simulated_statuses(self):
        for st in (
            AdapterStatus.NOT_RUNNING.value,
            AdapterStatus.VERSION_INCOMPATIBLE.value,
            AdapterStatus.MISSING_CREDS.value,
            AdapterStatus.AUTH_FAILED.value,
            AdapterStatus.API_INCOMPATIBLE.value,
            AdapterStatus.BLOCKED.value,
        ):
            r = doctor(simulated=True, simulate_status=st)
            self.assertTrue(r.simulated)
            self.assertEqual(r.status, st)

    def test_windows_doctor(self):
        r = doctor(platform="win32", teleagent_version="2.4.1")
        self.assertEqual(r.status, AdapterStatus.BLOCKED.value)

    def test_version_incompatible(self):
        # Force port-open bypass by using simulated path for version check via teleagent_version
        # when port closed → not_running first; use simulated for version
        r = doctor(simulated=True, simulate_status=AdapterStatus.VERSION_INCOMPATIBLE.value)
        self.assertEqual(r.status, "version_incompatible")


class TestPolicies(unittest.TestCase):
    def test_policy_docs_present(self):
        for fn in (creds_refresh_policy, reconnect_policy, resume_policy):
            d = fn()
            self.assertIsInstance(d, dict)
            self.assertTrue(d)


class TestSignHeadersSimulated(unittest.TestCase):
    def test_local_v1_headers(self):
        ad = LinuxLocalV1Adapter(
            find_creds_fn=lambda: ("super-agent", "pw", "key"),
        )
        h = ad.sign_headers("GET", "http://127.0.0.1:4399/permission")
        self.assertEqual(h["X-SA-Sign-Version"], "local-v1")
        self.assertIn("Authorization", h)
        self.assertTrue(h["X-SA-Signature"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
