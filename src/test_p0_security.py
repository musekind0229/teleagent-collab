#!/usr/bin/env python3
"""条1 security unit tests — simulated (no live TeleAgent)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pathutil import canonicalize, is_path_within, permission_fingerprint
from decision_packet import packet_from_permission, PingDeduper
from hard_rules import path_allowlisted, hard_rule_decision
from scheduler import ParallelScheduler, smoke_parallel_isolation, smoke_serial_short_poll


class TestPathBoundary(unittest.TestCase):
    def test_is_path_within_rejects_prefix_sibling(self):
        self.assertTrue(is_path_within("/workspace/job/a.py", "/workspace/job"))
        self.assertFalse(is_path_within("/workspace/job-evil/a.py", "/workspace/job"))
        self.assertFalse(is_path_within("/workspace/foobar/x", "/workspace/foo"))

    def test_allow_paths_no_startswith_hole(self):
        charter = {"allow_paths": ["/workspace/job"], "allow_secret_globs": []}
        self.assertFalse(path_allowlisted("/workspace/job-evil/.env", charter=charter))
        self.assertTrue(path_allowlisted("/workspace/job/sub/.env", charter=charter))


class TestPacketAllPaths(unittest.TestCase):
    def test_all_paths_patterns_ops(self):
        pkt = packet_from_permission(
            {
                "id": "p-multi",
                "path": "/ws/a.py",
                "paths": ["/ws/b.py", "/ws/.env"],
                "patterns": ["**/.env*", "**/token*"],
                "ops": ["read", "edit"],
                "tool": "read",
            },
            worker_intent="need all targets in packet",
            blocker={"detail": "gate"},
            charter={"goal": "g"},
        )
        pa = pkt["proposed_action"]
        self.assertIn("/ws/a.py", pa["targets"])
        self.assertIn("/ws/b.py", pa["targets"])
        self.assertIn("/ws/.env", pa["targets"])
        self.assertEqual(pa["patterns"], ["**/.env*", "**/token*"])
        self.assertIn("read", pa["ops"])
        self.assertIn("edit", pa["ops"])
        # worst class among targets
        self.assertIn(pa["target_class"], ("env_file", "user_secret_store"))  # worst among targets/patterns


class TestDedupeByRequestId(unittest.TestCase):
    def test_same_class_new_id_allowed(self):
        d = PingDeduper()
        self.assertTrue(d.should_emit_id("req-1"))
        d.mark_replied_id("req-1")
        self.assertTrue(d.already_handled("req-1"))
        # same pattern/class, new id → still ok
        self.assertTrue(d.should_emit("permission", "source", "**/*.py"))
        self.assertTrue(d.should_emit_id("req-2"))


class TestNoUnconditionalOnce(unittest.TestCase):
    def test_ordinary_rw_calls_lead(self):
        leads = []

        def fake_lead(prompt, schema, cwd):
            leads.append(1)
            return json.dumps({"decision": "once"}), {"decision": "once"}

        root = Path("/workspace/teleagent-collab/jobs/workspaces/_p0_sec")
        sched = ParallelScheduler(
            max_parallel=1,
            workspaces_root=root,
            dry_run=True,
            call_lead_fn=fake_lead,
            idle_min=10,
            idle_max=10,
            busy_min=1,
            busy_max=1,
        )
        charter = {
            "name": "ord-rw",
            "goal": "edit source",
            "must": [],
            "must_not": [],
            "allow_secret_globs": [],
            "allow_paths": [],
            "allow_keys": [],
            "done_when": {"artifacts": ["out.txt"]},
            "timeout_sec": 60,
        }
        job = sched.enqueue_charter(
            charter,
            simulated_pending=[
                {"id": "ord-1", "path": "", "tool": "edit", "permission": "edit"},
            ],
        )
        job.simulated_pending[0]["path"] = str(job.workdir / "worker.py")
        sched.tick()
        self.assertEqual(len(leads), 1)
        self.assertTrue(any(r.get("via") == "lead" for r in job.api_replies))
        self.assertFalse(any(r.get("via") == "ordinary_rw_no_lead" for r in job.api_replies))
        sched.shutdown()


class TestReconfirm(unittest.TestCase):
    def test_fingerprint_changes(self):
        a = {"id": "p1", "sessionID": "s", "path": "/a", "tool": "read"}
        b = {"id": "p1", "sessionID": "s", "path": "/b", "tool": "read"}
        self.assertNotEqual(permission_fingerprint(a), permission_fingerprint(b))

    def test_reply_skipped_when_reconfirm_fails(self):
        calls = []

        def fake_ta(method, path, body=None, extra_headers=None, timeout=120):
            calls.append((method, path, body))
            if method == "GET" and path == "/permission":
                return 200, []  # no longer pending
            return 200, {}

        root = Path("/workspace/teleagent-collab/jobs/workspaces/_p0_reconfirm")
        sched = ParallelScheduler(
            max_parallel=1,
            workspaces_root=root,
            dry_run=False,  # force reconfirm path
            teleagent_call=fake_ta,
            call_lead_fn=lambda *a, **k: (json.dumps({"decision": "once"}), {"decision": "once"}),
        )
        # manually craft a job mid-flight
        from scheduler import JobSlot, JobState

        job = JobSlot(
            job_id="j1",
            name="j1",
            charter={"name": "j1", "goal": "g", "must": [], "must_not": []},
            instruction="x",
            expected_artifacts=[],
            workdir=root / "j1",
            session_id="ses_x",
            state=JobState.PENDING_APPROVAL,
        )
        job.workdir.mkdir(parents=True, exist_ok=True)
        sched.jobs[job.job_id] = job
        ok = sched._reply(
            job,
            "gone-1",
            "once",
            via="lead",
            original={"id": "gone-1", "sessionID": "ses_x", "path": "/x"},
        )
        self.assertFalse(ok)
        self.assertNotIn("gone-1", job.handled_perm_ids)
        # should not POST reply when reconfirm fails
        self.assertFalse(any(c[0] == "POST" and "reply" in c[1] for c in calls))
        sched.shutdown()


class TestSmokesStillPass(unittest.TestCase):
    def test_iso_and_serial(self):
        s = smoke_parallel_isolation(n=3)
        self.assertTrue(s["isolation_ok"])
        self.assertGreaterEqual(s["stats"]["lead_calls"], 1)
        s2 = smoke_serial_short_poll()
        self.assertTrue(s2["serial_ok"])
        self.assertEqual(s2["lead_calls"], 2)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
