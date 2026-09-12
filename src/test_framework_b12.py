#!/usr/bin/env python3
"""Path-B knife12: Goal coordinator ownership + versioned plan revisions."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import load_charter  # noqa: E402
from framework.charter_map import map_charter_to_goal_task  # noqa: E402
from framework.goal_ownership import (  # noqa: E402
    OWNERSHIP_DIRNAME,
    REASON_ALREADY_OWNED,
    REASON_ILLEGAL_PLAN,
    REASON_NOT_COORDINATOR,
    REASON_STALE_OWNERSHIP,
    STATUS_COMMITTED,
    STATUS_REJECTED,
    GoalOwnershipStore,
    attach_goal_ownership,
    open_goal_ownership,
    persist_path,
    plan_from_mapped,
    reset_goal_ownership_cache,
    validate_plan_revision,
)
from framework.project_report import attach_framework_projection  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife12", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan(goal_id: str, tasks: list[dict]) -> dict:
    return {"goal_id": goal_id, "tasks": tasks}


class TestOneCoordinator(unittest.TestCase):
    def setUp(self):
        reset_goal_ownership_cache()

    def tearDown(self):
        reset_goal_ownership_cache()

    def test_claim_assigns_version_one(self):
        with tempfile.TemporaryDirectory() as td:
            store = GoalOwnershipStore.open("goal_own", td)
            out = store.claim("coord_a")
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["created"])
            own = store.current()
            self.assertEqual(own.coordinator_id, "coord_a")
            self.assertEqual(own.version, 1)
            self.assertGreater(own.updated_at, 0)
            self.assertEqual(store.effective_coordinator(), "coord_a")
            path = persist_path(td, "goal_own")
            self.assertTrue(path.is_file(), path)
            self.assertIn(OWNERSHIP_DIRNAME, str(path))
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["ownership"]["coordinator_id"], "coord_a")
            self.assertEqual(disk["ownership"]["version"], 1)

    def test_second_coordinator_cannot_claim(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_own", td, coordinator_id="coord_a")
            out = store.claim("coord_b")
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_ALREADY_OWNED)
            self.assertEqual(store.effective_coordinator(), "coord_a")
            self.assertEqual(store.current().version, 1)

    def test_same_coordinator_reclaim_does_not_bump(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_own", td, coordinator_id="coord_a")
            again = store.claim("coord_a")
            self.assertTrue(again["ok"], again)
            self.assertFalse(again["created"])
            self.assertFalse(again["bumped"])
            self.assertEqual(store.current().version, 1)


class TestHandoffBumpsVersion(unittest.TestCase):
    def setUp(self):
        reset_goal_ownership_cache()

    def tearDown(self):
        reset_goal_ownership_cache()

    def test_handoff_bumps_and_replaces_coordinator(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_own", td, coordinator_id="coord_a")
            v1 = store.current().version
            out = store.handoff("coord_a", "coord_b", expected_version=v1)
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["bumped"])
            own = store.current()
            self.assertEqual(own.coordinator_id, "coord_b")
            self.assertEqual(own.version, v1 + 1)
            self.assertEqual(own.predecessor_id, "coord_a")
            self.assertEqual(store.effective_coordinator(), "coord_b")

    def test_succession_same_coordinator_still_bumps(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_own", td, coordinator_id="coord_a")
            out = store.succeed("coord_a", expected_version=1)
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["bumped"])
            self.assertEqual(store.current().coordinator_id, "coord_a")
            self.assertEqual(store.current().version, 2)

    def test_reload_keeps_version(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_own", td, coordinator_id="coord_a")
            store.handoff("coord_a", "coord_b")
            reset_goal_ownership_cache()
            store2 = GoalOwnershipStore.open("goal_own", td)
            self.assertEqual(store2.effective_coordinator(), "coord_b")
            self.assertEqual(store2.current().version, 2)


class TestSuccessfulRevision(unittest.TestCase):
    def setUp(self):
        reset_goal_ownership_cache()

    def tearDown(self):
        reset_goal_ownership_cache()

    def test_successful_plan_revision(self):
        """Required: one successful revision through propose → kernel validate → commit."""
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_rev", td, coordinator_id="coord_a")
            plan = _plan(
                "goal_rev",
                [
                    {
                        "task_id": "task_one",
                        "goal_id": "goal_rev",
                        "title": "first",
                        "status": "queued",
                        "depends_on": [],
                    }
                ],
            )
            prop = store.propose_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=plan,
                reason="add first task",
            )
            self.assertEqual(prop.status, "proposed", prop.to_dict())
            self.assertEqual(prop.ownership_version, 1)
            committed = store.commit_revision(prop.proposal_id)
            self.assertTrue(committed["ok"], committed)
            self.assertEqual(committed["plan_revision"], 1)
            self.assertEqual(store.plan["tasks"][0]["task_id"], "task_one")
            self.assertEqual(store.proposals[prop.proposal_id].status, STATUS_COMMITTED)
            # ownership version is unchanged by a plan revision (only handoff bumps it)
            self.assertEqual(store.current().version, 1)
            self.assertEqual(store.current().coordinator_id, "coord_a")

    def test_submit_plan_revision_convenience(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_rev", td, coordinator_id="coord_a")
            plan = _plan(
                "goal_rev",
                [{"task_id": "task_a", "goal_id": "goal_rev", "status": "queued"}],
            )
            out = store.submit_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=plan,
                reason="initial",
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["plan_revision"], 1)
            self.assertEqual(len(store.plan["tasks"]), 1)


class TestStaleCoordinatorRejected(unittest.TestCase):
    def setUp(self):
        reset_goal_ownership_cache()

    def tearDown(self):
        reset_goal_ownership_cache()

    def test_stale_coordinator_submit_rejected(self):
        """Required: submit from an expired ownership instance is denied."""
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_stale", td, coordinator_id="coord_a")
            live_v = store.current().version
            store.handoff("coord_a", "coord_b", expected_version=live_v)
            self.assertEqual(store.current().version, live_v + 1)
            self.assertEqual(store.effective_coordinator(), "coord_b")

            stale_plan = _plan(
                "goal_stale",
                [{"task_id": "task_stale", "goal_id": "goal_stale", "status": "queued"}],
            )
            out = store.submit_plan_revision(
                coordinator_id="coord_a",
                ownership_version=live_v,
                plan=stale_plan,
                reason="expired instance",
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_STALE_OWNERSHIP)
            self.assertIn("stale", (out.get("error") or "").lower())
            self.assertEqual(store.plan_revision, 0)
            self.assertEqual(store.plan.get("tasks"), [])
            rec = store.proposals[out["proposal_id"]]
            self.assertEqual(rec.status, STATUS_REJECTED)
            self.assertEqual(rec.reject_reason, REASON_STALE_OWNERSHIP)

    def test_propose_then_handoff_then_commit_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_stale", td, coordinator_id="coord_a")
            plan = _plan(
                "goal_stale",
                [{"task_id": "task_x", "goal_id": "goal_stale", "status": "queued"}],
            )
            prop = store.propose_plan_revision(
                coordinator_id="coord_a",
                ownership_version=1,
                plan=plan,
            )
            self.assertEqual(prop.status, "proposed")
            store.handoff("coord_a", "coord_b")
            committed = store.commit_revision(prop.proposal_id)
            self.assertFalse(committed["ok"], committed)
            self.assertEqual(committed["reason"], REASON_STALE_OWNERSHIP)
            self.assertEqual(store.plan_revision, 0)

    def test_new_coordinator_old_version_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_stale", td, coordinator_id="coord_a")
            store.handoff("coord_a", "coord_b")
            plan = _plan(
                "goal_stale",
                [{"task_id": "task_y", "goal_id": "goal_stale", "status": "queued"}],
            )
            ok, why = store.check_submitter("coord_b", 1)
            self.assertFalse(ok)
            self.assertEqual(why, REASON_STALE_OWNERSHIP)
            out = store.submit_plan_revision(
                coordinator_id="coord_b",
                ownership_version=1,
                plan=plan,
            )
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], REASON_STALE_OWNERSHIP)

    def test_wrong_coordinator_current_version_not_coordinator(self):
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("goal_stale", td, coordinator_id="coord_a")
            ok, why = store.check_submitter("coord_intruder", 1)
            self.assertFalse(ok)
            self.assertEqual(why, REASON_NOT_COORDINATOR)


class TestPlanLegality(unittest.TestCase):
    def test_cannot_rewrite_ownership_in_plan(self):
        ok, why = validate_plan_revision(
            {"tasks": [], "coordinator_id": "hijack"},
            goal_id="g1",
        )
        self.assertFalse(ok)
        self.assertIn("ownership", why)

    def test_illegal_status_transition(self):
        current = {
            "goal_id": "g1",
            "tasks": [{"task_id": "t1", "goal_id": "g1", "status": "succeeded"}],
        }
        nxt = {
            "goal_id": "g1",
            "tasks": [{"task_id": "t1", "goal_id": "g1", "status": "running"}],
        }
        ok, why = validate_plan_revision(nxt, goal_id="g1", current_plan=current)
        self.assertFalse(ok)
        self.assertIn("transition", why)

    def test_cannot_drop_running_task(self):
        current = {
            "goal_id": "g1",
            "tasks": [{"task_id": "t1", "goal_id": "g1", "status": "running"}],
        }
        nxt = {"goal_id": "g1", "tasks": []}
        ok, why = validate_plan_revision(nxt, goal_id="g1", current_plan=current)
        self.assertFalse(ok)
        self.assertIn("drop", why)

    def test_submit_illegal_plan_rejected(self):
        reset_goal_ownership_cache()
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("g1", td, coordinator_id="c1")
            out = store.submit_plan_revision(
                coordinator_id="c1",
                ownership_version=1,
                plan={"tasks": [], "ownership_version": 99},
            )
            self.assertFalse(out["ok"], out)
            self.assertEqual(out["reason"], REASON_ILLEGAL_PLAN)
        reset_goal_ownership_cache()


class TestCharterProjection(unittest.TestCase):
    def test_hello_has_no_coordinator_hint(self):
        ch = load_charter(HELLO)
        out = map_charter_to_goal_task(ch)
        self.assertNotIn("role_hints", out["goal"])
        self.assertNotIn("coordinator_id", out)

    def test_coordinator_maps_to_role_hints(self):
        ch = load_charter(HELLO)
        ch["coordinator_id"] = "coord_hello"
        out = map_charter_to_goal_task(ch)
        self.assertEqual(out["coordinator_id"], "coord_hello")
        self.assertEqual(out["goal"]["role_hints"]["coordinator"], "coord_hello")
        plan = plan_from_mapped(out)
        self.assertEqual(plan["goal_id"], out["goal"]["goal_id"])
        self.assertEqual(plan["tasks"][0]["task_id"], out["task"]["task_id"])

    def test_projection_does_not_flip_ok(self):
        ch = load_charter(HELLO)
        ch["coordinator_id"] = "coord_hello"
        report = {"ok": True, "state": "ok", "error": ""}
        attach_framework_projection(report, ch)
        self.assertTrue(report["ok"])
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["framework_projection"]["coordinator_id"], "coord_hello")
        self.assertTrue(report["framework_projection"]["readonly"])

    def test_attach_ownership_snapshot_readonly(self):
        reset_goal_ownership_cache()
        with tempfile.TemporaryDirectory() as td:
            store = open_goal_ownership("g1", td, coordinator_id="c1")
            result = {"ok": True, "state": "ok"}
            out = attach_goal_ownership(result, store)
            self.assertTrue(out["ok"])
            self.assertEqual(out["goal_ownership"]["coordinator_id"], "c1")
            self.assertEqual(out["goal_ownership"]["version"], 1)
            self.assertTrue(out["goal_ownership"]["readonly"])
        reset_goal_ownership_cache()


class TestRunJobCliKnife12(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_run_job()

    def _run(self, argv, env_extra=None):
        env = os.environ.copy()
        env.pop("COLLAB_EXECUTION_BACKEND", None)
        env.pop("COLLAB_INPROCESS_REVIEW_STUB", None)
        if env_extra:
            env.update(env_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = self.mod.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_hello_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["state"], "dry_run")
            self.assertNotIn("backend", summary)
            self.assertNotIn("goal_ownership", summary)

    def test_default_entry_not_inprocess(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")
            self.assertNotIn("backend", summary)


if __name__ == "__main__":
    unittest.main()
