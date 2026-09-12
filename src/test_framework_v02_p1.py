#!/usr/bin/env python3
"""v0.2 P1: minimal delegation semantics + durable simple caller."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
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
from framework.delegation import (  # noqa: E402
    AUTONOMY_BOUNDED,
    AUTONOMY_EXPLICIT_PLAN,
    EVENT_CLASS_DECISION,
    KIND_ESCALATE_INSUFFICIENT_AUTH,
    KIND_ESCALATE_OUT_OF_SCOPE,
    KIND_ESCALATE_OVER_BUDGET,
)
from framework.durable_api import (  # noqa: E402
    REASON_ACTOR_NOT_AUTHORIZED,
    REASON_SUBMIT_CONTENT_CONFLICT,
    DurableLayer,
    reset_durable_cache,
)
from framework.goal_ownership import (  # noqa: E402
    REASON_AUTONOMY_DENIED,
    GoalOwnershipStore,
    reset_goal_ownership_cache,
)

HELLO = REPO / "jobs/examples/hello.charter.yaml"
CLI = REPO / "bin" / "durable-cli.py"
DEMO = REPO / "bin" / "delegate-demo.py"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_v02_p1", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _P1Case(unittest.TestCase):
    def setUp(self):
        reset_durable_cache()
        reset_goal_ownership_cache()

    def tearDown(self):
        reset_durable_cache()
        reset_goal_ownership_cache()


class TestAutonomyStored(_P1Case):
    def test_autonomy_flag_stored_on_submit_and_get(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            out = layer.submit_goal(
                submit_key="sk-auto",
                title="bounded",
                desired_outcome="stay in contract",
                autonomy="bounded_autonomy",
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["autonomy"]["mode"], AUTONOMY_BOUNDED)
            self.assertTrue(out["autonomy"]["allow_local_plan"])
            self.assertTrue(out["autonomy"]["allow_rework"])
            self.assertTrue(out["autonomy"]["allow_reassign"])
            got = layer.get_goal(out["goal_id"])
            self.assertEqual(got["autonomy"]["mode"], AUTONOMY_BOUNDED)
            self.assertEqual(got["goal"]["autonomy"]["mode"], AUTONOMY_BOUNDED)
            self.assertEqual(got["goal"]["goal"]["role_hints"]["autonomy"], AUTONOMY_BOUNDED)

    def test_explicit_plan_stored(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            out = layer.submit_goal(
                submit_key="sk-plan",
                title="planned",
                desired_outcome="execute given plan",
                autonomy="explicit_plan",
                tasks=[{"title": "step-1", "status": "queued"}],
            )
            self.assertTrue(out["ok"], out)
            auto = layer.get_goal(out["goal_id"])["autonomy"]
            self.assertEqual(auto["mode"], AUTONOMY_EXPLICIT_PLAN)
            self.assertFalse(auto["allow_local_plan"])
            self.assertFalse(auto["allow_rework"])
            self.assertFalse(auto["allow_reassign"])

    def test_unknown_autonomy_refused(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            bad = layer.submit_goal(
                submit_key="sk-bad",
                title="x",
                desired_outcome="x",
                autonomy="total_freedom",
            )
            self.assertFalse(bad["ok"], bad)
            self.assertEqual(bad["reason"], "unknown_autonomy")
            self.assertEqual(layer.goal_count(), 0)


class TestSubmitterIdentity(_P1Case):
    def test_submitter_and_external_ref_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            out = layer.submit_goal(
                submit_key="sk-sub",
                title="audit me",
                desired_outcome="record submitter",
                submitter_id="upper-1",
                external_goal_ref="parent:week-beta",
                autonomy="explicit_plan",
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["submitter_id"], "upper-1")
            self.assertEqual(out["external_goal_ref"], "parent:week-beta")
            got = layer.get_goal(out["goal_id"])
            self.assertEqual(got["submitter_id"], "upper-1")
            self.assertEqual(got["external_goal_ref"], "parent:week-beta")
            report = layer.get_report(out["goal_id"])["report"]
            self.assertEqual(report["submitter_id"], "upper-1")
            self.assertEqual(report["external_goal_ref"], "parent:week-beta")

    def test_conflict_rules_stay_p0(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            first = layer.submit_goal(
                submit_key="sk-c",
                title="same",
                desired_outcome="same",
                submitter_id="upper-1",
                autonomy="bounded_autonomy",
            )
            gid = first["goal_id"]
            dup = layer.submit_goal(
                submit_key="sk-c",
                title="same",
                desired_outcome="same",
                submitter_id="upper-1",
                autonomy="bounded_autonomy",
            )
            self.assertTrue(dup["ok"], dup)
            self.assertTrue(dup["duplicate"])
            self.assertEqual(dup["goal_id"], gid)
            conflict = layer.submit_goal(
                submit_key="sk-c",
                title="different",
                desired_outcome="same",
                submitter_id="upper-1",
                autonomy="bounded_autonomy",
            )
            self.assertFalse(conflict["ok"], conflict)
            self.assertEqual(conflict["reason"], REASON_SUBMIT_CONTENT_CONFLICT)
            self.assertEqual(layer.goal_count(), 1)
            self.assertEqual(layer.get_goal(gid)["goal"]["goal"]["title"], "same")


class TestPlanRevisionRespectsAutonomy(_P1Case):
    def test_explicit_plan_forbids_local_replan(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            opened = layer.submit_goal(
                submit_key="sk-xp",
                title="planned",
                desired_outcome="no local plan",
                autonomy="explicit_plan",
                coordinator_id="coord-a",
                submitter_id="upper-1",
                tasks=[{"task_id": "t1", "title": "given-step", "status": "queued"}],
            )
            self.assertTrue(opened["ok"], opened)
            gid = opened["goal_id"]
            store = GoalOwnershipStore.open(gid, td)
            self.assertEqual(store.autonomy["mode"], AUTONOMY_EXPLICIT_PLAN)
            revised = store.submit_plan_revision(
                coordinator_id="coord-a",
                ownership_version=1,
                plan={
                    "goal_id": gid,
                    "tasks": [
                        {"task_id": "t1", "goal_id": gid, "status": "queued"},
                        {"task_id": "t2", "goal_id": gid, "status": "queued", "title": "local extra"},
                    ],
                },
                reason="local split",
            )
            self.assertFalse(revised["ok"], revised)
            self.assertEqual(revised["reason"], REASON_AUTONOMY_DENIED)

    def test_bounded_autonomy_allows_local_replan(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            opened = layer.submit_goal(
                submit_key="sk-bd",
                title="bounded",
                desired_outcome="local plan ok",
                autonomy="bounded_autonomy",
                coordinator_id="coord-a",
                submitter_id="upper-1",
                tasks=[{"task_id": "t1", "title": "first", "status": "queued"}],
                goal={"budget": {"wall_sec": 60}, "boundaries": {"must": [], "must_not": []}},
            )
            self.assertTrue(opened["ok"], opened)
            gid = opened["goal_id"]
            store = GoalOwnershipStore.open(gid, td)
            revised = store.submit_plan_revision(
                coordinator_id="coord-a",
                ownership_version=1,
                plan={
                    "goal_id": gid,
                    "tasks": [
                        {"task_id": "t1", "goal_id": gid, "status": "queued"},
                        {"task_id": "t2", "goal_id": gid, "status": "queued", "title": "split"},
                    ],
                },
                reason="contract-internal split",
            )
            self.assertTrue(revised["ok"], revised)
            self.assertEqual(revised["plan_revision"], 2)


class TestEscalatePending(_P1Case):
    def test_escalate_over_budget_is_pending_not_retry(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(
                submit_key="sk-esc",
                title="esc",
                desired_outcome="esc",
                submitter_id="upper-1",
                coordinator_id="coord-a",
                autonomy="bounded_autonomy",
            )["goal_id"]
            before_tasks = len(layer.get_goal(gid)["goal"]["tasks"])
            esc = layer.escalate_to_upper(
                gid,
                kind="over_budget",
                reason="reserve would exceed wall_sec",
            )
            self.assertTrue(esc["ok"], esc)
            self.assertEqual(esc["decision"]["kind"], KIND_ESCALATE_OVER_BUDGET)
            self.assertTrue(esc["return_to_upper"])
            self.assertFalse(esc["silent_retry"])
            self.assertEqual(esc["event_class"], EVENT_CLASS_DECISION)
            snap = layer.get_goal(gid)["goal"]
            self.assertEqual(len(snap["pending_decisions"]), 1)
            self.assertEqual(len(snap["tasks"]), before_tasks)
            pending = snap["pending_decisions"][0]
            self.assertTrue(pending["return_to_upper"])
            self.assertFalse(pending["silent_retry"])

            listed = layer.list_events(gid)
            self.assertTrue(listed["ok"], listed)
            kinds = {e.get("event_class") for e in listed["events"]}
            self.assertIn(EVENT_CLASS_DECISION, kinds)
            self.assertEqual(listed["pending"][0]["kind"], KIND_ESCALATE_OVER_BUDGET)

            stranger = layer.resolve_decision(
                gid,
                decision_id=pending["decision_id"],
                verdict="ack",
                reason="nope",
                actor_id="stranger",
            )
            self.assertFalse(stranger["ok"], stranger)
            self.assertEqual(stranger["reason"], REASON_ACTOR_NOT_AUTHORIZED)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 1)

            ok = layer.resolve_decision(
                gid,
                decision_id=pending["decision_id"],
                verdict="ack_raise_budget",
                reason="upper will revise contract",
                actor_id="upper-1",
            )
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(ok["actor_id"], "upper-1")
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 0)

    def test_escalate_out_of_scope_and_auth_kinds(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(
                submit_key="sk-kinds",
                title="k",
                desired_outcome="k",
                submitter_id="upper-1",
            )["goal_id"]
            a = layer.escalate_to_upper(gid, kind="out_of_scope", reason="other repo")
            b = layer.escalate_to_upper(gid, kind="insufficient_auth", reason="no deploy")
            self.assertEqual(a["decision"]["kind"], KIND_ESCALATE_OUT_OF_SCOPE)
            self.assertEqual(b["decision"]["kind"], KIND_ESCALATE_INSUFFICIENT_AUTH)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 2)
            bad = layer.escalate_to_upper(gid, kind="shrug", reason="x")
            self.assertFalse(bad["ok"], bad)
            self.assertEqual(bad["reason"], "unknown_escalate_kind")


class TestOwnershipOnGet(_P1Case):
    def test_ownership_visible_on_get_after_submit_with_coordinator(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            opened = layer.submit_goal(
                submit_key="sk-own",
                title="owned",
                desired_outcome="see coordinator",
                coordinator_id="coord-a",
                submitter_id="upper-1",
                autonomy="bounded_autonomy",
            )
            self.assertTrue(opened["ok"], opened)
            self.assertEqual(opened["ownership"]["coordinator_id"], "coord-a")
            self.assertEqual(opened["ownership"]["version"], 1)
            got = layer.get_goal(opened["goal_id"])
            self.assertEqual(got["ownership"]["coordinator_id"], "coord-a")
            self.assertEqual(got["ownership"]["version"], 1)
            self.assertTrue(got["ownership"]["readonly"])
            self.assertEqual(got["plan_revision"]["coordinator_id"], "coord-a")
            report = layer.get_report(opened["goal_id"])["report"]
            self.assertEqual(report["ownership"]["coordinator_id"], "coord-a")

    def test_handoff_bumps_version_visible_on_get(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(
                submit_key="sk-ho",
                title="h",
                desired_outcome="h",
                coordinator_id="coord-a",
            )["goal_id"]
            out = layer.handoff_coordinator(
                gid,
                from_coordinator_id="coord-a",
                to_coordinator_id="coord-b",
                expected_version=1,
            )
            self.assertTrue(out["ok"], out)
            got = layer.get_goal(gid)
            self.assertEqual(got["ownership"]["coordinator_id"], "coord-b")
            self.assertEqual(got["ownership"]["version"], 2)

    def test_submit_without_coordinator_still_has_no_ownership(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(submit_key="sk-bare", title="b", desired_outcome="b")["goal_id"]
            got = layer.get_goal(gid)
            self.assertNotIn("ownership", got)


class TestCharterPassthrough(_P1Case):
    def test_hello_unchanged_without_delegation_fields(self):
        ch = load_charter(HELLO)
        out = map_charter_to_goal_task(ch)
        self.assertNotIn("role_hints", out["goal"])
        self.assertNotIn("submitter_id", out)
        self.assertNotIn("autonomy", out)

    def test_charter_autonomy_and_submitter_map(self):
        ch = load_charter(HELLO)
        ch["submitter_id"] = "upper-1"
        ch["external_goal_ref"] = "parent:x"
        ch["autonomy"] = "bounded_autonomy"
        ch["coordinator_id"] = "coord-a"
        out = map_charter_to_goal_task(ch)
        self.assertEqual(out["submitter_id"], "upper-1")
        self.assertEqual(out["external_goal_ref"], "parent:x")
        self.assertEqual(out["autonomy"]["mode"], AUTONOMY_BOUNDED)
        self.assertEqual(out["goal"]["role_hints"]["submitter"], "upper-1")
        self.assertEqual(out["goal"]["role_hints"]["autonomy"], AUTONOMY_BOUNDED)
        self.assertEqual(out["goal"]["role_hints"]["coordinator"], "coord-a")


class TestCliAndDemo(_P1Case):
    def test_cli_submit_events_escalate_resolve_get_report_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")

            def run(*argv: str, expect_ok: bool = True) -> dict:
                proc = subprocess.run(
                    [sys.executable, str(CLI), "--persist", td, *argv],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                if expect_ok:
                    self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                payload = json.loads(proc.stdout)
                return payload

            submitted = run(
                "submit",
                "--submit-key",
                "cli-p1",
                "--title",
                "cli-p1",
                "--outcome",
                "delegate",
                "--submitter",
                "upper-cli",
                "--external-goal-ref",
                "parent:cli",
                "--autonomy",
                "bounded_autonomy",
                "--coordinator",
                "coord-cli",
                "--budget-json",
                json.dumps({"wall_sec": 90, "max_reworks": 1}),
                "--boundaries-json",
                json.dumps({"must": ["workspace"], "must_not": ["deploy"]}),
            )
            self.assertTrue(submitted["created"], submitted)
            gid = submitted["goal_id"]
            self.assertEqual(submitted["submitter_id"], "upper-cli")
            self.assertEqual(submitted["autonomy"]["mode"], AUTONOMY_BOUNDED)
            self.assertEqual(submitted["ownership"]["coordinator_id"], "coord-cli")

            got = run("get", gid)
            self.assertEqual(got["ownership"]["version"], 1)
            self.assertEqual(got["submitter_id"], "upper-cli")

            events = run("events", gid)
            self.assertTrue(events["ok"], events)

            esc = run(
                "escalate",
                gid,
                "--kind",
                "over_budget",
                "--reason",
                "need more wall",
            )
            did = esc["decision"]["decision_id"]
            self.assertEqual(esc["decision"]["kind"], KIND_ESCALATE_OVER_BUDGET)
            self.assertFalse(esc["silent_retry"])

            pending = run("pending", gid)
            self.assertEqual(pending["pending_count"], 1)

            resolved = run(
                "resolve",
                gid,
                "--decision-id",
                did,
                "--verdict",
                "ack",
                "--reason",
                "upper handles it",
                "--actor-id",
                "upper-cli",
            )
            self.assertTrue(resolved["ok"], resolved)

            report = run("report", gid)
            self.assertEqual(report["report"]["submitter_id"], "upper-cli")
            self.assertEqual(report["report"]["ownership"]["coordinator_id"], "coord-cli")

            cancelled = run("cancel", gid)
            self.assertEqual(cancelled["state"], "cancel_requested")
            self.assertFalse(cancelled["cancelled"])

    def test_delegate_demo_script(self):
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                [sys.executable, str(DEMO), "--persist", td],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("escalate_over_budget", proc.stdout)
            self.assertIn("upper-bot-1", proc.stdout)
            self.assertIn("coord-local-1", proc.stdout)
            self.assertIn("cancel_requested", proc.stdout)


class TestRunJobUnchanged(_P1Case):
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
            self.assertNotIn("durable", summary)
            self.assertNotIn("delegation", summary)


if __name__ == "__main__":
    unittest.main()
