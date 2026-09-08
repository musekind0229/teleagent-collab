#!/usr/bin/env python3
"""Astra P1 regression: cancel abort, status gate, restore contract, unbound lead reject."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scheduler import JobState, ParallelScheduler
from state_store import CONTRACT_VERSION, JobRecord, StateStore


def _charter(**over):
    c = {
        "name": "astra",
        "goal": "write OK",
        "must": ["only workdir"],
        "must_not": ["no network", "no secrets"],
        "allow_secret_globs": [],
        "allow_paths": [],
        "allow_keys": [],
        "done_when": {"artifacts": ["out.txt"]},
        "acceptance": "out.txt == OK",
        "force_lead_review": True,
        "timeout_sec": 120,
        "task_kind": "file_task",
    }
    c.update(over)
    return c


class TestCancelAbort(unittest.TestCase):
    def test_effect_cancel_calls_abort_and_pending_on_fail(self):
        calls = []

        def transport(method, path, *a, **k):
            calls.append((method, path))
            return 500, {"error": "abort failed"}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter())
            job.session_id = "sess-1"
            job.state = JobState.RUNNING
            sched.request_cancel(job.job_id)
            out = sched.effect_cancel(job.job_id)
            self.assertTrue(any(m == "POST" and p.endswith("/abort") for m, p in calls))
            self.assertFalse(out.get("cancel_effected"))
            self.assertTrue(out.get("stop_pending_confirm"))
            self.assertEqual(job.state, JobState.CANCEL_REQUESTED)
            sched.shutdown()

    def test_effect_cancel_effected_when_abort_ok(self):
        calls = []

        def transport(method, path, *a, **k):
            calls.append((method, path))
            if method == "POST" and path.endswith("/abort"):
                return 200, {"ok": True}
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter())
            job.session_id = "sess-1"
            job.state = JobState.RUNNING
            sched.request_cancel(job.job_id)
            out = sched.effect_cancel(job.job_id)
            self.assertTrue(out.get("cancel_effected"))
            self.assertEqual(job.state, JobState.CANCELLED)
            self.assertTrue(any(p.endswith("/session/sess-1/abort") for _, p in calls))
            sched.shutdown()

    def test_timeout_requests_abort(self):
        calls = []
        aborted = {"v": False}

        def transport(method, path, *a, **k):
            calls.append((method, path))
            if method == "POST" and path.endswith("/abort"):
                aborted["v"] = True
                return 200, {}
            # After abort accepted, status goes idle so stop can be confirmed.
            if aborted["v"]:
                return 200, {"sess-t": {"type": "idle"}}
            return 200, {"sess-t": {"type": "busy"}}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(timeout_sec=2, force_lead_review=False))
            job.session_id = "sess-t"
            job.state = JobState.RUNNING
            job.started_at = time.time() - 10
            Path(job.expected_artifacts[0]).write_text("x")
            sched.refresh_job_status(job)
            self.assertEqual(job.state, JobState.TIMEOUT)
            self.assertTrue(any(p.endswith("/abort") for _, p in calls))
            sched.shutdown()

    def test_abort_202_busy_not_cancel_effected(self):
        calls = []

        def transport(method, path, *a, **k):
            calls.append((method, path))
            if path.endswith("/abort"):
                return 202, {"accepted": True}
            return 200, {"probe-session": {"type": "busy"}}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter())
            job.session_id = "probe-session"
            job.state = JobState.RUNNING
            sched.request_cancel(job.job_id)
            out = sched.effect_cancel(job.job_id)
            self.assertFalse(out.get("cancel_effected"))
            self.assertTrue(out.get("stop_pending_confirm"))
            self.assertEqual(job.state, JobState.CANCEL_REQUESTED)
            self.assertTrue(any(p.endswith("/abort") for _, p in calls))
            self.assertTrue(any(p == "/session/status" for _, p in calls))
            sched.shutdown()

    def test_timeout_abort_202_busy_stays_pending(self):
        calls = []

        def transport(method, path, *a, **k):
            calls.append((method, path))
            if path.endswith("/abort"):
                return 202, {"accepted": True}
            return 200, {"sess-t": {"type": "busy"}}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(timeout_sec=2, force_lead_review=False))
            job.session_id = "sess-t"
            job.state = JobState.RUNNING
            job.started_at = time.time() - 10
            Path(job.expected_artifacts[0]).write_text("x")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.TIMEOUT)
            self.assertNotEqual(job.state, JobState.CANCELLED)
            self.assertTrue((job.result or {}).get("stop_pending_confirm"))
            sched.shutdown()


class TestCompletionStatusGate(unittest.TestCase):
    def test_status_500_not_done(self):
        def transport(method, path, *a, **k):
            return 500, {"error": "unavailable"}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("wrong content")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            sched.shutdown()

    def test_idle_without_finish_not_done(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"sess": {"type": "idle"}}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant"}}]  # no finish
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            sched.shutdown()

    def test_finish_stop_allows_done_without_force(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"sess": {"type": "idle"}}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertEqual(job.state, JobState.DONE)
            self.assertTrue((job.result or {}).get("ok"))
            sched.shutdown()

    def _lead_pass(self, prompt, schema, cwd):
        req = json.loads(prompt)["_lead_request"]
        body = {
            "application_id": req["application_id"],
            "context_summary": req["context_summary"],
            "verdict": "pass",
            "reason": "bound pass",
        }
        return json.dumps(body), body

    def test_force_review_message_500_not_ok(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"sess": {"type": "idle"}}
            if "/message" in path:
                return 500, {"error": "unavailable"}
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                call_lead_fn=self._lead_pass,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=True))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            sched.shutdown()

    def test_force_review_cancelled_not_ok(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"sess": {"type": "idle"}}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "cancelled"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                call_lead_fn=self._lead_pass,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=True))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            self.assertEqual((job.result or {}).get("finish"), "cancelled")
            sched.shutdown()

    def test_malformed_status_string_not_done(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, "not a status object"
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            sched.shutdown()


class TestRestoreContract(unittest.TestCase):
    def test_persist_restore_full_charter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = StateStore(root=root / "state", run_id="c")
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                persist=True,
                state_store=store,
                dry_run=True,
            )
            job = sched.enqueue_charter(_charter())
            job.state = JobState.RUNNING
            job.session_id = "dry-x"
            job.started_at = time.time()
            sched._persist_job(job)
            rec = store.get_job(job.job_id)
            self.assertEqual(rec.contract_version, CONTRACT_VERSION)
            self.assertEqual(rec.charter["must_not"], ["no network", "no secrets"])
            self.assertTrue(rec.force_lead_review)
            self.assertTrue(rec.expected_artifacts)

            sched2 = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                persist=True,
                state_store=store,
                dry_run=True,
            )
            plan = sched2.restore_from_store()
            self.assertEqual(plan.get("blocked"), 0)
            r = sched2.jobs[job.job_id]
            self.assertEqual(r.charter["must"], ["only workdir"])
            self.assertEqual(r.charter["must_not"], ["no network", "no secrets"])
            self.assertTrue(r.force_lead_review)
            self.assertTrue(r.expected_artifacts)
            sched.shutdown()
            sched2.shutdown()

    def test_missing_contract_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = StateStore(root=root / "state", run_id="b")
            # Legacy-shaped record without charter/contract_version
            store.upsert_job(
                JobRecord(
                    job_id="legacy",
                    name="legacy",
                    state="running",
                    session_id="s",
                    workdir=str(root / "ws" / "legacy"),
                    contract_version=0,
                    charter={},
                )
            )
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                persist=True,
                state_store=store,
                dry_run=True,
            )
            plan = sched.restore_from_store()
            self.assertGreaterEqual(plan.get("blocked"), 1)
            self.assertEqual(sched.jobs["legacy"].state, JobState.FAIL)
            self.assertIn("restore_contract_blocked", (sched.jobs["legacy"].result or {}).get("error", ""))
            sched.shutdown()

    def test_restart_new_permission_approve_deliver(self):
        """Integration: restore full contract → new permission → approve → DONE."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = StateStore(root=root / "state", run_id="int")

            def fake_lead(prompt, schema, cwd):
                # dry_run stitching will bind; return once for permission
                return json.dumps({"decision": "once", "reason": "allow"}), {
                    "decision": "once",
                    "reason": "allow",
                }

            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                persist=True,
                state_store=store,
                dry_run=True,
                call_lead_fn=fake_lead,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.simulated_pending = [
                {
                    "id": "perm-old",
                    "path": str(job.workdir / "a.sh"),
                    "tool": "bash",
                    "permission": "bash",
                    "command": "echo 1",
                }
            ]
            sched.tick()
            for _ in range(5):
                if "perm-old" in job.handled_perm_ids:
                    break
                sched.tick()
            self.assertIn("perm-old", job.handled_perm_ids)
            sid = job.session_id
            jid = job.job_id

            # Restart
            sched2 = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                persist=True,
                state_store=store,
                dry_run=True,
                call_lead_fn=fake_lead,
            )
            plan = sched2.restore_from_store()
            self.assertEqual(plan.get("blocked"), 0)
            restored = sched2.jobs[jid]
            self.assertEqual(restored.charter["must_not"], ["no network", "no secrets"])
            self.assertTrue(restored.expected_artifacts)
            restored.state = JobState.RUNNING
            restored.session_id = sid
            # New permission after restart
            restored.simulated_pending = [
                {
                    "id": "perm-new",
                    "path": str(restored.workdir / "b.sh"),
                    "tool": "bash",
                    "permission": "bash",
                    "command": "echo 2",
                }
            ]
            for _ in range(8):
                sched2.tick()
                if "perm-new" in restored.handled_perm_ids and restored.state == JobState.DONE:
                    break
            self.assertIn("perm-new", restored.handled_perm_ids)
            # Drain pending and complete
            restored.simulated_pending.clear()
            Path(restored.expected_artifacts[0]).write_text("OK\n")
            sched2.refresh_job_status(restored)
            self.assertEqual(restored.state, JobState.DONE)
            self.assertTrue((restored.result or {}).get("ok"))
            sched.shutdown()
            sched2.shutdown()


class TestUnboundLeadRejected(unittest.TestCase):
    def test_production_rejects_unbound_review(self):
        def unbound(*a, **k):
            return '{"verdict":"pass","reason":"x"}', {"verdict": "pass", "reason": "x"}

        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"sess": {"type": "idle"}}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                call_lead_fn=unbound,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=True))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            sched.shutdown()

    def test_wrong_application_id_rejected(self):
        def bad_id(*a, **k):
            body = {
                "application_id": "wrong-id",
                "context_summary": "nope",
                "verdict": "pass",
                "reason": "x",
            }
            return json.dumps(body), body

        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"sess": {"type": "idle"}}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                call_lead_fn=bad_id,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=True))
            job.session_id = "sess"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            sched.shutdown()



class TestStopConfirmThreeState(unittest.TestCase):
    def _cancel_with_status(self, status):
        calls = []

        def transport(method, path, *a, **k):
            calls.append((method, path))
            if method == "POST" and path.endswith("/abort"):
                return 202, {"accepted": True}
            return 200, status

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter())
            job.session_id = "s"
            job.state = JobState.RUNNING
            sched.request_cancel(job.job_id)
            out = sched.effect_cancel(job.job_id)
            sched.shutdown()
            return out, job, calls

    def test_error_dict_not_cancel_effected(self):
        out, job, calls = self._cancel_with_status({"error": "backend not ready"})
        self.assertFalse(out.get("cancel_effected"))
        self.assertTrue(out.get("stop_pending_confirm"))
        self.assertEqual(job.state, JobState.CANCEL_REQUESTED)
        self.assertTrue(any(p == "/session/status" for _, p in calls))

    def test_unrecognized_state_not_cancel_effected(self):
        out, job, _ = self._cancel_with_status({"s": {"type": "unrecognized-state"}})
        self.assertFalse(out.get("cancel_effected"))
        self.assertEqual(job.state, JobState.CANCEL_REQUESTED)

    def test_empty_map_still_confirms_idle(self):
        out, job, _ = self._cancel_with_status({})
        self.assertTrue(out.get("cancel_effected"))
        self.assertEqual(job.state, JobState.CANCELLED)

    def test_error_dict_blocks_completion(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"error": "backend not ready"}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "s"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertTrue(job.busy)
            sched.shutdown()

    def test_unrecognized_state_blocks_completion(self):
        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"s": {"type": "unrecognized-state"}}
            if "/message" in path:
                return 200, [{"info": {"role": "assistant", "finish": "stop"}}]
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "s"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            Path(job.expected_artifacts[0]).write_text("OK")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            sched.shutdown()


class TestThisRoundCompletion(unittest.TestCase):
    def test_previous_turn_stop_not_done(self):
        msgs = [
            {"info": {"id": "u-old", "role": "user"}},
            {"info": {"id": "a-old", "role": "assistant", "parentID": "u-old", "finish": "stop"}},
            {"info": {"id": "u-new", "role": "user"}},
        ]

        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {}
            if "/message" in path:
                return 200, msgs
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "s"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            job.dispatch_user_message_id = "u-old"
            Path(job.expected_artifacts[0]).write_text("OLD")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            sched.shutdown()

    def test_this_turn_stop_allows_done(self):
        msgs = [
            {"info": {"id": "u-old", "role": "user"}},
            {"info": {"id": "a-old", "role": "assistant", "parentID": "u-old", "finish": "stop"}},
            {"info": {"id": "u-new", "role": "user"}},
            {"info": {"id": "a-new", "role": "assistant", "parentID": "u-new", "finish": "stop"}},
        ]

        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {"s": {"type": "idle"}}
            if "/message" in path:
                return 200, msgs
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=False))
            job.session_id = "s"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            job.dispatch_user_message_id = "u-new"
            Path(job.expected_artifacts[0]).write_text("NEW")
            sched.refresh_job_status(job)
            self.assertEqual(job.state, JobState.DONE)
            self.assertTrue((job.result or {}).get("ok"))
            sched.shutdown()

    def test_force_review_previous_turn_not_ok(self):
        msgs = [
            {"info": {"id": "u-old", "role": "user"}},
            {"info": {"id": "a-old", "role": "assistant", "parentID": "u-old", "finish": "stop"}},
            {"info": {"id": "u-new", "role": "user"}},
        ]

        def lead(prompt, schema, cwd):
            req = json.loads(prompt)["_lead_request"]
            body = {
                "application_id": req["application_id"],
                "context_summary": req["context_summary"],
                "verdict": "pass",
                "reason": "bound pass",
            }
            return json.dumps(body), body

        def transport(method, path, *a, **k):
            if path == "/session/status":
                return 200, {}
            if "/message" in path:
                return 200, msgs
            return 200, {}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                teleagent_call=transport,
                call_lead_fn=lead,
                persist=False,
                dry_run=False,
            )
            job = sched.enqueue_charter(_charter(force_lead_review=True))
            job.session_id = "s"
            job.state = JobState.RUNNING
            job.started_at = time.time()
            job.dispatch_user_message_id = "u-old"
            Path(job.expected_artifacts[0]).write_text("OLD")
            sched.refresh_job_status(job)
            self.assertNotEqual(job.state, JobState.DONE)
            self.assertFalse((job.result or {}).get("ok"))
            sched.shutdown()


class TestLinuxLoopbackGuard(unittest.TestCase):
    def test_non_loopback_blocked(self):
        from teleagent_adapter.base import AdapterError
        from teleagent_adapter.linux_local_v1 import LinuxLocalV1Adapter

        with self.assertRaises(AdapterError):
            LinuxLocalV1Adapter(base_url="http://example.com:4399")

    def test_redirect_sends_zero_credentials_to_sink(self):
        """Refuse redirect before a second hop sees auth headers."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from teleagent_adapter.base import AdapterError
        from teleagent_adapter.linux_local_v1 import LinuxLocalV1Adapter

        received = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append({
                    "authorization_present": bool(self.headers.get("Authorization")),
                    "signature_present": bool(self.headers.get("X-SA-Signature")),
                })
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.2:{sink.server_port}/sink")
                self.end_headers()

            def log_message(self, *args):
                pass

        sink = ThreadingHTTPServer(("127.0.0.2", 0), Sink)
        source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (source, sink):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            adapter = LinuxLocalV1Adapter(
                base_url=f"http://127.0.0.1:{source.server_port}",
                find_creds_fn=lambda: ("synthetic-user", "synthetic-password", "synthetic-key"),
            )
            with self.assertRaises(AdapterError):
                adapter.call("GET", "/probe", timeout=3)
            self.assertEqual(received, [])
        finally:
            for server in (source, sink):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
