"""Simulated tests: Windows in-flight recovery after TeleAgent restart/port/cred change."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from desktop_lock_isolation import install_desktop_lock_isolation
from win_collab.core import SCAN_ERROR_LIMIT, SESSION_STATUS_GRACE_S, TERMINAL, Engine, Store


class FakeClient:
    def __init__(self, base="http://127.0.0.1:4397", instance_id=None):
        self.base = base
        self.instance_id = instance_id or base
        self.calls = []
        self.routes = {}

    def call(self, method, path, body=None, workspace=None):
        self.calls.append((method, path, body))
        key = (method.upper(), path.split("?")[0])
        if key in self.routes:
            val = self.routes[key]
            if isinstance(val, Exception):
                raise val
            return val
        raise RuntimeError(f"unexpected call {method} {path}")


def _running_job(store, *, backend="http://127.0.0.1:4397", backend_id=None, session_id="sess-1"):
    jid = "jobdeadbeef01"
    workspace = store.home / "workspaces" / jid
    workspace.mkdir(parents=True, exist_ok=True)
    job = {
        "id": jid,
        "run_id": "run1",
        "state": "running",
        "charter": {
            "name": "t",
            "goal": "g",
            "must": ["stay"],
            "must_not": ["secrets"],
            "artifacts": ["out.txt"],
            "acceptance": "out.txt exists",
            "timeout_sec": 900,
        },
        "charter_hash": "h",
        "workspace": str(workspace),
        "session_id": session_id,
        "created_at": time.time(),
        "deadline": time.time() + 900,
        "next_scan": 0,
        "scans": 0,
        "lead_requests": 0,
        "approved_permissions": 0,
        "redos": 0,
        "handled": [],
        "error": None,
        "backend": backend,
        "backend_id": backend_id or backend,
        "scan_error_streak": 0,
    }
    with store.transaction():
        store.save(job)
    return job


class TestInFlightRecovery(unittest.TestCase):
    def setUp(self):
        install_desktop_lock_isolation(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(home=self.tmp.name)

    def tearDown(self):
        try:
            if getattr(self.store, 'db', None) is not None:
                self.store.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_backend_change_fails_need_human_no_new_session(self):
        _running_job(self.store, backend="http://127.0.0.1:4397", backend_id="old")
        client = FakeClient(base="http://127.0.0.1:4401", instance_id="new")
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "failed")
        self.assertIn("need_human", job["error"])
        self.assertIn("backend/port/cred", job["error"])
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))

    def test_missing_session_fails_need_human_no_redispatch(self):
        # An empty transcript is not in flight. Fail closed only after
        # consecutive status misses, and still do not redispatch.
        job = _running_job(self.store, session_id="sess-gone")
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        client.routes[("GET", "/session/sess-gone/message")] = []
        engine = Engine(self.store, client)
        job = None
        for i in range(SCAN_ERROR_LIMIT):
            current = self.store.get("jobdeadbeef01")
            current["next_scan"] = 0
            with self.store.transaction():
                self.store.save(current)
            engine.tick()
            job = self.store.get("jobdeadbeef01")
            if i + 1 < SCAN_ERROR_LIMIT:
                self.assertEqual(job["state"], "running", job)
                self.assertEqual(job.get("session_id"), "sess-gone")
                self.assertEqual(job.get("status_miss_streak"), i + 1)
                self.assertFalse(job.get("error"))
                self.assertNotIn(job["state"], TERMINAL)
        self.assertEqual(job["state"], "failed")
        self.assertIn("need_human", job["error"])
        self.assertIn("session missing from status", job["error"])
        self.assertIn(f"after {SESSION_STATUS_GRACE_S:g}s grace", job["error"])
        self.assertIn(f"scans={SCAN_ERROR_LIMIT}", job["error"])
        self.assertIn("refusing silent redispatch", job["error"])
        self.assertNotIn("restart", job["error"].lower())
        self.assertNotIn("teleagent restart", job["error"].lower())
        self.assertIsNone(job.get("session_id"))
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))
        self.assertFalse(any(str(p).endswith("/abort") for _, p, _ in client.calls))

    def test_just_started_missing_session_within_grace_stays_running(self):
        job = _running_job(self.store, session_id="sess-new")
        job["dispatched_at"] = time.time()
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        started = time.time()
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running")
        self.assertEqual(job.get("session_id"), "sess-new")
        self.assertNotIn(job["state"], TERMINAL)
        self.assertFalse(job.get("error"))
        self.assertGreater(job["next_scan"], started)
        self.assertLess(job["next_scan"] - started, 3)
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))
        self.assertFalse(any(str(p).endswith("/abort") for _, p, _ in client.calls))
        self.assertFalse(any(str(p).endswith("/message") for _, p, _ in client.calls))

    def test_missing_session_within_grace_falls_back_to_created_at(self):
        _running_job(self.store, session_id="sess-new")
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running")
        self.assertEqual(job.get("session_id"), "sess-new")
        self.assertFalse(job.get("error"))
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))

    def test_deleted_session_fails_immediately_inside_grace(self):
        job = _running_job(self.store, session_id="sess-gone")
        job["dispatched_at"] = time.time()
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {"deleted-session-ids": ["sess-gone"]}
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "failed")
        self.assertIn("need_human", job["error"])
        self.assertIn("session missing from status deleted", job["error"])
        self.assertIn("refusing silent redispatch", job["error"])
        self.assertNotIn("grace", job["error"])
        self.assertNotIn("restart", job["error"].lower())
        self.assertIsNone(job.get("session_id"))
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))

    def test_session_still_present_stays_running(self):
        _running_job(self.store, session_id="sess-live")
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {"sess-live": {"type": "busy"}}
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running")
        self.assertNotIn(job["state"], TERMINAL)

    def test_http_401_fails_need_human(self):
        _running_job(self.store)
        client = FakeClient()
        client.routes[("GET", "/permission")] = RuntimeError(
            "TeleAgent GET /permission returned HTTP 401"
        )
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "failed")
        self.assertIn("need_human", job["error"])
        self.assertIn("auth", job["error"].lower())

    def test_repeated_scan_errors_fail_closed(self):
        job = _running_job(self.store)
        job["scan_error_streak"] = SCAN_ERROR_LIMIT - 1
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = RuntimeError("boom")
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "failed")
        self.assertIn("need_human", job["error"])
        self.assertIn("repeated scan", job["error"])

    def test_busy_then_empty_status_finish_stop_reaches_review(self):
        sid = "sess-done"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        (Path(job["workspace"]) / "out.txt").write_text("done\n", encoding="utf-8")
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {sid: {"type": "busy"}}
        client.routes[("GET", f"/session/{sid}/message")] = [
            {"info": {"role": "user"}},
            {"info": {"role": "assistant", "finish": "stop"}, "parts": []},
        ]
        engine = Engine(self.store, client)
        engine.tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running", job)
        self.assertFalse(any(str(p).endswith("/message") for _, p, _ in client.calls))
        client.routes[("GET", "/session/status")] = {}
        job["next_scan"] = 0
        with self.store.transaction():
            self.store.save(job)
        engine.tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "awaiting_review", job)
        self.assertEqual(job.get("session_id"), sid)
        self.assertFalse(job.get("error"))
        self.assertNotIn("need_human", job.get("error") or "")
        self.assertNotIn("session missing", job.get("error") or "")
        inbox = self.store.inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["kind"], "review")
        self.assertEqual(inbox[0]["payload"]["finish"], "stop")
        self.assertTrue(any(p == f"/session/{sid}/message" for _, p, _ in client.calls))
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))
        self.assertFalse(any(str(p).endswith("/abort") for _, p, _ in client.calls))

    def test_empty_status_without_terminal_messages_stays_running(self):
        sid = "sess-live"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        client.routes[("GET", f"/session/{sid}/message")] = [
            {"info": {"role": "assistant"}},
        ]
        started = time.time()
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running", job)
        self.assertEqual(job.get("session_id"), sid)
        # A bare assistant is in flight; do not burn the miss streak.
        self.assertFalse(job.get("status_miss_streak"))
        self.assertFalse(job.get("error"))
        self.assertNotIn(job["state"], TERMINAL)
        self.assertGreater(job["next_scan"], started)
        self.assertTrue(any(p == f"/session/{sid}/message" for _, p, _ in client.calls))
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))
        self.assertFalse(any(str(p).endswith("/abort") for _, p, _ in client.calls))

    def test_tool_calls_inflight_empty_status_does_not_bump_miss_streak(self):
        # Status often drops a sid while the worker is mid-turn on tool_calls.
        sid = "sess-inflight"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        client.routes[("GET", f"/session/{sid}/message")] = [
            {"info": {"role": "user"}},
            {"info": {"role": "assistant", "finish": "tool_calls"}, "parts": []},
        ]
        engine = Engine(self.store, client)
        started = time.time()
        job = None
        for _ in range(SCAN_ERROR_LIMIT + 1):
            current = self.store.get("jobdeadbeef01")
            current["next_scan"] = 0
            with self.store.transaction():
                self.store.save(current)
            engine.tick()
            job = self.store.get("jobdeadbeef01")
            self.assertEqual(job["state"], "running", job)
            self.assertEqual(job.get("session_id"), sid)
            self.assertFalse(job.get("status_miss_streak"))
            self.assertFalse(job.get("error"))
            self.assertNotIn(job["state"], TERMINAL)
            self.assertNotIn("need_human", job.get("error") or "")
        self.assertGreater(job["next_scan"], started)
        self.assertTrue(any(p == f"/session/{sid}/message" for _, p, _ in client.calls))
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))
        self.assertFalse(any(str(p).endswith("/abort") for _, p, _ in client.calls))

    def test_status_omits_sid_with_assistant_error_uses_normal_fail(self):
        sid = "sess-err"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        client.routes[("POST", f"/session/{sid}/abort")] = {}
        client.routes[("GET", f"/session/{sid}/message")] = [
            {"info": {"role": "assistant", "error": {"name": "APIError", "message": "boom"}}},
        ]
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "failed", job)
        self.assertIn("Worker reported an error", job["error"])
        self.assertIn("name=APIError", job["error"])
        self.assertNotIn("need_human", job["error"])
        self.assertNotIn("session missing", job["error"])
        self.assertEqual(job.get("session_id"), sid)
        self.assertFalse(any(p == "/session" and m == "POST" for m, p, _ in client.calls))

    def test_message_probe_invalid_counts_as_scan_error(self):
        sid = "sess-live"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        client.routes[("GET", f"/session/{sid}/message")] = {"error": "nope"}
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running", job)
        self.assertEqual(job.get("session_id"), sid)
        self.assertEqual(job.get("scan_error_streak"), 1)
        self.assertFalse(job.get("status_miss_streak"))
        self.assertIn("Invalid message response", job.get("error") or "")
        self.assertNotIn("session missing", job.get("error") or "")
        self.assertNotIn("deleted", job.get("error") or "")

    def test_message_probe_http_error_counts_as_scan_error(self):
        sid = "sess-live"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        client.routes[("GET", f"/session/{sid}/message")] = RuntimeError("connection reset")
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running", job)
        self.assertEqual(job.get("session_id"), sid)
        self.assertEqual(job.get("scan_error_streak"), 1)
        self.assertFalse(job.get("status_miss_streak"))
        self.assertIn("connection reset", job.get("error") or "")
        self.assertNotIn("session missing", job.get("error") or "")
        self.assertNotIn("need_human", job.get("error") or "")

    def test_status_miss_streak_resets_when_sid_returns(self):
        sid = "sess-live"
        job = _running_job(self.store, session_id=sid)
        job["dispatched_at"] = time.time() - (SESSION_STATUS_GRACE_S + 1)
        job["status_miss_streak"] = SCAN_ERROR_LIMIT - 1
        with self.store.transaction():
            self.store.save(job)
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {sid: {"type": "busy"}}
        client.routes[("GET", f"/session/{sid}/message")] = [
            {"info": {"role": "assistant"}},
        ]
        engine = Engine(self.store, client)
        engine.tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running", job)
        self.assertEqual(job.get("status_miss_streak") or 0, 0)
        self.assertEqual(job.get("session_id"), sid)
        self.assertFalse(job.get("error"))
        client.routes[("GET", "/session/status")] = {}
        job["next_scan"] = 0
        with self.store.transaction():
            self.store.save(job)
        engine.tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "running", job)
        # In-flight assistant: soft-retry without bumping the streak.
        self.assertFalse(job.get("status_miss_streak"))
        self.assertEqual(job.get("session_id"), sid)
        self.assertFalse(job.get("error"))


if __name__ == "__main__":
    unittest.main()
