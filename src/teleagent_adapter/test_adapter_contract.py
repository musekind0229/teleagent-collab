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
    WindowsLocalV1Adapter,
    doctor,
    get_adapter,
)
from teleagent_adapter.windows_local_v1 import (  # noqa: E402
    default_find_creds_windows,
    discover_windows_base_url,
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
        if method == "POST" and path.startswith("/question/") and path.endswith("/reply"):
            qid = path.split("/")[2]
            self.replies.append((qid, (body or {}).get("answers")))
            self.questions = [q for q in self.questions if q.get("id") != qid]
            return 200, True
        if method == "POST" and path.startswith("/question/") and path.endswith("/reject"):
            qid = path.split("/")[2]
            self.replies.append((qid, "reject"))
            self.questions = [q for q in self.questions if q.get("id") != qid]
            return 200, True
        if method == "GET" and path == "/session/status":
            return 200, {sid: {"type": "idle"} for sid in self.sessions}
        if method == "GET" and "/message" in path:
            return 200, []
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

    def test_reply_and_reject_question(self):
        ad, tr = _sim_adapter()
        tr.questions = [
            {"id": "q1", "sessionID": "ses_x", "questions": [{"header": "pick?"}]},
        ]
        code, _ = ad.reply_question("q1", [["yes"]])
        self.assertEqual(code, 200)
        self.assertEqual(tr.replies[-1][0], "q1")
        self.assertEqual(tr.replies[-1][1], [["yes"]])
        tr.questions = [{"id": "q2", "sessionID": "ses_x"}]
        code, _ = ad.reject_question("q2")
        self.assertEqual(code, 200)
        self.assertEqual(len(tr.questions), 0)


def _win_sim_adapter() -> tuple[WindowsLocalV1Adapter, FakeTransport]:
    ad = WindowsLocalV1Adapter(
        base_url="http://127.0.0.1:4399",
        find_creds_fn=lambda: ("super-agent", "sim-pass", "sim-session-key"),
        lazy_creds=True,
        discover=False,
    )
    transport = FakeTransport()
    ad.call = transport  # type: ignore[method-assign]
    return ad, transport


class _ProbeAdapter:
    """Injected HTTP adapter for doctor classification tests."""

    def __init__(
        self,
        *,
        refresh_exc: Exception | None = None,
        version: tuple[int, Any] = (200, {"version": "1.2.27"}),
        permission: tuple[int, Any] = (200, []),
        question: tuple[int, Any] = (200, []),
        call_exc: Exception | None = None,
    ) -> None:
        self._refresh_exc = refresh_exc
        self._version = version
        self._permission = permission
        self._question = question
        self._call_exc = call_exc
        self.calls: list[tuple[str, str]] = []

    def refresh_creds(self) -> None:
        if self._refresh_exc is not None:
            raise self._refresh_exc

    def call(self, method: str, path: str, *args: Any, **kwargs: Any):
        self.calls.append((method, path))
        if self._call_exc is not None:
            raise self._call_exc
        if path == "/version":
            return self._version
        if path == "/permission":
            return self._permission
        if path == "/question":
            return self._question
        return 404, {"error": path}


def _open(_h: str, _p: int) -> bool:
    return True


def _closed(_h: str, _p: int) -> bool:
    return False


class TestWindowsBlocked(unittest.TestCase):
    """Explicit blocked stub still works; it is no longer the factory default."""

    def test_all_ops_blocked(self):
        w = WindowsBlockedAdapter(teleagent_version="2.4.1")
        with self.assertRaises(AdapterError) as cm:
            w.create_session(title="t", directory="C:\\ws")
        self.assertEqual(cm.exception.status, AdapterStatus.BLOCKED)
        hint = w.doctor_hint()
        self.assertEqual(hint["status"], "blocked")
        self.assertIn("2.4.1", hint["reason"])

    def test_factory_blocked_flag(self):
        ad = get_adapter(platform="win32", blocked=True)
        self.assertIsInstance(ad, WindowsBlockedAdapter)


class TestWindowsLocalV1Simulated(unittest.TestCase):
    """SIMULATED — mock transport, no live Windows TeleAgent. Windows 真机未验收."""

    def test_factory_win32_and_windows_not_blocked(self):
        self.assertTrue(SIMULATED)
        for plat in ("win32", "windows", "win"):
            ad = get_adapter(platform=plat, discover=False)
            self.assertIsInstance(ad, WindowsLocalV1Adapter, plat)
            self.assertNotIsInstance(ad, WindowsBlockedAdapter)

    def test_linux_factory_unchanged(self):
        ad = get_adapter(platform="linux")
        self.assertIsInstance(ad, LinuxLocalV1Adapter)
        self.assertNotIsInstance(ad, WindowsLocalV1Adapter)

    def test_create_prompt_permissions_reply_cancel_win_paths(self):
        ad, tr = _win_sim_adapter()
        ws = r"C:\Users\alice\ws"
        code, created = ad.create_session(title="job-1", directory=ws)
        self.assertEqual(code, 200)
        sid = created["id"]
        self.assertEqual(created["directory"], ws)
        code, _ = ad.prompt(sid, "hello", directory=ws)
        self.assertEqual(code, 204)
        tr.permissions = [
            {"id": "p1", "sessionID": sid, "path": ws + r"\a.py"},
            {"id": "p2", "sessionID": "ses_other", "path": r"C:\Windows\System32\config"},
        ]
        code, perms = ad.list_permissions(session_id=sid)
        self.assertEqual(code, 200)
        self.assertEqual(len(perms), 1)
        self.assertEqual(perms[0]["id"], "p1")
        code, body = ad.reply_permission("p1", "once")
        self.assertEqual(code, 200)
        self.assertEqual(tr.replies[-1], ("p1", "once"))
        # reply shape: always coerced to once (same as Linux)
        tr.permissions.append({"id": "p3", "sessionID": sid})
        ad.reply_permission("p3", "always")
        self.assertEqual(tr.replies[-1], ("p3", "once"))
        # reject stays reject
        tr.permissions.append({"id": "p4", "sessionID": sid})
        ad.reply_permission("p4", "reject")
        self.assertEqual(tr.replies[-1], ("p4", "reject"))
        code, st = ad.session_status(session_id=sid)
        self.assertEqual(code, 200)
        self.assertIn(sid, st)
        code, _ = ad.cancel(sid)
        self.assertEqual(code, 200)

    def test_permission_reply_posts_expected_path_and_body(self):
        ad, tr = _win_sim_adapter()
        tr.permissions = [{"id": "req_win_1", "sessionID": "ses_x"}]
        ad.reply_permission("req_win_1", "once")
        self.assertIn(("POST", "/permission/req_win_1/reply"), tr.calls)
        self.assertEqual(tr.replies[-1], ("req_win_1", "once"))

    def test_resume_and_questions(self):
        ad, tr = _win_sim_adapter()
        _, created = ad.create_session(title="t", directory=r"C:\ws")
        sid = created["id"]
        code, obj = ad.resume(sid)
        self.assertEqual(code, 200)
        self.assertEqual(obj["id"], sid)
        tr.questions = [
            {"id": "q1", "sessionID": sid},
            {"id": "q2", "sessionID": "ses_other"},
        ]
        _, qs = ad.list_questions(session_id=sid)
        self.assertEqual(len(qs), 1)
        ad.reply_question("q1", [["yes"]])
        self.assertEqual(tr.replies[-1][1], [["yes"]])

    def test_sign_headers_local_v1(self):
        ad = WindowsLocalV1Adapter(
            base_url="http://127.0.0.1:4399",
            find_creds_fn=lambda: ("super-agent", "pw", "key"),
            discover=False,
        )
        h = ad.sign_headers("GET", "http://127.0.0.1:4399/permission")
        self.assertEqual(h["X-SA-Sign-Version"], "local-v1")
        self.assertIn("Authorization", h)
        self.assertTrue(h["X-SA-Signature"])

    def test_discover_prefers_4399_then_4397(self):
        seen: list[int] = []

        def probe(host: str, port: int) -> bool:
            seen.append(port)
            return port == 4399

        url = discover_windows_base_url(probe_fn=probe, env={})
        self.assertEqual(url, "http://127.0.0.1:4399")
        self.assertEqual(seen[0], 4399)

        def only_4397(host: str, port: int) -> bool:
            return port == 4397

        url = discover_windows_base_url(probe_fn=only_4397, env={})
        self.assertEqual(url, "http://127.0.0.1:4397")

    def test_discover_nothing_listening_defaults_4399(self):
        url = discover_windows_base_url(probe_fn=lambda h, p: False, env={})
        self.assertEqual(url, "http://127.0.0.1:4399")

    def test_discover_env_base_url_and_port(self):
        url = discover_windows_base_url(
            probe_fn=lambda h, p: False,
            env={"TELEAGENT_BASE_URL": "http://127.0.0.1:4399"},
        )
        self.assertEqual(url, "http://127.0.0.1:4399")
        url = discover_windows_base_url(
            probe_fn=lambda h, p: p == 4400,
            env={"TELEAGENT_PORT": "4400"},
        )
        self.assertEqual(url, "http://127.0.0.1:4400")

    def test_creds_from_env_and_missing(self):
        u, p, k = default_find_creds_windows(
            environ={
                "OPENCODE_SERVER_USERNAME": "super-agent",
                "OPENCODE_SERVER_PASSWORD": "sim-pass",
                "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
            }
        )
        self.assertEqual((u, p, k), ("super-agent", "sim-pass", "sim-key"))
        with self.assertRaises(AdapterError) as cm:
            default_find_creds_windows(environ={}, foreign_finder=lambda: None)
        self.assertEqual(cm.exception.status, AdapterStatus.MISSING_CREDS)
        self.assertIn("other", str(cm.exception).lower())
        self.assertIn("Do not disable authentication", str(cm.exception))

    def test_creds_process_env_preferred_over_foreign(self):
        u, p, k = default_find_creds_windows(
            environ={
                "OPENCODE_SERVER_PASSWORD": "sim-pass",
                "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
            },
            foreign_finder=lambda: ("super-agent", "sim-foreign-pass", "sim-foreign-key"),
        )
        self.assertEqual((u, p, k), ("super-agent", "sim-pass", "sim-key"))

    def test_creds_foreign_fallback_when_process_env_empty(self):
        u, p, k = default_find_creds_windows(
            environ={},
            foreign_finder=lambda: ("super-agent", "sim-pass", "sim-key"),
        )
        self.assertEqual((u, p, k), ("super-agent", "sim-pass", "sim-key"))

    def test_adapter_discover_uses_probe(self):
        ad = WindowsLocalV1Adapter(
            find_creds_fn=lambda: ("super-agent", "p", "k"),
            discover=True,
            probe_fn=lambda h, p: p == 4397,
            env={},
        )
        self.assertEqual(ad.base_url, "http://127.0.0.1:4397")


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
            AdapterStatus.OK.value,
        ):
            r = doctor(simulated=True, simulate_status=st)
            self.assertTrue(r.simulated)
            self.assertEqual(r.status, st)

    def test_explicit_blocked_adapter_doctor(self):
        r = doctor(platform="win32", adapter=WindowsBlockedAdapter(), port_open_fn=_open)
        self.assertEqual(r.status, AdapterStatus.BLOCKED.value)
        self.assertFalse(r.extras.get("windows_live_verified", True))

    def test_version_incompatible(self):
        r = doctor(simulated=True, simulate_status=AdapterStatus.VERSION_INCOMPATIBLE.value)
        self.assertEqual(r.status, "version_incompatible")


class TestDoctorClassificationWinAndLinux(unittest.TestCase):
    """Doctor categories on win32 and linux — injected TCP + HTTP. Windows 真机未验收."""

    def test_not_running(self):
        for plat in ("win32", "linux"):
            r = doctor(platform=plat, port_open_fn=_closed)
            self.assertEqual(r.status, AdapterStatus.NOT_RUNNING.value, plat)

    def test_version_incompatible_when_port_open(self):
        for plat in ("win32", "linux"):
            r = doctor(
                platform=plat,
                teleagent_version="2.4.1",
                port_open_fn=_open,
            )
            self.assertEqual(r.status, AdapterStatus.VERSION_INCOMPATIBLE.value, plat)

    def test_missing_creds(self):
        ad = _ProbeAdapter(refresh_exc=AdapterError(AdapterStatus.MISSING_CREDS, "no creds"))
        for plat in ("win32", "linux"):
            r = doctor(platform=plat, adapter=ad, port_open_fn=_open)
            self.assertEqual(r.status, AdapterStatus.MISSING_CREDS.value, plat)

    def test_auth_failed_http_401(self):
        ad = _ProbeAdapter(version=(401, {"code": "unauthorized"}))
        for plat in ("win32", "linux"):
            r = doctor(platform=plat, adapter=ad, port_open_fn=_open)
            self.assertEqual(r.status, AdapterStatus.AUTH_FAILED.value, plat)

    def test_auth_failed_raised(self):
        ad = _ProbeAdapter(call_exc=AdapterError(AdapterStatus.AUTH_FAILED, "HTTP 401"))
        for plat in ("win32", "linux"):
            r = doctor(platform=plat, adapter=ad, port_open_fn=_open)
            self.assertEqual(r.status, AdapterStatus.AUTH_FAILED.value, plat)

    def test_api_incompatible_404(self):
        ad = _ProbeAdapter(version=(404, {"error": "missing"}))
        for plat in ("win32", "linux"):
            r = doctor(platform=plat, adapter=ad, port_open_fn=_open)
            self.assertEqual(r.status, AdapterStatus.API_INCOMPATIBLE.value, plat)

    def test_ok(self):
        ad = _ProbeAdapter()
        for plat in ("win32", "linux"):
            r = doctor(platform=plat, adapter=ad, port_open_fn=_open, teleagent_version="2.5.0")
            self.assertEqual(r.status, AdapterStatus.OK.value, plat)
        win = doctor(platform="win32", adapter=ad, port_open_fn=_open)
        self.assertFalse(win.extras.get("windows_live_verified", True))
        self.assertIn(win.extras.get("creds_source"), (
            "process_env",
            "foreign_process_environ",
            "missing",
        ))
        self.assertIn("4397", win.extras.get("ports") or {})
        self.assertIn("4399", win.extras.get("ports") or {})
        self.assertNotIn("sim-pass", json.dumps(win.to_dict()))

    def test_permission_401_is_auth_failed(self):
        ad = _ProbeAdapter(permission=(401, {"code": "unauthorized"}))
        r = doctor(platform="win32", adapter=ad, port_open_fn=_open)
        self.assertEqual(r.status, AdapterStatus.AUTH_FAILED.value)


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


class TestDoctorWinPortFallback(unittest.TestCase):
    def test_doctor_win_falls_back_to_4397(self):
        from teleagent_adapter.doctor import doctor

        def probe(_host, port):
            return port == 4397

        r = doctor(
            platform="win32",
            base_url="http://127.0.0.1:4399",
            port_open_fn=probe,
            adapter=None,
        )
        self.assertEqual(r.base_url, "http://127.0.0.1:4397")
        self.assertNotEqual(r.status, "not_running")

