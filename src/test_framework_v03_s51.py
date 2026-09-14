#!/usr/bin/env python3
"""v0.3 §5.1: durable consistency — cross-process RMW lock, no silent empty overwrite."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.goal_budget import (  # noqa: E402
    BudgetCost,
    BudgetLimits,
    GoalBudget,
    reset_goal_budget_cache,
)
from framework.durable_api import (  # noqa: E402
    DurableLayer,
    reset_durable_cache,
    store_path,
    submit_goal,
)
from framework.goal_ownership import (  # noqa: E402
    GoalOwnershipStore,
    persist_path as ownership_path,
    reset_goal_ownership_cache,
)
from framework.models import CONTRACT_VERSION  # noqa: E402
from framework.outbox import OutboxStore, reset_outbox_cache  # noqa: E402
from framework.persist_lock import (  # noqa: E402
    StoreCorruptError,
    StoreIncompatibleError,
    StorePermissionError,
    persist_lock,
)

# Independent-process worker. sys.argv[1] is src/, then op + args.
_WORKER = r"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

src = sys.argv[1]
sys.path.insert(0, src)
op = sys.argv[2]

from framework import persist_lock as pl
from framework.durable_api import DurableLayer, reset_durable_cache
from framework.goal_ownership import GoalOwnershipStore, reset_goal_ownership_cache
from execution_backend.goal_budget import BudgetCost, BudgetLimits, GoalBudget, reset_goal_budget_cache
from framework.outbox import OutboxStore, reset_outbox_cache, make_event

def arm_stall(path: str) -> None:
    p = Path(path)
    def hook():
        p.write_text("ready", encoding="utf-8")
        deadline = time.time() + 20
        while time.time() < deadline:
            if not p.exists():
                return
            if p.read_text(encoding="utf-8").strip() == "go":
                return
            time.sleep(0.02)
        raise SystemExit("stall timeout")
    pl.after_lock_reload_hook = hook

def dump(obj):
    print(json.dumps(obj, default=str))

if op == "submit":
    root, key, title = sys.argv[3], sys.argv[4], sys.argv[5]
    stall = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else ""
    wait_file = sys.argv[7] if len(sys.argv) > 7 and sys.argv[7] else ""
    reset_durable_cache()
    layer = DurableLayer.open(root)
    if wait_file:
        marker = Path(wait_file)
        marker.write_text("opened", encoding="utf-8")
        deadline = time.time() + 20
        while time.time() < deadline:
            if marker.read_text(encoding="utf-8").strip() == "go":
                break
            time.sleep(0.02)
        else:
            raise SystemExit("wait_file timeout")
    if stall:
        arm_stall(stall)
    out = layer.submit_goal(submit_key=key, title=title, desired_outcome=title)
    dump({"ok": out.get("ok"), "goal_id": out.get("goal_id"), "reason": out.get("reason"), "created": out.get("created"), "goal_count": out.get("goal_count")})
elif op == "claim":
    root, gid, cid = sys.argv[3], sys.argv[4], sys.argv[5]
    stall = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else ""
    reset_goal_ownership_cache()
    store = GoalOwnershipStore.open(gid, root)
    if stall:
        arm_stall(stall)
    out = store.claim(cid)
    dump({"ok": out.get("ok"), "reason": out.get("reason"), "coordinator": (out.get("ownership") or {}).get("coordinator_id")})
elif op == "reserve":
    root, gid = sys.argv[3], sys.argv[4]
    stall = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else ""
    reset_goal_budget_cache()
    acc = GoalBudget.open(gid, root, limits=BudgetLimits(max_attempts=1, max_reworks=0, max_approvals=0, wall_sec=60))
    if stall:
        arm_stall(stall)
    rec = acc.reserve(cost=BudgetCost(attempts=1))
    dump({"granted": rec.granted, "status": rec.status, "reason": rec.reason})
elif op == "outbox":
    root, eid = sys.argv[3], sys.argv[4]
    stall = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else ""
    reset_outbox_cache()
    store = OutboxStore.open(root)
    if stall:
        arm_stall(stall)
    ev = make_event(type="task.state_changed", goal_id="g", task_id=eid, payload={"entity_id": eid})
    out = store.append_in_txn([ev], entities={f"task:{eid}": {"kind": "task", "task_id": eid, "state": "queued"}})
    dump({"ok": out.get("ok"), "eid": eid})
elif op == "crash_after_claim":
    root, key, title, cid = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
    import framework.durable_api as dapi
    reset_durable_cache()
    reset_goal_ownership_cache()
    def boom():
        raise SystemExit(17)
    dapi.after_ownership_claim_hook = boom
    layer = DurableLayer.open(root)
    layer.submit_goal(submit_key=key, title=title, desired_outcome=title, coordinator_id=cid)
    dump({"unexpected": True})
else:
    raise SystemExit("unknown op " + op)
"""


def _spawn(args: list[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(SRC), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_text(path: Path, expected: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path} == {expected!r} (have {path.read_text() if path.is_file() else None!r})")


def _finish(proc: subprocess.Popen, timeout: float = 20.0) -> dict:
    out, err = proc.communicate(timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"worker rc={proc.returncode} stdout={out!r} stderr={err!r}")
    line = (out or "").strip().splitlines()[-1]
    return json.loads(line)


class _S51Case(unittest.TestCase):
    def setUp(self):
        reset_durable_cache()
        reset_goal_ownership_cache()
        reset_goal_budget_cache()
        reset_outbox_cache()

    def tearDown(self):
        reset_durable_cache()
        reset_goal_ownership_cache()
        reset_goal_budget_cache()
        reset_outbox_cache()


@unittest.skipIf(sys.platform == "win32", "§5.1 Linux fcntl.flock only; Windows lock is §5.3")
class TestCrossProcessSubmitKeepsBothGoals(_S51Case):
    def test_two_processes_interleaved_submit_keep_both_goals(self):
        """A stalls after reload while holding the RMW lock; B waits; both Goals remain."""
        with tempfile.TemporaryDirectory() as td:
            stall = Path(td) / "stall"
            stall.write_text("armed", encoding="utf-8")
            a = _spawn(["submit", td, "key-a", "goal-a", str(stall)])
            _wait_text(stall, "ready")
            b = _spawn(["submit", td, "key-b", "goal-b", ""])
            time.sleep(0.4)
            self.assertIsNone(b.poll(), "B must block while A holds the RMW lock")
            stall.write_text("go", encoding="utf-8")
            ra = _finish(a)
            rb = _finish(b)
            self.assertTrue(ra["ok"], ra)
            self.assertTrue(rb["ok"], rb)
            self.assertTrue(ra["created"])
            self.assertTrue(rb["created"])
            self.assertNotEqual(ra["goal_id"], rb["goal_id"])
            reset_durable_cache()
            layer = DurableLayer.open(td)
            self.assertEqual(layer.goal_count(), 2)
            store = json.loads(store_path(td).read_text(encoding="utf-8"))
            self.assertEqual(set(store["submit_keys"]), {"key-a", "key-b"})
            self.assertEqual(len(store["goals"]), 2)

    def test_stale_cache_reload_does_not_clobber_other_process_goal(self):
        """Process A opens (caches empty), B submits, A then submits — both Goals kept."""
        with tempfile.TemporaryDirectory() as td:
            wait = Path(td) / "wait"
            a = _spawn(["submit", td, "key-a", "goal-a", "", str(wait)])
            _wait_text(wait, "opened")
            b = _spawn(["submit", td, "key-b", "goal-b", ""])
            rb = _finish(b)
            self.assertTrue(rb["ok"], rb)
            wait.write_text("go", encoding="utf-8")
            ra = _finish(a)
            self.assertTrue(ra["ok"], ra)
            reset_durable_cache()
            layer = DurableLayer.open(td)
            self.assertEqual(layer.goal_count(), 2)
            self.assertNotEqual(ra["goal_id"], rb["goal_id"])


@unittest.skipIf(sys.platform == "win32", "§5.1 Linux fcntl.flock only; Windows lock is §5.3")
class TestConflictingOwnershipAndBudget(_S51Case):
    def test_conflicting_ownership_claims_at_most_one_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            stall = Path(td) / "stall"
            stall.write_text("armed", encoding="utf-8")
            a = _spawn(["claim", td, "goal_conflict", "coord-a", str(stall)])
            _wait_text(stall, "ready")
            b = _spawn(["claim", td, "goal_conflict", "coord-b", ""])
            time.sleep(0.4)
            self.assertIsNone(b.poll(), "B must block on ownership RMW lock")
            stall.write_text("go", encoding="utf-8")
            ra = _finish(a)
            rb = _finish(b)
            wins = [x for x in (ra, rb) if x.get("ok")]
            losses = [x for x in (ra, rb) if not x.get("ok")]
            self.assertEqual(len(wins), 1, {"a": ra, "b": rb})
            self.assertEqual(len(losses), 1, {"a": ra, "b": rb})
            self.assertEqual(losses[0]["reason"], "already_owned")
            reset_goal_ownership_cache()
            store = GoalOwnershipStore.open("goal_conflict", td)
            self.assertEqual(store.current().coordinator_id, wins[0]["coordinator"])

    def test_conflicting_budget_reserve_at_most_one_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            stall = Path(td) / "stall"
            stall.write_text("armed", encoding="utf-8")
            a = _spawn(["reserve", td, "goal_budget", str(stall)])
            _wait_text(stall, "ready")
            b = _spawn(["reserve", td, "goal_budget", ""])
            time.sleep(0.4)
            self.assertIsNone(b.poll(), "B must block on budget RMW lock")
            stall.write_text("go", encoding="utf-8")
            ra = _finish(a)
            rb = _finish(b)
            granted = [x for x in (ra, rb) if x.get("granted")]
            denied = [x for x in (ra, rb) if not x.get("granted")]
            self.assertEqual(len(granted), 1, {"a": ra, "b": rb})
            self.assertEqual(len(denied), 1, {"a": ra, "b": rb})
            self.assertEqual(denied[0]["reason"], "budget_exhausted")

    def test_outbox_two_writers_keep_both_entities(self):
        with tempfile.TemporaryDirectory() as td:
            stall = Path(td) / "stall"
            stall.write_text("armed", encoding="utf-8")
            a = _spawn(["outbox", td, "task-a", str(stall)])
            _wait_text(stall, "ready")
            b = _spawn(["outbox", td, "task-b", ""])
            time.sleep(0.4)
            self.assertIsNone(b.poll())
            stall.write_text("go", encoding="utf-8")
            ra = _finish(a)
            rb = _finish(b)
            self.assertTrue(ra["ok"], ra)
            self.assertTrue(rb["ok"], rb)
            reset_outbox_cache()
            store = OutboxStore.open(td)
            self.assertIsNotNone(store.get_entity("task", "task-a"))
            self.assertIsNotNone(store.get_entity("task", "task-b"))


class TestCorruptIncompatibleKeepOriginal(_S51Case):
    def test_corrupt_store_raises_and_keeps_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            layer.submit_goal(submit_key="keep-me", title="keep", desired_outcome="keep")
            path = store_path(td)
            original = path.read_bytes()
            path.write_bytes(b"{not-json")
            damaged = path.read_bytes()
            reset_durable_cache()
            with self.assertRaises(StoreCorruptError) as ctx:
                DurableLayer.open(td)
            self.assertEqual(ctx.exception.reason, "corrupt")
            self.assertEqual(path.read_bytes(), damaged)
            self.assertNotEqual(path.read_bytes(), b"")
            self.assertNotIn(b'"submit_keys": {}', path.read_bytes())
            # original Goal payload is still in the damaged-or... wait, we overwrote with garbage.
            # The garbage itself must be unchanged (not replaced with empty store).
            self.assertEqual(path.read_bytes(), b"{not-json")
            self.assertNotEqual(original, path.read_bytes())

    def test_incompatible_object_shape_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            DurableLayer.open(td)
            path = store_path(td)
            payload = {"contract_version": CONTRACT_VERSION, "goals": [], "submit_keys": {}}
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            original = path.read_bytes()
            reset_durable_cache()
            with self.assertRaises(StoreIncompatibleError) as ctx:
                DurableLayer.open(td)
            self.assertEqual(ctx.exception.reason, "incompatible")
            self.assertEqual(path.read_bytes(), original)

    def test_unsupported_contract_version_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            DurableLayer.open(td)
            path = store_path(td)
            payload = {
                "contract_version": "contract.v9-not-a-thing",
                "goals": {"g1": {"goal_id": "g1"}},
                "submit_keys": {"k": "g1"},
            }
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            original = path.read_bytes()
            reset_durable_cache()
            with self.assertRaises(StoreIncompatibleError):
                DurableLayer.open(td)
            self.assertEqual(path.read_bytes(), original)

    def test_json_array_is_incompatible_not_empty_init(self):
        with tempfile.TemporaryDirectory() as td:
            DurableLayer.open(td)
            path = store_path(td)
            path.write_text("[1, 2, 3]\n", encoding="utf-8")
            original = path.read_bytes()
            reset_durable_cache()
            with self.assertRaises(StoreIncompatibleError):
                DurableLayer.open(td)
            self.assertEqual(path.read_bytes(), original)

    def test_permission_denied_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            DurableLayer.open(td)
            path = store_path(td)
            original = path.read_bytes()
            reset_durable_cache()

            def boom(*_a, **_k):
                raise PermissionError("denied")

            with patch.object(Path, "read_text", boom):
                with self.assertRaises(StorePermissionError) as ctx:
                    DurableLayer.open(td)
            self.assertEqual(ctx.exception.reason, "permission")
            self.assertEqual(path.read_bytes(), original)

    def test_leftover_tmp_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td)
            first = layer.submit_goal(submit_key="real", title="real", desired_outcome="real")
            path = store_path(td)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "submit_keys": {"ghost": "goal_ghost"},
                        "goals": {"goal_ghost": {"goal_id": "goal_ghost", "submit_key": "ghost"}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            reset_durable_cache()
            reopened = DurableLayer.open(td)
            self.assertEqual(reopened.goal_count(), 1)
            self.assertEqual(reopened.get_goal(first["goal_id"])["ok"], True)
            store = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("real", store["submit_keys"])
            self.assertNotIn("ghost", store["submit_keys"])

    def test_ownership_corrupt_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            store = GoalOwnershipStore.open("g1", td)
            store.claim("coord-a")
            path = ownership_path(td, "g1")
            path.write_bytes(b"{{{{")
            damaged = path.read_bytes()
            reset_goal_ownership_cache()
            with self.assertRaises(StoreCorruptError):
                GoalOwnershipStore.open("g1", td)
            self.assertEqual(path.read_bytes(), damaged)

    def test_budget_incompatible_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            GoalBudget.open("g1", td, limits=BudgetLimits(max_attempts=2))
            path = GoalBudget.open("g1", td).path
            payload = {"goal_id": "g1", "consumed": [], "reserved": {}}
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            original = path.read_bytes()
            reset_goal_budget_cache()
            with self.assertRaises(StoreIncompatibleError):
                GoalBudget.open("g1", td)
            self.assertEqual(path.read_bytes(), original)


@unittest.skipIf(sys.platform == "win32", "§5.1 Linux fcntl.flock only; Windows lock is §5.3")
class TestInterruptRecovery(_S51Case):
    def test_crash_after_ownership_claim_before_store_does_not_invent_empty_overwrite(self):
        """Crash after side-file claim, before store.json replace.

        Canonical Goal set is store.json. Retry with the same submit_key must
        create the Goal (it never committed). store.json must not be wiped
        into a silent empty overwrite of a prior Goal.
        """
        with tempfile.TemporaryDirectory() as td:
            prior = submit_goal(td, submit_key="prior", title="prior", desired_outcome="prior")
            self.assertTrue(prior["ok"], prior)
            prior_bytes = store_path(td).read_bytes()
            reset_durable_cache()
            reset_goal_ownership_cache()
            proc = _spawn(["crash_after_claim", td, "new-key", "new-title", "coord-x"])
            out, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 17, f"stdout={out!r} stderr={err!r}")
            after = store_path(td).read_bytes()
            store = json.loads(after)
            self.assertIn("prior", store["submit_keys"])
            self.assertNotIn("new-key", store["submit_keys"])
            self.assertEqual(len(store["goals"]), 1)
            # Original committed Goal still present (not replaced by empty library).
            self.assertIn(prior["goal_id"].encode(), after)
            self.assertNotEqual(after.strip(), b"")
            reset_durable_cache()
            retry = submit_goal(
                td,
                submit_key="new-key",
                title="new-title",
                desired_outcome="new-title",
                coordinator_id="coord-x",
            )
            self.assertTrue(retry["ok"], retry)
            reset_durable_cache()
            layer = DurableLayer.open(td)
            self.assertEqual(layer.goal_count(), 2)
            self.assertTrue(layer.get_goal(prior["goal_id"])["ok"])
            self.assertTrue(layer.get_goal(retry["goal_id"])["ok"])
            # prior bytes were a committed snapshot; crash must not have dropped that Goal
            self.assertIn("prior", json.loads(store_path(td).read_text(encoding="utf-8"))["submit_keys"])
            self.assertTrue(prior_bytes)  # silence unused if assert above holds

    def test_outbox_journal_replay_after_crash_keeps_pending(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            from framework.outbox import CrashSimulated, make_event

            ev = make_event(type="task.state_changed", goal_id="g", task_id="t1", payload={"entity_id": "t1"})
            with self.assertRaises(CrashSimulated):
                store.append_in_txn(
                    [ev],
                    entities={"task:t1": {"kind": "task", "task_id": "t1", "state": "queued"}},
                    crash="after_journal",
                )
            reset_outbox_cache()
            store2 = OutboxStore.open(td)
            self.assertIsNotNone(store2.get_entity("task", "t1"))
            self.assertGreaterEqual(store2.pending_count(), 1)

    def test_corrupt_journal_does_not_wipe_live_outbox(self):
        with tempfile.TemporaryDirectory() as td:
            store = OutboxStore.open(td)
            from framework.outbox import make_event

            ev = make_event(type="task.state_changed", goal_id="g", task_id="t1", payload={"entity_id": "t1"})
            store.append_in_txn([ev], entities={"task:t1": {"kind": "task", "task_id": "t1", "state": "queued"}})
            live = store.entities_path().read_bytes()
            store.journal_path().write_bytes(b"{bad")
            reset_outbox_cache()
            with self.assertRaises(StoreCorruptError):
                OutboxStore.open(td)
            self.assertEqual(store.entities_path().read_bytes(), live)


class TestPersistLockCoversRMW(_S51Case):
    def test_nested_lock_same_root_does_not_drop_flock(self):
        with tempfile.TemporaryDirectory() as td:
            with persist_lock(td):
                with persist_lock(td):
                    (Path(td) / "x").write_text("ok", encoding="utf-8")
            self.assertEqual((Path(td) / "x").read_text(encoding="utf-8"), "ok")


if __name__ == "__main__":
    unittest.main()
