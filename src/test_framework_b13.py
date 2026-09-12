#!/usr/bin/env python3
"""Path-B knife13: transactional state + durable outbox + receiver dedup."""
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
    WorkdirClaimRegistry,
    run_inprocess_charter,
)
from framework.lifecycle import GOAL_STATES, LifecycleError, assert_transition  # noqa: E402
from framework.outbox import (  # noqa: E402
    EVENT_GOAL_COMPLETED,
    EVENT_TASK_BLOCKED,
    EVENT_TASK_WAITING,
    OUTBOX_DIRNAME,
    STATUS_PENDING,
    STATUS_SENT,
    CrashSimulated,
    DedupStore,
    OutboxStore,
    commit_transition,
    open_outbox,
    persist_dir,
    pump_outbox,
    reset_outbox_cache,
)

HELLO = REPO / "jobs/examples/hello.charter.yaml"
CONSUMER = REPO / "jobs/examples/consumer.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife13", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _OutboxCase(unittest.TestCase):
    def setUp(self):
        reset_outbox_cache()

    def tearDown(self):
        reset_outbox_cache()


class TestGoalLifecycle(_OutboxCase):
    def test_goal_completed_edge(self):
        self.assertIn("completed", GOAL_STATES)
        assert_transition("goal", "running", "completed")
        assert_transition("task", "queued", "running")
        assert_transition("task", "running", "succeeded")

    def test_illegal_goal_edge(self):
        with self.assertRaises(LifecycleError):
            assert_transition("goal", "completed", "running")


class TestAtomicCommit(_OutboxCase):
    def test_state_and_outbox_same_txn(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            task = {"task_id": "task_run", "goal_id": "goal_run", "status": "queued"}
            out = commit_transition(store, task, "running", reason="enter_running")
            self.assertTrue(out["ok"], out)
            self.assertEqual(store.entity_state("task", "task_run"), "running")
            pending = store.list_rows(status=STATUS_PENDING)
            self.assertEqual(len(pending), 1)
            ev = pending[0].event
            self.assertEqual(ev["contract_version"], "contract.v0.1-draft")
            self.assertEqual(ev["goal_id"], "goal_run")
            self.assertEqual(ev["task_id"], "task_run")
            self.assertEqual(ev["payload"]["from_state"], "queued")
            self.assertEqual(ev["payload"]["to_state"], "running")
            self.assertTrue((persist_dir(td) / "entities.json").is_file())
            self.assertTrue((persist_dir(td) / "outbox.json").is_file())
            self.assertFalse(store.journal_path().is_file())
            self.assertIn(OUTBOX_DIRNAME, str(store.path))

    def test_illegal_transition_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"task_id": "t1", "goal_id": "g1"},
                "succeeded",
                from_state="running",
            )
            with self.assertRaises(LifecycleError):
                commit_transition(
                    store,
                    {"task_id": "t1", "goal_id": "g1"},
                    "running",
                )
            self.assertEqual(store.entity_state("task", "t1"), "succeeded")
            self.assertEqual(len(store.list_rows()), 1)

    def test_journal_crash_replay_does_not_lose_pending(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            with self.assertRaises(CrashSimulated):
                commit_transition(
                    store,
                    {"task_id": "t_crash", "goal_id": "g_crash"},
                    "running",
                    from_state="queued",
                    crash="after_journal",
                )
            self.assertTrue(store.journal_path().is_file())
            reset_outbox_cache()
            store2 = OutboxStore.open(td)
            self.assertEqual(store2.entity_state("task", "t_crash"), "running")
            self.assertGreaterEqual(store2.pending_count(), 1)
            self.assertFalse(store2.journal_path().is_file())
            disk = json.loads((persist_dir(td) / "outbox.json").read_text(encoding="utf-8"))
            self.assertTrue(disk.get("rows"))


class TestCoveredTransitions(_OutboxCase):
    def test_task_queued_to_running(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"task_id": "t_qr", "goal_id": "g_qr"},
                "running",
                from_state="queued",
                reason="enter_running",
            )
            self.assertEqual(store.entity_state("task", "t_qr"), "running")
            ev = store.list_rows()[0].event
            self.assertEqual(ev["payload"]["from_state"], "queued")
            self.assertEqual(ev["payload"]["to_state"], "running")

    def test_blocked_or_queued_due_to_deps(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"task_id": "t_dep", "goal_id": "g_dep"},
                "queued",
                reason="unsatisfied_deps",
                extra={"unsatisfied_deps": ["artifact:producer.txt"]},
            )
            self.assertEqual(store.entity_state("task", "t_dep"), "queued")
            ev = store.list_rows()[0].event
            self.assertEqual(ev["type"], EVENT_TASK_WAITING)
            self.assertEqual(ev["payload"]["reason"], "unsatisfied_deps")

    def test_blocked_due_to_resources(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"task_id": "t_res", "goal_id": "g_res"},
                "blocked",
                from_state="queued",
                reason="workdir_occupied",
            )
            self.assertEqual(store.entity_state("task", "t_res"), "blocked")
            ev = store.list_rows()[0].event
            self.assertEqual(ev["type"], EVENT_TASK_BLOCKED)

    def test_goal_completed(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"goal_id": "g_done", "kind": "goal"},
                "running",
                from_state="queued",
            )
            commit_transition(
                store,
                {"goal_id": "g_done", "kind": "goal"},
                "completed",
                reason="goal_completed",
            )
            self.assertEqual(store.entity_state("goal", "g_done"), "completed")
            types = [r.event["type"] for r in store.list_rows()]
            self.assertIn(EVENT_GOAL_COMPLETED, types)


class TestCrashReplayAndDedup(_OutboxCase):
    def test_crash_before_mark_sent_replay_still_delivers(self):
        """Pending outbox is not lost; at-least-once delivery on replay."""
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"task_id": "t_pump", "goal_id": "g_pump"},
                "running",
                from_state="queued",
            )
            recv = DedupStore.open(td)
            sink: list[dict] = []
            with self.assertRaises(CrashSimulated):
                pump_outbox(
                    store,
                    recv,
                    sink=sink.append,
                    crash_before_mark_sent=True,
                )
            self.assertEqual(len(sink), 1)
            self.assertEqual(len(recv.events), 1)
            self.assertEqual(store.sent_count(), 0)
            self.assertGreaterEqual(store.pending_count(), 1)

            reset_outbox_cache()
            store2 = OutboxStore.open(td)
            recv2 = DedupStore.open(td)
            self.assertEqual(store2.pending_count(), 1)
            self.assertEqual(len(recv2.events), 1)
            out = pump_outbox(store2, recv2, sink=sink.append)
            self.assertEqual(len(sink), 2, "external side effect is at-least-once")
            self.assertEqual(out["duplicate_count"], 1)
            self.assertEqual(len(recv2.events), 1, "receiver processed the event once")
            self.assertEqual(store2.sent_count(), 1)
            self.assertEqual(store2.pending_count(), 0)

    def test_second_identical_delivery_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"task_id": "t_dup", "goal_id": "g_dup"},
                "running",
                from_state="queued",
            )
            row = store.list_rows()[0]
            recv = DedupStore.open(td)
            first = recv.receive(row.event, delivery_key=row.delivery_key)
            self.assertTrue(first["accepted"])
            self.assertFalse(first["duplicate"])
            second = recv.receive(row.event, delivery_key=row.delivery_key)
            self.assertFalse(second["accepted"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(len(recv.events), 1)
            by_id = recv.receive(dict(row.event), delivery_key="")
            self.assertTrue(by_id["duplicate"])

    def test_at_least_once_is_not_exactly_once_side_effect(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            commit_transition(
                store,
                {"goal_id": "g_once", "kind": "goal"},
                "completed",
                from_state="running",
            )
            recv = DedupStore.open(td)
            sink: list[dict] = []
            try:
                pump_outbox(store, recv, sink=sink.append, crash_before_mark_sent=True)
            except CrashSimulated:
                pass
            pump_outbox(store, recv, sink=sink.append)
            self.assertGreaterEqual(len(sink), 2)
            self.assertEqual(len(recv.events), 1)
            self.assertEqual(store.list_rows()[0].status, STATUS_SENT)


class TestInprocessWiring(_OutboxCase):
    def test_hello_queued_running_and_goal_completed(self):
        with tempfile.TemporaryDirectory() as td:
            ch = load_charter(HELLO)
            result = run_inprocess_charter(charter=ch, workdir=td, claim_workdir=False)
            self.assertTrue(result["ok"], result)
            store = OutboxStore.open(td)
            tid = str(result.get("task_id") or "")
            gid = str(result.get("goal_id") or "")
            self.assertTrue(tid)
            self.assertTrue(gid)
            self.assertEqual(store.entity_state("task", tid), "succeeded")
            self.assertEqual(store.entity_state("goal", gid), "completed")
            types = [r.event.get("type") for r in store.list_rows()]
            self.assertIn(EVENT_GOAL_COMPLETED, types)
            payloads = [r.event.get("payload") or {} for r in store.list_rows()]
            self.assertTrue(
                any(p.get("from_state") == "queued" and p.get("to_state") == "running" for p in payloads),
                payloads,
            )

    def test_consumer_stays_queued_on_unsatisfied_deps(self):
        with tempfile.TemporaryDirectory() as td:
            ch = load_charter(CONSUMER)
            result = run_inprocess_charter(charter=ch, workdir=td, claim_workdir=False)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["state"], "queued")
            store = OutboxStore.open(td)
            rows = store.list_rows()
            self.assertTrue(rows)
            self.assertTrue(
                any(
                    r.event.get("type") == EVENT_TASK_WAITING
                    or (r.event.get("payload") or {}).get("reason") == "unsatisfied_deps"
                    for r in rows
                ),
                [r.event for r in rows],
            )
            tid = str(result.get("task_id") or "")
            if tid:
                self.assertEqual(store.entity_state("task", tid), "queued")

    def test_workdir_resource_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            reg = WorkdirClaimRegistry()
            held = reg.claim(td, holder_id="holder-1", job_name="holder")
            self.assertTrue(held.granted, held.to_dict())
            ch = load_charter(HELLO)
            result = run_inprocess_charter(
                charter=ch,
                workdir=td,
                claim_registry=reg,
                holder_id="challenger",
                on_conflict="block",
            )
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["state"], "blocked")
            store = OutboxStore.open(td)
            tid = str(result.get("task_id") or "")
            self.assertTrue(tid)
            self.assertEqual(store.entity_state("task", tid), "blocked")
            types = [r.event.get("type") for r in store.list_rows()]
            self.assertIn(EVENT_TASK_BLOCKED, types)
            reg.release(td, "holder-1")


class TestRunJobCliKnife13(_OutboxCase):
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
            self.assertNotIn("outbox", summary)

    def test_default_entry_not_inprocess(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")
            self.assertNotIn("backend", summary)


if __name__ == "__main__":
    unittest.main()
