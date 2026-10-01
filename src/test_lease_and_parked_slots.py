"""#10/#13 box retest: leases released on cancel, dead leases do not block
startup, parked checkpoints do not hold run slots. Offline fakes only."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from execution_backend import get_execution_backend
from execution_backend.agy_account_pool import (
    AccountPoolError,
    ENV_POOL,
    inject_agy_pool_into_backend_kwargs,
    load_pool,
)
from framework.concurrency import count_inflight, occupies_run_slot, scheduler_view

_SLEEPY_AGY = r'''#!/usr/bin/env python3
import json, sys, time
time.sleep(30)
print(json.dumps({"conversation_id": "c", "status": "SUCCESS", "response": "ok"}))
'''


def _fake(td: str) -> Path:
    root = Path(td)
    if os.name == "nt":
        script = root / "sleepy-agy.py"
        script.write_text(_SLEEPY_AGY, encoding="utf-8")
        cmd = root / "sleepy-agy.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return cmd
    path = root / "sleepy-agy"
    path.write_text(_SLEEPY_AGY, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _pool(td: str, accounts: list[dict]) -> Path:
    path = Path(td) / "pool.json"
    path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    return path


def _env(extra: dict) -> dict:
    env = os.environ.copy()
    for key in ("AGY_AUTO_APPROVE", "COLLAB_AGY_AUTO_APPROVE", ENV_POOL, "AGY_PROFILE",
                "COLLAB_AGY_POOL_PRECHECK", "COLLAB_AGY_POOL_LIVE"):
        env.pop(key, None)
    env.update(extra)
    return env


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class CancelReleasesLeaseTests(unittest.TestCase):
    def test_cancel_frees_the_busy_lease(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "homeA"
            home.mkdir()
            pool_path = _pool(td, [{"id": "A", "home": str(home), "state": "available"}])
            backend = get_execution_backend(
                "antigravity",
                account_pool_path=str(pool_path),
                environ=_env({"AGY_BIN": str(_fake(td))}),
            )
            ws = Path(td) / "ws"
            ws.mkdir()
            started = backend.start_run(title="t", directory=str(ws), instruction="x", artifacts=[])
            self.assertTrue(started.get("ok"), started)
            self.assertEqual(load_pool(pool_path).by_id("A").state, "busy")
            code, body = backend.cancel(started["run_id"])
            self.assertEqual(code, 200, body)
            acc = load_pool(pool_path).by_id("A")
            self.assertEqual(acc.state, "available")
            self.assertIsNone(acc.lease_id)
            # The only account is reusable right away, not after lease_until.
            ws2 = Path(td) / "ws2"
            ws2.mkdir()
            again = backend.start_run(title="t2", directory=str(ws2), instruction="x", artifacts=[])
            self.assertTrue(again.get("ok"), again)
            backend.cancel(again["run_id"])
            self.assertEqual(load_pool(pool_path).by_id("A").state, "available")


class StartupProbeTests(unittest.TestCase):
    def test_dead_holder_with_future_lease_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "homeA"
            home.mkdir()
            future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            pool_path = _pool(td, [{"id": "A", "home": str(home), "state": "busy",
                                    "lease_pid": _dead_pid(), "lease_until": future, "lease_id": "old"}])
            before = pool_path.read_text(encoding="utf-8")
            kw = inject_agy_pool_into_backend_kwargs({"account_pool_path": str(pool_path)})
            self.assertEqual(kw["account_pool_path"], str(pool_path))
            # Probe only: the file still says busy until a dispatch reclaims it.
            self.assertEqual(pool_path.read_text(encoding="utf-8"), before)

    def test_live_holder_still_blocks_startup(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "homeA"
            home.mkdir()
            future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                pool_path = _pool(td, [{"id": "A", "home": str(home), "state": "busy",
                                        "lease_pid": holder.pid, "lease_until": future, "lease_id": "live"}])
                with self.assertRaises(AccountPoolError):
                    inject_agy_pool_into_backend_kwargs({"account_pool_path": str(pool_path)})
            finally:
                holder.kill()
                holder.wait()


class ParkedCheckpointSlotTests(unittest.TestCase):
    def test_parked_checkpoint_does_not_occupy_a_run_slot(self):
        parked = {"task_id": "p", "status": "awaiting_decision", "run_parked": True,
                  "parked_decision": "d1", "blocked_on_decision": "d1"}
        live_question = {"task_id": "q", "status": "awaiting_decision", "blocked_on_decision": "d2"}
        stale_marker = {"task_id": "s", "status": "awaiting_decision", "run_parked": True,
                        "parked_decision": "d1", "blocked_on_decision": "d9"}
        running = {"task_id": "r", "status": "running"}
        self.assertFalse(occupies_run_slot(parked))
        self.assertTrue(occupies_run_slot(live_question))
        self.assertTrue(occupies_run_slot(stale_marker))
        self.assertTrue(occupies_run_slot(running))
        self.assertFalse(occupies_run_slot({"status": "queued", "run_parked": True}))
        self.assertEqual(count_inflight([parked, live_question, stale_marker, running]), 3)

    def test_scheduler_has_capacity_next_to_a_parked_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            parked = {"task_id": "p", "status": "awaiting_decision", "run_parked": True,
                      "parked_decision": "d1", "blocked_on_decision": "d1"}
            ready = {"task_id": "n", "status": "queued", "depends_on": []}
            view = scheduler_view(tasks=[parked, ready], pending=[], per_goal=1, global_limit=4,
                                  backend_limit=1, running_elsewhere=0, held_keys=set(),
                                  workspaces_root=td, goal_id="g")
            self.assertEqual(view["running"], 0)
            self.assertNotEqual(view["waiting_reason"], "capacity")

    def test_open_checkpoint_marks_task_parked(self):
        from framework.app_service import CollabApplication
        from test_app_service import _request

        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            goal = app.submit(_request())["goal_id"]
            app.layer.add_child_task(goal, task={"task_id": "T1", "title": "t", "status": "running",
                                                 "depends_on": [], "inputs": {"instruction": "x"},
                                                 "expected_artifacts": ["delivery.txt"]})
            opened = app.layer.open_checkpoint(goal, task_id="T1", run_id="r1", summary="checkpoint: x",
                                               result={"ok": False}, reason="checkpoint: x")
            self.assertTrue(opened.get("ok"), opened)
            task = next(t for t in app.layer.get_goal(goal)["goal"]["tasks"] if t["task_id"] == "T1")
            self.assertEqual(task["status"], "awaiting_decision")
            self.assertIs(task["run_parked"], True)
            self.assertEqual(task["parked_decision"], task["blocked_on_decision"])
            self.assertFalse(occupies_run_slot(task))


if __name__ == "__main__":
    unittest.main()
