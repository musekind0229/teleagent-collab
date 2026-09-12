#!/usr/bin/env python3
"""Path-B knife11: Goal-level budget (reserve/reconcile) + task dependencies."""
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
from execution_backend import (  # noqa: E402
    BudgetCost,
    BudgetLimits,
    GoalBudget,
    can_enter_running,
    make_fail_once_then_pass,
    open_goal_budget,
    reset_goal_budget_cache,
    run_dependent_inprocess_jobs,
    run_inprocess_charter,
    run_inprocess_closed_loop,
    simulate_scheduler_goal_deps,
)
from framework.charter_map import map_charter_to_goal_task  # noqa: E402
from framework.lifecycle import assert_transition  # noqa: E402
from framework.models import new_run_id, new_task_id  # noqa: E402
from framework.task_deps import depends_on_strings, unsatisfied_deps  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"
CLOSED = REPO / "jobs/examples/hello-inprocess-closed-loop.charter.yaml"
PRODUCER = REPO / "jobs/examples/producer.charter.yaml"
CONSUMER = REPO / "jobs/examples/consumer.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife11", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCharterDependsOn(unittest.TestCase):
    def test_producer_consumer_charters(self):
        p = load_charter(PRODUCER)
        c = load_charter(CONSUMER)
        self.assertEqual(p["name"], "producer")
        self.assertEqual(c["name"], "consumer")
        self.assertEqual(p.get("goal_id"), "goal_dep_demo")
        self.assertEqual(c.get("goal_id"), "goal_dep_demo")
        self.assertEqual(p["done_when"]["artifacts"], ["producer.txt"])
        self.assertEqual(c["done_when"]["artifacts"], ["consumer.txt"])
        refs = depends_on_strings(c.get("depends_on"))
        self.assertIn("producer", refs)
        self.assertIn("artifact:producer.txt", refs)

    def test_map_copies_depends_on_and_budget(self):
        c = load_charter(CONSUMER)
        out = map_charter_to_goal_task(c)
        self.assertEqual(out["goal"]["goal_id"], "goal_dep_demo")
        self.assertIn("producer", out["task"]["depends_on"])
        self.assertIn("artifact:producer.txt", out["task"]["depends_on"])
        self.assertEqual(out["task"]["status"], "queued")
        self.assertEqual(out["goal"]["budget"]["max_attempts"], 2)


class TestGoalBudgetAccount(unittest.TestCase):
    def setUp(self):
        reset_goal_budget_cache()

    def tearDown(self):
        reset_goal_budget_cache()

    def test_reserve_reconcile_releases_unused(self):
        with tempfile.TemporaryDirectory() as td:
            acc = GoalBudget.open(
                "goal_rsv",
                td,
                limits=BudgetLimits(wall_sec=60, max_attempts=4, max_reworks=2, max_approvals=4),
            )
            rsv = acc.reserve(
                "run_a",
                task_id="task_a",
                cost=BudgetCost(attempts=1, wall_sec=30, approvals=1),
            )
            self.assertTrue(rsv.granted, rsv.to_dict())
            self.assertEqual(acc.reserved.attempts, 1)
            rec = acc.reconcile("run_a", BudgetCost(attempts=1, wall_sec=0.05, approvals=1))
            self.assertTrue(rec["ok"], rec)
            self.assertEqual(acc.reserved.attempts, 0)
            self.assertEqual(acc.consumed.attempts, 1)
            self.assertEqual(acc.consumed.approvals, 1)
            self.assertLess(acc.consumed.wall_sec, 1.0)
            self.assertFalse(acc.over_budget())

    def test_minting_new_id_does_not_reset(self):
        with tempfile.TemporaryDirectory() as td:
            acc = GoalBudget.open(
                "goal_stable",
                td,
                limits=BudgetLimits(wall_sec=90, max_attempts=5, max_reworks=3, max_approvals=5),
            )
            deadline = acc.wall_deadline
            rsv = acc.reserve("run_old", task_id="task_old", cost=BudgetCost(attempts=1, wall_sec=10))
            self.assertTrue(rsv.granted)
            acc.reconcile("run_old", BudgetCost(attempts=1, wall_sec=0.2, reworks=0, approvals=0))
            self.assertEqual(acc.consumed.attempts, 1)
            new_tid = new_task_id()
            new_rid = new_run_id()
            self.assertTrue(new_tid.startswith("task_"))
            self.assertTrue(new_rid.startswith("run_"))
            reset_goal_budget_cache()
            acc2 = GoalBudget.open("goal_stable", td)
            self.assertEqual(acc2.consumed.attempts, 1)
            self.assertEqual(acc2.wall_deadline, deadline)
            rsv2 = acc2.reserve(new_rid, task_id=new_tid, cost=BudgetCost(attempts=1, wall_sec=10))
            self.assertTrue(rsv2.granted, rsv2.to_dict())
            self.assertEqual(acc2.consumed.attempts, 1)
            self.assertEqual(acc2.reserved.attempts, 1)
            self.assertNotEqual(new_tid, "task_old")
            self.assertNotEqual(new_rid, "run_old")

    def test_over_budget_reserve_denied(self):
        with tempfile.TemporaryDirectory() as td:
            acc = GoalBudget.open(
                "goal_cap",
                td,
                limits=BudgetLimits(wall_sec=30, max_attempts=1, max_reworks=0, max_approvals=1, max_usage=1.0),
            )
            r1 = acc.reserve("r1", task_id="t1", cost=BudgetCost(attempts=1, wall_sec=5))
            self.assertTrue(r1.granted)
            acc.reconcile("r1", BudgetCost(attempts=1, wall_sec=0.1))
            r2 = acc.reserve("r2", task_id="t2", cost=BudgetCost(attempts=1, wall_sec=5))
            self.assertFalse(r2.granted)
            self.assertEqual(r2.reason, "budget_exhausted")
            ok, reason = acc.can_reserve(BudgetCost(usage=2.0, attempts=0))
            self.assertFalse(ok)
            self.assertEqual(reason, "budget_exhausted")

    def test_child_rework_and_approval_roll_up(self):
        charter = load_charter(CLOSED)
        stub = make_fail_once_then_pass()
        with tempfile.TemporaryDirectory() as td:
            acc = open_goal_budget(
                "goal_rollup",
                td,
                limits=BudgetLimits(wall_sec=60, max_attempts=8, max_reworks=4, max_approvals=8),
            )
            result = run_inprocess_closed_loop(
                charter=charter,
                workdir=td,
                name="hello-cl",
                decision_fn=stub,
                timeout_sec=30,
                max_reworks=1,
                goal_budget=acc,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["attempt"], 2)
            self.assertTrue(result["rework_used_new_run"])
            self.assertEqual(acc.consumed.attempts, 2, acc.to_dict())
            self.assertEqual(acc.consumed.reworks, 1, acc.to_dict())
            self.assertEqual(acc.consumed.approvals, 2, acc.to_dict())
            self.assertEqual(acc.goal_id, result["goal_id"])
            self.assertEqual(result["run_ids"][0] != result["run_ids"][1], True)
            self.assertEqual(acc.wall_deadline, acc.to_dict()["wall_deadline"])


class TestTaskDepsGate(unittest.TestCase):
    def setUp(self):
        reset_goal_budget_cache()

    def tearDown(self):
        reset_goal_budget_cache()

    def test_unsatisfied_named_dep_stays_queued(self):
        missing = unsatisfied_deps(
            ["producer", "artifact:producer.txt"],
            completed={},
            workdir=".",
            enforce_named_deps=True,
        )
        self.assertTrue(missing)
        self.assertTrue(any("producer" in m for m in missing))

    def test_lifecycle_queued_to_failed_for_budget(self):
        assert_transition("task", "queued", "failed")
        assert_transition("task", "queued", "running")

    def test_consumer_alone_stays_queued(self):
        charter = load_charter(CONSUMER)
        with tempfile.TemporaryDirectory() as td:
            before = set(sys.modules)
            result = run_inprocess_charter(charter=charter, workdir=td, name="consumer")
            newly = set(sys.modules) - before
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["state"], "queued")
            self.assertTrue(result.get("unsatisfied_deps"), result)
            self.assertFalse((Path(td) / "consumer.txt").exists())
            self.assertFalse(result.get("entered_running", True))
            banned = [
                n
                for n in newly
                if n == "glue" or n.startswith("glue.") or n.startswith("teleagent_adapter")
            ]
            self.assertEqual(banned, [], f"inprocess dep gate imported TA/glue: {banned}")

    def test_can_enter_running_workdir_then_budget(self):
        from execution_backend.workdir_claim import WorkdirClaimRegistry

        charter = load_charter(PRODUCER)
        with tempfile.TemporaryDirectory() as td:
            acc = GoalBudget.open(
                "goal_gate",
                td,
                limits=BudgetLimits(wall_sec=30, max_attempts=0, max_reworks=0, max_approvals=0),
            )
            gate_b = can_enter_running(
                charter=charter,
                name="producer",
                completed={},
                workdir=td,
                budget=acc,
            )
            self.assertFalse(gate_b["ready"])
            self.assertEqual(gate_b["reason"], "budget_exhausted")
            self.assertEqual(gate_b["task_state"], "failed")

            acc2 = GoalBudget.open(
                "goal_gate2",
                td,
                limits=BudgetLimits(wall_sec=30, max_attempts=4, max_reworks=1, max_approvals=4),
            )
            reg = WorkdirClaimRegistry()
            held = reg.claim(td, holder_id="holder", job_name="hold")
            self.assertTrue(held.granted)
            try:
                gate_w = can_enter_running(
                    charter=charter,
                    name="producer",
                    completed={},
                    workdir=td,
                    budget=acc2,
                    claim_registry=reg,
                    holder_id="other",
                )
                self.assertFalse(gate_w["ready"])
                self.assertEqual(gate_w["reason"], "workdir_occupied")
                self.assertEqual(gate_w["task_state"], "queued")
            finally:
                reg.release(td, "holder")


class TestTwoInprocessJobsDeps(unittest.TestCase):
    def setUp(self):
        reset_goal_budget_cache()

    def tearDown(self):
        reset_goal_budget_cache()

    def test_consumer_queued_until_producer_then_runs(self):
        producer = load_charter(PRODUCER)
        consumer = load_charter(CONSUMER)
        with tempfile.TemporaryDirectory() as td:
            report = run_dependent_inprocess_jobs(
                producer_charter=producer,
                consumer_charter=consumer,
                workdir=td,
            )
            self.assertTrue(report["consumer_queued_until_producer"], report["ticks"])
            self.assertTrue(report["deps_blocked_unready"], report)
            self.assertTrue(report["ok"], report)
            self.assertTrue((report.get("producer") or {}).get("ok"), report.get("producer"))
            self.assertTrue((report.get("consumer") or {}).get("ok"), report.get("consumer"))
            self.assertTrue((Path(td) / "producer.txt").is_file())
            self.assertTrue((Path(td) / "consumer.txt").is_file())
            first = report["ticks"][0]
            self.assertIn("producer", first["started"])
            self.assertIn("consumer", first["queued"])
            self.assertEqual(first["queued_reasons"].get("consumer"), "unsatisfied_deps")
            gb = report["goal_budget"]
            self.assertGreaterEqual(gb["consumed"]["attempts"], 2)
            self.assertEqual(gb["goal_id"], "goal_dep_demo")
            self.assertTrue(report["wall_deadline_unchanged"])
            self.assertFalse(report["over_budget_reported_success"])

    def test_over_budget_does_not_report_success(self):
        producer = load_charter(PRODUCER)
        consumer = load_charter(CONSUMER)
        with tempfile.TemporaryDirectory() as td:
            acc = GoalBudget.open(
                "goal_dep_demo",
                td,
                limits=BudgetLimits(wall_sec=60, max_attempts=1, max_reworks=0, max_approvals=2),
            )
            report = run_dependent_inprocess_jobs(
                producer_charter=producer,
                consumer_charter=consumer,
                workdir=td,
                goal_budget=acc,
            )
            self.assertFalse(report["ok"], report)
            prod = report.get("producer") or {}
            cons = report.get("consumer") or {}
            cons_slot = report.get("consumer_slot") or {}
            self.assertTrue(prod.get("ok"), prod)
            self.assertFalse(cons.get("ok", True), cons)
            err = cons.get("error_class") or cons_slot.get("error_class")
            self.assertEqual(err, "budget_exhausted", cons)
            self.assertNotEqual(cons.get("state"), "ok")
            self.assertFalse(cons.get("ok"))
            self.assertTrue((Path(td) / "producer.txt").is_file())
            self.assertFalse((Path(td) / "consumer.txt").exists())
            self.assertFalse(report["over_budget_reported_success"])
            self.assertTrue(report["consumer_queued_until_producer"] or cons_slot.get("state") == "failed", report["ticks"])

    def test_scheduler_first_tick_does_not_start_consumer(self):
        producer = load_charter(PRODUCER)
        consumer = load_charter(CONSUMER)
        with tempfile.TemporaryDirectory() as td:
            jobs = [
                {"job_id": "job-producer", "name": "producer", "charter": producer, "workdir": td},
                {"job_id": "job-consumer", "name": "consumer", "charter": consumer, "workdir": td},
            ]
            report = simulate_scheduler_goal_deps(jobs, max_ticks=1, execute=True, persist_dir=td)
            self.assertTrue(report["consumer_queued_until_producer"], report)
            self.assertEqual(len(report["ticks"]), 1)
            self.assertIn("consumer", report["ticks"][0]["queued"])
            self.assertFalse((Path(td) / "consumer.txt").exists())
            self.assertTrue((Path(td) / "producer.txt").is_file())


class TestRunJobCliKnife11(unittest.TestCase):
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
            self.assertNotIn("goal_budget", summary)

    def test_default_entry_not_inprocess(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")

    def test_cli_consumer_without_producer_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            wa = str(Path(td) / "ws")
            Path(wa).mkdir()
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                ["--backend", "inprocess", "--workspace", wa, "--runs-dir", runs, str(CONSUMER)]
            )
            self.assertEqual(rc, 1, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertFalse(summary["ok"], summary)
            self.assertEqual(summary["state"], "queued")
            self.assertFalse((Path(wa) / "consumer.txt").exists())


if __name__ == "__main__":
    unittest.main()
