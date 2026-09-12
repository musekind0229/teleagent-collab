#!/usr/bin/env python3
"""Path-B knife14: durable/perpetual layer minimal API."""
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

from framework.durable_api import (  # noqa: E402
    DURABLE_DIRNAME,
    REASON_ADMISSION_CLOSED,
    REASON_DUPLICATE_SUBMIT,
    REASON_IDENTITY_MEMORY_FORBIDDEN,
    REASON_UNRELATED_BATCH,
    DurableLayer,
    cancel_goal,
    get_goal,
    get_report,
    kernel_promotes_identity_memory,
    open_durable,
    persist_dir,
    reset_durable_cache,
    resolve_decision,
    submit_goal,
)
from framework.lifecycle import GOAL_STATES, LifecycleError, assert_transition  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"
CLI = REPO / "bin" / "durable-cli.py"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife14", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _DurableCase(unittest.TestCase):
    def setUp(self):
        reset_durable_cache()

    def tearDown(self):
        reset_durable_cache()


class TestGoalCancelLifecycle(_DurableCase):
    def test_cancel_requested_is_a_goal_state_distinct_from_cancelled(self):
        self.assertIn("cancel_requested", GOAL_STATES)
        self.assertIn("cancelled", GOAL_STATES)
        assert_transition("goal", "running", "cancel_requested")
        assert_transition("goal", "cancel_requested", "cancelled")
        with self.assertRaises(LifecycleError):
            assert_transition("goal", "cancelled", "cancel_requested")


class TestSubmitGoalIdempotent(_DurableCase):
    def test_duplicate_submit_same_key_same_goal_no_double_open(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            first = layer.submit_goal(
                submit_key="sk-hello",
                title="hello",
                desired_outcome="write hello",
            )
            self.assertTrue(first["ok"], first)
            self.assertTrue(first["created"])
            self.assertFalse(first["duplicate"])
            gid = first["goal_id"]
            self.assertTrue(gid)
            self.assertEqual(layer.goal_count(), 1)

            second = layer.submit_goal(
                submit_key="sk-hello",
                title="hello again",
                desired_outcome="should not open another",
            )
            self.assertTrue(second["ok"], second)
            self.assertFalse(second["created"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(second["reason"], REASON_DUPLICATE_SUBMIT)
            self.assertEqual(second["goal_id"], gid)
            self.assertEqual(layer.goal_count(), 1)
            self.assertFalse(second.get("opened"))

            snap = layer.get_goal(gid)
            self.assertTrue(snap["ok"], snap)
            self.assertEqual(snap["goal"]["submit_key"], "sk-hello")
            self.assertEqual(snap["goal"]["goal"]["title"], "hello")

            other = layer.submit_goal(submit_key="sk-other", title="other", desired_outcome="other")
            self.assertTrue(other["created"], other)
            self.assertNotEqual(other["goal_id"], gid)
            self.assertEqual(layer.goal_count(), 2)

            store = json.loads((persist_dir(td) / "store.json").read_text(encoding="utf-8"))
            self.assertEqual(store["submit_keys"]["sk-hello"], gid)
            self.assertEqual(len(store["goals"]), 2)
            self.assertIn(DURABLE_DIRNAME, str(layer.path))

            reset_durable_cache()
            reopened = DurableLayer.open(td)
            self.assertEqual(reopened.goal_count(), 2)
            again = reopened.submit_goal(submit_key="sk-hello", title="x", desired_outcome="y")
            self.assertEqual(again["goal_id"], gid)
            self.assertFalse(again["created"])
            self.assertEqual(reopened.goal_count(), 2)

    def test_module_functions_same_key(self):
        with tempfile.TemporaryDirectory() as td:
            a = submit_goal(td, submit_key="k", title="t", desired_outcome="o")
            b = submit_goal(td, submit_key="k", title="t2", desired_outcome="o2")
            self.assertEqual(a["goal_id"], b["goal_id"])
            self.assertEqual(get_goal(td, a["goal_id"])["goal_id"], a["goal_id"])


class TestResolveDecisionOneAtATime(_DurableCase):
    def test_resolve_decision_rejects_multi_unrelated_batch(self):
        with tempfile.TemporaryDirectory() as td:
            layer = open_durable(td)
            opened = layer.submit_goal(submit_key="sk-dec", title="d", desired_outcome="decide")
            gid = opened["goal_id"]
            d1 = layer.open_decision(gid, kind="action_approval", title="one")
            d2 = layer.open_decision(gid, kind="question", title="two")
            self.assertTrue(d1["ok"] and d2["ok"], (d1, d2))
            id1 = d1["decision"]["decision_id"]
            id2 = d2["decision"]["decision_id"]
            self.assertNotEqual(id1, id2)

            batch = layer.resolve_decision(
                gid,
                verdict="allow",
                reason="nope",
                actions=[
                    {"decision_id": id1, "verdict": "allow"},
                    {"decision_id": id2, "verdict": "deny"},
                ],
            )
            self.assertFalse(batch["ok"], batch)
            self.assertEqual(batch["reason"], REASON_UNRELATED_BATCH)
            self.assertEqual(len(layer.get_goal(gid)["goal"]["pending_decisions"]), 2)

            listed = layer.resolve_decision(
                gid,
                verdict="allow",
                reason="nope",
                decisions=[
                    {"decision_id": id1, "verdict": "allow"},
                    {"decision_id": id2, "verdict": "deny"},
                ],
            )
            self.assertFalse(listed["ok"], listed)
            self.assertEqual(listed["reason"], REASON_UNRELATED_BATCH)

            mixed = layer.resolve_decision(
                gid,
                decision_id=id1,
                verdict="allow",
                reason="nope",
                extra={"cancel_goal": True},
            )
            self.assertFalse(mixed["ok"], mixed)
            self.assertEqual(mixed["reason"], REASON_UNRELATED_BATCH)

            unnamed = layer.resolve_decision(gid, verdict="allow", reason="pick one")
            self.assertFalse(unnamed["ok"], unnamed)
            self.assertEqual(unnamed["reason"], "ambiguous_pending")

            via_mod = resolve_decision(
                td,
                gid,
                actions=[
                    {"decision_id": id1, "kind": "action_approval"},
                    {"decision_id": id2, "kind": "question"},
                ],
                verdict="allow",
                reason="still no",
            )
            self.assertFalse(via_mod["ok"], via_mod)
            self.assertEqual(via_mod["reason"], REASON_UNRELATED_BATCH)

            ok_one = layer.resolve_decision(
                gid,
                decision_id=id1,
                verdict="allow",
                reason="this one only",
                actions=[{"decision_id": id1, "op": "approve_path"}],
            )
            self.assertTrue(ok_one["ok"], ok_one)
            self.assertEqual(ok_one["pending_count"], 1)
            pending = layer.get_goal(gid)["goal"]["pending_decisions"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["decision_id"], id2)

    def test_related_actions_same_decision_ok(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(submit_key="rel", title="r", desired_outcome="r")["goal_id"]
            opened = layer.open_decision(
                gid,
                kind="action_approval",
                actions=[{"op": "path_a"}, {"op": "path_b"}],
            )
            did = opened["decision"]["decision_id"]
            out = layer.resolve_decision(
                gid,
                decision_id=did,
                verdict="allow",
                reason="same decision",
                actions=[
                    {"decision_id": did, "op": "path_a"},
                    {"decision_id": did, "op": "path_b"},
                ],
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["pending_count"], 0)


class TestCancelRequestedBeforeCancelled(_DurableCase):
    def test_after_cancel_state_is_cancel_requested_not_cancelled(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(submit_key="sk-c", title="c", desired_outcome="c")["goal_id"]
            added = layer.add_child_task(gid, title="work", status="queued")
            self.assertTrue(added["ok"], added)
            tid = added["task"]["task_id"]
            started = layer.start_task(gid, tid)
            self.assertTrue(started["ok"], started)
            self.assertEqual(started["task"]["status"], "running")

            out = layer.cancel_goal(gid)
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["state"], "cancel_requested")
            self.assertTrue(out["cancel_requested"])
            self.assertFalse(out["cancelled"])
            self.assertNotEqual(out["state"], "cancelled")
            self.assertFalse(out["accepting_child_tasks"])
            self.assertIn(tid, out["terminate_requested_task_ids"])

            snap = layer.get_goal(gid)
            self.assertEqual(snap["state"], "cancel_requested")
            self.assertTrue(snap["cancel_requested"])
            self.assertFalse(snap["cancelled"])
            self.assertNotEqual(snap["state"], "cancelled")
            task = snap["goal"]["tasks"][0]
            self.assertEqual(task["status"], "cancel_requested")
            self.assertTrue(task["terminate_requested"])
            self.assertNotEqual(task["status"], "cancelled")

            report = layer.get_report(gid)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["state"], "cancel_requested")
            self.assertTrue(report["cancel_requested"])
            self.assertFalse(report["cancelled"])
            self.assertTrue(report["report"]["readonly"])
            self.assertNotEqual(report["report"]["state"], "cancelled")

            refused = layer.add_child_task(gid, title="late")
            self.assertFalse(refused["ok"], refused)
            self.assertEqual(refused["reason"], REASON_ADMISSION_CLOSED)

            effected = layer.effect_cancel(gid)
            self.assertTrue(effected["ok"], effected)
            self.assertEqual(effected["state"], "cancelled")
            self.assertTrue(effected["cancelled"])
            self.assertTrue(effected["cancel_requested"])
            after = layer.get_goal(gid)
            self.assertEqual(after["state"], "cancelled")
            self.assertTrue(after["cancelled"])
            self.assertNotEqual(snap["state"], after["state"])
            self.assertEqual(after["goal"]["tasks"][0]["status"], "cancelled")

    def test_module_cancel_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            opened = submit_goal(td, submit_key="m", title="m", desired_outcome="m")
            gid = opened["goal_id"]
            c = cancel_goal(td, gid)
            self.assertEqual(c["state"], "cancel_requested")
            self.assertFalse(c["cancelled"])
            r = get_report(td, gid)
            self.assertEqual(r["state"], "cancel_requested")
            self.assertFalse(r["cancelled"])
            self.assertFalse(r["report"]["kernel_promotes_identity_memory"])


class TestNoIdentityMemoryPromotion(_DurableCase):
    def test_kernel_does_not_promote_identity_memory(self):
        self.assertFalse(kernel_promotes_identity_memory())
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            bad = layer.submit_goal(
                submit_key="idmem",
                title="nope",
                desired_outcome="nope",
                extra={"identity": {"promote": True}, "memory": {"keep": True}},
            )
            self.assertFalse(bad["ok"], bad)
            self.assertEqual(bad["reason"], REASON_IDENTITY_MEMORY_FORBIDDEN)
            self.assertEqual(layer.goal_count(), 0)

            cn = layer.submit_goal(
                submit_key="idmem-cn",
                title="nope",
                desired_outcome="nope",
                extra={"晋升身份记忆": True},
            )
            self.assertFalse(cn["ok"], cn)
            self.assertEqual(cn["reason"], REASON_IDENTITY_MEMORY_FORBIDDEN)

            ok = layer.submit_goal(submit_key="plain", title="p", desired_outcome="p")
            gid = ok["goal_id"]
            layer.open_decision(gid)
            refused = layer.resolve_decision(
                gid,
                verdict="allow",
                reason="x",
                extra={"promote_identity": True},
            )
            self.assertFalse(refused["ok"], refused)


class TestGetGoalAndReport(_DurableCase):
    def test_get_goal_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            miss = layer.get_goal("goal_missing")
            self.assertFalse(miss["ok"])
            self.assertEqual(miss["reason"], "unknown_goal")

    def test_report_snapshot_fields(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            gid = layer.submit_goal(submit_key="rep", title="report-me", desired_outcome="shown")[
                "goal_id"
            ]
            layer.add_child_task(gid, title="t1")
            layer.open_decision(gid, kind="plan_review")
            report = layer.get_report(gid)["report"]
            self.assertEqual(report["title"], "report-me")
            self.assertEqual(report["task_count"], 1)
            self.assertEqual(report["pending_decision_count"], 1)
            self.assertTrue(report["readonly"])
            self.assertFalse(report["kernel_promotes_identity_memory"])


class TestDurableCli(_DurableCase):
    def test_cli_submit_get_cancel_report(self):
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")

            def run(*args: str) -> dict:
                proc = subprocess.run(
                    [sys.executable, str(CLI), "--persist", td, *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                return json.loads(proc.stdout)

            submitted = run("submit", "--submit-key", "cli-k", "--title", "cli", "--outcome", "o")
            self.assertTrue(submitted["created"])
            gid = submitted["goal_id"]
            dup = run("submit", "--submit-key", "cli-k", "--title", "cli2", "--outcome", "o2")
            self.assertEqual(dup["goal_id"], gid)
            self.assertFalse(dup["created"])
            got = run("get", gid)
            self.assertEqual(got["goal_id"], gid)
            cancelled = run("cancel", gid)
            self.assertEqual(cancelled["state"], "cancel_requested")
            self.assertFalse(cancelled["cancelled"])
            report = run("report", gid)
            self.assertEqual(report["state"], "cancel_requested")
            self.assertFalse(report["cancelled"])


class TestRunJobCliKnife14(_DurableCase):
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
            self.assertNotIn("durable_layer", summary)

    def test_default_entry_not_inprocess(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")
            self.assertNotIn("backend", summary)


if __name__ == "__main__":
    unittest.main()
