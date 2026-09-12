#!/usr/bin/env python3
"""Path-B knife4: public ids on records + readonly Task/Run lifecycle projection."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.id_projection import stable_goal_task_ids  # noqa: E402
from framework.lifecycle import project_scheduler_state  # noqa: E402
from scheduler import JobState, ParallelScheduler  # noqa: E402
from state_store import DecisionRecord, JobRecord, StateStore  # noqa: E402


def _base_charter(**over):
    c = {
        "name": "b4",
        "goal": "g",
        "must": ["m"],
        "must_not": ["n"],
        "timeout_sec": 60,
    }
    c.update(over)
    return c


class TestStableIds(unittest.TestCase):
    def test_stable_across_calls(self):
        a = stable_goal_task_ids("job1", {"name": "b4"})
        b = stable_goal_task_ids("job1", {"name": "b4"})
        self.assertEqual(a, b)
        self.assertNotEqual(a[0], stable_goal_task_ids("job2", {"name": "b4"})[0])


class TestLifecycleProjection(unittest.TestCase):
    def test_pending_maps_awaiting(self):
        p = project_scheduler_state("pending_approval", goal_id="g1", task_id="t1")
        self.assertEqual(p["task_state"], "awaiting_decision")
        self.assertEqual(p["run_state"], "awaiting_decision")
        self.assertTrue(p["readonly"])
        self.assertEqual(p["goal_id"], "g1")

    def test_unknown_job_state_not_success(self):
        p = project_scheduler_state("not_a_real_state")
        self.assertEqual(p["task_state"], "unknown")
        self.assertEqual(p["run_state"], "unknown")

    def test_timeout_error_class(self):
        p = project_scheduler_state("timeout")
        self.assertEqual(p["task_state"], "failed")
        self.assertEqual(p["error_class"], "budget_exhausted")


class TestRecordsPublicIds(unittest.TestCase):
    def test_decision_request_id_roundtrip(self):
        d = DecisionRecord(
            decision_id="dec1",
            job_id="j",
            permission_id="per_1",
            request_id="per_1",
            reply="once",
            via="hard_rule",
            goal_id="goal_x",
            task_id="task_x",
        )
        back = DecisionRecord.from_dict(d.to_dict())
        self.assertEqual(back.request_id, "per_1")
        self.assertEqual(back.goal_id, "goal_x")
        self.assertEqual(back.task_id, "task_x")

    def test_old_decision_row(self):
        back = DecisionRecord.from_dict(
            {
                "decision_id": "d",
                "job_id": "j",
                "permission_id": "old",
                "reply": "reject",
                "via": "x",
            }
        )
        self.assertEqual(back.request_id, "old")

    def test_job_record_ids_persist(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(root=td, run_id="b4")
            rec = JobRecord(
                job_id="job_a",
                name="n",
                state="queued",
                charter={"goal": "g", "must": [], "must_not": []},
                goal_id="goal_a",
                task_id="task_a",
            )
            store.upsert_job(rec)
            store2 = StateStore(root=td, run_id="b4")
            got = store2.get_job("job_a")
            self.assertIsNotNone(got)
            self.assertEqual(got.goal_id, "goal_a")
            self.assertEqual(got.task_id, "task_a")


class TestSchedulerProjection(unittest.TestCase):
    def test_enqueue_sets_ids_and_attach_projects(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                max_parallel=1,
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                dry_run=True,
                persist=False,
            )
            job = sched.enqueue_charter(_base_charter())
            self.assertTrue(job.goal_id.startswith("goal_"))
            self.assertTrue(job.task_id.startswith("task_"))
            job.state = JobState.PENDING_APPROVAL
            job.result = {"ok": True, "state": "pending"}
            sched._attach_lifecycle_projection(job)
            self.assertIn("framework_lifecycle", job.result)
            self.assertTrue(job.result["framework_lifecycle"]["readonly"])
            self.assertEqual(job.result["framework_lifecycle"]["task_state"], "awaiting_decision")
            self.assertEqual(job.result["ok"], True)  # projection must not flip ok
            self.assertEqual(job.result["framework_lifecycle"]["goal_id"], job.goal_id)

    def test_refresh_queued_still_projects(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sched = ParallelScheduler(
                max_parallel=1,
                workspaces_root=root / "ws",
                runs_root=root / "runs",
                dry_run=True,
                persist=False,
            )
            job = sched.enqueue_charter(_base_charter())
            self.assertEqual(job.state, JobState.QUEUED)
            sched.refresh_job_status(job)
            self.assertEqual(job.state, JobState.QUEUED)
            self.assertEqual(job.result["framework_lifecycle"]["task_state"], "queued")


if __name__ == "__main__":
    unittest.main()
