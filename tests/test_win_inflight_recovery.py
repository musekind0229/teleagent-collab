"""Simulated tests: Windows in-flight recovery after TeleAgent restart/port/cred change."""
from __future__ import annotations

import tempfile
import time
import unittest

from win_collab.core import SCAN_ERROR_LIMIT, TERMINAL, Engine, Store


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
        _running_job(self.store, session_id="sess-gone")
        client = FakeClient()
        client.routes[("GET", "/permission")] = []
        client.routes[("GET", "/question")] = []
        client.routes[("GET", "/session/status")] = {}
        Engine(self.store, client).tick()
        job = self.store.get("jobdeadbeef01")
        self.assertEqual(job["state"], "failed")
        self.assertIn("need_human", job["error"])
        self.assertIn("session lost", job["error"])
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


if __name__ == "__main__":
    unittest.main()
