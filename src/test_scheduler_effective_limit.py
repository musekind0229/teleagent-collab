#!/usr/bin/env python3
"""S: status scheduler uses the async_v1 dispatch limit.

U1–U3 call the real coordinator methods on a stand-in. E1–E2 reuse the
single-account CollabApplication harness via test_backend_run_limit_async._bind.
C1 checks summarize_status. No real agy, grok, or codex binary.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import test_agy_single_account_slots as slot_goals  # noqa: E402
import test_backend_run_limit_async as limit_async  # noqa: E402
from framework.app_service import AppCoordinator  # noqa: E402
from framework.concurrency import as_limit, public_concurrency, scheduler_view  # noqa: E402

ROOT = SRC.parent
_SCRIPT = ROOT / "bin" / "hermes-collab-request.py"
_OLD_KEYS = {"running", "queued_ready", "capacity", "waiting_reason"}
_QUERY = "goal-q"
_OTHER = "goal-other"
_REAL_METHODS = ("scheduler_snapshot", "_slots_for", "_occupancy", "_backend_run_limit")


def _load_client():
    spec = importlib.util.spec_from_file_location("hermes_collab_request_scheduler_view", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HCR = _load_client()


def _has_queued(status: dict) -> bool:
    return any(
        isinstance(task, dict) and task.get("status") == "queued" and not task.get("run_id")
        for task in (status.get("tasks") or [])
    )


def _was_dispatched(status: dict) -> bool:
    return any(
        isinstance(task, dict) and (task.get("run_id") or task.get("status") == "running")
        for task in (status.get("tasks") or [])
    )


class SchedulerEffectiveLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None

    def _stand_in(self, root: Path, *, pending, mode="async_v1", max_runs=1, missing_mode: bool = False):
        """Real coordinator methods on a stand-in. Rows: one other running task, one queued ready task."""
        caps = limit_async._concurrency(max_runs=max_runs, pending=pending)
        if missing_mode:
            backend = limit_async._NoModeBackend(caps)
        else:
            backend = limit_async._ModeBackend(caps, mode)
        other = {"task_id": "t-other", "goal_id": _OTHER, "status": "running"}
        query = {"task_id": "t-query", "goal_id": _QUERY, "status": "queued", "depends_on": []}
        rows = [(_OTHER, [other]), (_QUERY, [query])]
        coord = SimpleNamespace(
            backend=backend,
            max_parallel_per_goal=2,
            max_parallel_global=4,
            workspaces_root=root,
            _goal_task_rows=lambda bound=rows: [(gid, list(tasks)) for gid, tasks in bound],
        )
        for name in _REAL_METHODS:
            setattr(coord, name, MethodType(getattr(AppCoordinator, name), coord))
        snap = {"goal_id": _QUERY, "tasks": [query], "pending_decisions": []}
        conc = public_concurrency(
            max_parallel_per_goal=coord.max_parallel_per_goal,
            max_parallel_global=coord.max_parallel_global,
            backend_caps=backend.capabilities(),
        )
        return coord, snap, conc

    def _snapshot(self, coord, snap, conc):
        return coord.scheduler_snapshot(_QUERY, snap, {"concurrency": conc})

    def _limit_with_parts(self, coord, parts: dict):
        try:
            return coord._backend_run_limit(parts)
        except TypeError as exc:
            self.fail(
                "U3: _backend_run_limit does not take parts; "
                f"a None limit must leave parts empty ({exc})"
            )

    def _host(self, method_name: str):
        return limit_async.BackendRunLimitAsyncTests._bind(
            self,
            slot_goals.AgySingleAccountSlotTests,
            method_name,
        )

    def _status_while_queued(self, host, app, goal_id: str, timeout: float = 8.0) -> dict:
        # Submit only stores the goal. The tick that plans also dispatches, so hold
        # this goal's _dispatch_one until its queued task is visible.
        before = len(host._agy())
        status = app.status(goal_id)
        if _has_queued(status):
            return status
        coord = app.coordinator
        original = coord._dispatch_one

        def hold(gid: str, task_id: str):
            if gid == goal_id:
                return None
            return original(gid, task_id)

        coord._dispatch_one = hold
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                slot_goals._process_all(app)
                status = app.status(goal_id)
                if len(host._agy()) > before or _was_dispatched(status):
                    break
                if _has_queued(status):
                    return status
                time.sleep(0.05)
        finally:
            coord._dispatch_one = original
        return app.status(goal_id)

    def _drive_terminal(self, app, goal_ids: list[str], timeout: float) -> dict[str, dict]:
        deadline = time.monotonic() + timeout
        states = {gid: app.status(gid) for gid in goal_ids}
        while time.monotonic() < deadline:
            if all(str(row.get("state") or "") in slot_goals._TERMINAL for row in states.values()):
                return states
            slot_goals._process_all(app)
            states = {gid: app.status(gid) for gid in goal_ids}
            time.sleep(0.05)
        return states

    def test_u1_async_v1_snapshot_matches_slots_for(self) -> None:
        """U1: asserts async_v1 scheduler_snapshot capacity is 1 with waiting_reason empty when one review is parked, max(0, capacity-running) equals _slots_for 1, and the view exposes execution_limit 1, parked_review_runs 1, active_task_limit 2; pending 0 keeps capacity 0, waiting_reason capacity, _slots_for 0, and the same keys with parked 0 and active 1."""
        with tempfile.TemporaryDirectory(prefix="sched-u1-") as td:
            root = Path(td)
            with self.subTest(case="pending 1"):
                coord, snap, conc = self._stand_in(root, pending=1, mode="async_v1")
                _rows, _held, running = coord._occupancy()
                self.assertEqual(running.get(_OTHER), 1, running)
                self.assertEqual(running.get(_QUERY), 0, running)
                self.assertEqual(coord._backend_run_limit(), 2)
                view = self._snapshot(coord, snap, conc)
                slots = coord._slots_for(_QUERY)
                self.assertEqual(
                    view,
                    {
                        "running": 0,
                        "queued_ready": 1,
                        "capacity": 1,
                        "waiting_reason": "",
                        "execution_limit": 1,
                        "parked_review_runs": 1,
                        "active_task_limit": 2,
                    },
                    f"_slots_for={slots}; max(0, capacity-running) must equal it",
                )
                self.assertEqual(max(0, view["capacity"] - view["running"]), slots)
                self.assertEqual(slots, 1)
            with self.subTest(case="pending 0"):
                coord, snap, conc = self._stand_in(root, pending=0, mode="async_v1")
                self.assertEqual(coord._backend_run_limit(), 1)
                view = self._snapshot(coord, snap, conc)
                slots = coord._slots_for(_QUERY)
                self.assertEqual(slots, 0, view)
                self.assertEqual(
                    view,
                    {
                        "running": 0,
                        "queued_ready": 1,
                        "capacity": 0,
                        "waiting_reason": "capacity",
                        "execution_limit": 1,
                        "parked_review_runs": 0,
                        "active_task_limit": 1,
                    },
                    f"_slots_for={slots}",
                )
                self.assertEqual(max(0, view["capacity"] - view["running"]), slots)

    def test_u2_non_async_keeps_public_backend_max_runs(self) -> None:
        """U2: asserts a non-async_v1 backend with pending_review_runs 1 returns exactly the old 4 scheduler keys and equals scheduler_view called with the public backend_max_runs, with capacity 0, waiting_reason capacity, and _slots_for 0."""
        modes = (
            ("none", {"mode": None}),
            ("sync", {"mode": "sync"}),
            ("missing attribute", {"missing_mode": True}),
        )
        with tempfile.TemporaryDirectory(prefix="sched-u2-") as td:
            root = Path(td)
            for name, kwargs in modes:
                with self.subTest(case=name):
                    coord, snap, conc = self._stand_in(root, pending=1, **kwargs)
                    public_limit = as_limit(conc.get("backend_max_runs"))
                    self.assertEqual(public_limit, 1, conc)
                    self.assertEqual(coord.backend.capabilities()["concurrency"].get("pending_review_runs"), 1)
                    self.assertEqual(coord._backend_run_limit(), public_limit)
                    _rows, held, running = coord._occupancy()
                    elsewhere = sum(count for gid, count in running.items() if gid != _QUERY)
                    per = as_limit(conc.get("max_parallel_per_goal")) or coord.max_parallel_per_goal
                    glob = as_limit(conc.get("max_parallel_global")) or coord.max_parallel_global
                    expected = scheduler_view(
                        tasks=snap["tasks"],
                        pending=snap["pending_decisions"],
                        per_goal=per,
                        global_limit=glob,
                        backend_limit=public_limit,
                        running_elsewhere=elsewhere,
                        held_keys=held,
                        workspaces_root=coord.workspaces_root,
                        goal_id=_QUERY,
                    )
                    view = self._snapshot(coord, snap, conc)
                    self.assertEqual(set(view), _OLD_KEYS, view)
                    self.assertEqual(view, expected)
                    self.assertEqual(view["capacity"], 0, view)
                    self.assertEqual(view["waiting_reason"], "capacity", view)
                    self.assertEqual(coord._slots_for(_QUERY), 0, view)

    def test_u3_abnormal_pending_and_invalid_max_runs(self) -> None:
        """U3: asserts abnormal async_v1 pending yields parked_review_runs 0 and active_task_limit 1 with the same capacity and waiting_reason as pending 0, and _backend_run_limit(parts) returning None leaves parts empty so the snapshot keeps only the old 4 keys."""
        abnormal = (-1, True, "2", 1.5, None)
        with tempfile.TemporaryDirectory(prefix="sched-u3-") as td:
            root = Path(td)
            base_coord, base_snap, base_conc = self._stand_in(root, pending=0, mode="async_v1")
            pending0 = self._snapshot(base_coord, base_snap, base_conc)
            for pending in abnormal:
                with self.subTest(pending=pending):
                    coord, snap, conc = self._stand_in(root, pending=pending, mode="async_v1")
                    self.assertEqual(coord._backend_run_limit(), 1)
                    view = self._snapshot(coord, snap, conc)
                    self.assertEqual(view["capacity"], pending0["capacity"], view)
                    self.assertEqual(view["waiting_reason"], pending0["waiting_reason"], view)
                    self.assertEqual(view.get("parked_review_runs"), 0, view)
                    self.assertEqual(view.get("active_task_limit"), 1, view)
                    self.assertEqual(view.get("execution_limit"), 1, view)
            with self.subTest(case="invalid max_runs snapshot"):
                coord, snap, conc = self._stand_in(root, pending=1, mode="async_v1", max_runs=None)
                self.assertIsNone(coord._backend_run_limit())
                view = self._snapshot(coord, snap, conc)
                self.assertEqual(set(view), _OLD_KEYS, view)
            with self.subTest(case="invalid max_runs parts"):
                coord, snap, conc = self._stand_in(root, pending=1, mode="async_v1", max_runs=None)
                parts: dict = {}
                got = self._limit_with_parts(coord, parts)
                self.assertIsNone(got)
                self.assertEqual(parts, {})
                view = self._snapshot(coord, snap, conc)
                self.assertEqual(set(view), _OLD_KEYS, view)

    @unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy and lead are spawned through /bin/sh")
    def test_e1_parked_review_status_lets_the_next_goal_look_runnable(self) -> None:
        """E1: asserts one account with A awaiting_review shows B queued-ready with capacity and _slots_for at least 1 and waiting_reason empty, A exposes execution_limit 1, parked_review_runs 1, active_task_limit 2, capabilities max_runs stays 1, and a later tick spawns B with two agy rows and peak live agy 1."""
        host = self._host("test_s4a_parked_review_lets_other_goal_run")
        gate = host._gate("e1-lead")
        host._agy_steps(
            [
                host._ok("delivery.md", "from-a\n"),
                host._ok("side.md", "from-b\n"),
            ]
        )
        host._lead_steps([f"block_until:{gate}|90"])
        app = host._build()
        goal_a = host._submit("e1-a", wall=180, max_reworks=1)
        status_a = host._drive_until(app, goal_a, host._review_running, timeout=12.0)
        self.assertTrue(
            host._review_running(status_a),
            f"goal A never entered lead review; state={status_a.get('state')} "
            f"pending={status_a.get('pending_decisions')}",
        )
        run_a = host._run_id(status_a)
        saved = host._saved(run_a)
        self.assertEqual((saved or {}).get("state"), "awaiting_review", saved)
        self.assertGreaterEqual(len(host._wait_agy(1)), 1, host._agy())
        self.assertEqual(host._account().state, "available", host._account())
        agy_before = len(host._agy())
        self.assertEqual(agy_before, 1, host._agy())
        goal_b = host._submit("e1-b", prose=False, artifacts=["side.md"], max_reworks=0, wall=180)
        status_b = self._status_while_queued(host, app, goal_b)
        self.assertEqual(
            len(host._agy()),
            agy_before,
            "E1 harness: B was dispatched before the queued-ready snapshot",
        )
        self.assertTrue(
            _has_queued(status_b),
            "E1 harness: B's queued task was not visible before dispatch "
            f"(submit does not plan; the planning tick also dispatches). status={status_b}",
        )
        sched_b = dict(status_b["scheduler"])
        sched_a = dict(app.status(goal_a)["scheduler"])
        slots_b = app.coordinator._slots_for(goal_b)
        max_runs = host.backend.capabilities()["concurrency"].get("max_runs")
        self.assertEqual(max_runs, 1, host.backend.capabilities()["concurrency"])
        self.assertGreaterEqual(sched_b.get("queued_ready", 0), 1, sched_b)
        self.assertGreaterEqual(
            slots_b,
            1,
            f"dispatch would not start B while A is awaiting review; slots={slots_b} B={sched_b}",
        )
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and len(host._agy()) < 2:
            slot_goals._process_all(app)
            time.sleep(0.05)
        self.assertEqual(len(host._agy()), 2, host._agy())
        self.assertEqual(slot_goals._peak_live(host.agy_span), 1, slot_goals._jsonl(host.agy_span))
        detail = f"slots={slots_b} B={sched_b} A={sched_a}"
        with self.subTest(case="B waiting_reason"):
            self.assertEqual(sched_b.get("waiting_reason"), "", detail)
        with self.subTest(case="B capacity"):
            self.assertGreaterEqual(sched_b.get("capacity", 0), 1, detail)
        with self.subTest(case="slots match capacity"):
            self.assertEqual(max(0, int(sched_b["capacity"]) - int(sched_b["running"])), slots_b, detail)
        with self.subTest(case="A display limits"):
            self.assertEqual(sched_a.get("execution_limit"), 1, sched_a)
            self.assertEqual(sched_a.get("parked_review_runs"), 1, sched_a)
            self.assertEqual(sched_a.get("active_task_limit"), 2, sched_a)

    @unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy and lead are spawned through /bin/sh")
    def test_e2_rework_waiting_status_shows_no_room_then_all_complete(self) -> None:
        """E2: asserts B executing and A in rework_waiting leave C at capacity 0, waiting_reason capacity, and _slots_for 0 so a few ticks do not spawn C, A and B expose execution_limit 1, parked_review_runs 0, active_task_limit 1, then opening the gates completes A, B, and C with no lease and peak live agy 1."""
        host = self._host("test_s4b_rework_waits_for_the_single_account")
        gate_lead = host._gate("e2-lead")
        gate_b = host._gate("e2-agy")
        host._agy_steps(
            [
                host._ok("delivery.md", "round1\n"),
                {
                    "do": "block_until",
                    "path": str(gate_b),
                    "sec": 60,
                    "write": {"side.md": "side\n"},
                },
                host._ok("delivery.md", "round2\n"),
                host._ok("extra.md", "extra\n"),
            ]
        )
        host._lead_steps(
            [
                f"block_until_fail:{gate_lead}|needs work",
                "pass:round two",
            ]
        )
        app = host._build()
        goal_a = host._submit("e2-a", wall=180, max_reworks=1)
        status_a = host._drive_until(app, goal_a, host._review_running, timeout=12.0)
        self.assertTrue(
            host._review_running(status_a),
            f"goal A never entered lead review; state={status_a.get('state')} "
            f"pending={status_a.get('pending_decisions')}",
        )
        run_a = host._run_id(status_a)
        self.assertGreaterEqual(len(host._wait_agy(1)), 1, host._agy())
        goal_b = host._submit("e2-b", prose=False, artifacts=["side.md"], max_reworks=0, wall=180)
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and len(host._agy()) < 2:
            slot_goals._process_all(app)
            time.sleep(0.05)
        rows = host._agy()
        self.assertEqual(len(rows), 2, rows)
        b_pid = int(rows[1]["pid"])
        self.assertTrue(slot_goals._pid_alive(b_pid), b_pid)
        self.assertEqual(host._account().state, "busy", host._account())
        gate_lead.write_text("open", encoding="utf-8")
        saved = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            slot_goals._process_all(app)
            saved = host._saved(run_a)
            if (saved or {}).get("state") == "rework_waiting":
                break
            time.sleep(0.05)
        self.assertEqual((saved or {}).get("state"), "rework_waiting", saved)
        self.assertTrue(slot_goals._pid_alive(b_pid), b_pid)
        agy_before = len(host._agy())
        goal_c = host._submit("e2-c", prose=False, artifacts=["extra.md"], max_reworks=0, wall=180)
        status_c = app.status(goal_c)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not _has_queued(status_c):
            slot_goals._process_all(app)
            status_c = app.status(goal_c)
            if len(host._agy()) > agy_before:
                break
            time.sleep(0.05)
        self.assertEqual(len(host._agy()), agy_before, host._agy())
        self.assertTrue(
            _has_queued(status_c),
            f"E2 harness: C never reached queued-ready; status={status_c}",
        )
        for _ in range(5):
            slot_goals._process_all(app)
            time.sleep(0.05)
        self.assertEqual(len(host._agy()), agy_before, host._agy())
        self.assertTrue(slot_goals._pid_alive(b_pid), b_pid)
        status_c = app.status(goal_c)
        sched_c = dict(status_c["scheduler"])
        sched_a = dict(app.status(goal_a)["scheduler"])
        sched_b = dict(app.status(goal_b)["scheduler"])
        slots_c = app.coordinator._slots_for(goal_c)
        self.assertGreaterEqual(sched_c.get("queued_ready", 0), 1, sched_c)
        self.assertEqual(sched_c.get("capacity"), 0, sched_c)
        self.assertEqual(sched_c.get("waiting_reason"), "capacity", sched_c)
        self.assertEqual(slots_c, 0, f"C={sched_c} A={sched_a} B={sched_b}")
        for task in status_c.get("tasks") or []:
            if isinstance(task, dict):
                self.assertFalse(task.get("run_id"), status_c)
                self.assertNotEqual(task.get("status"), "running", status_c)
        with self.subTest(case="A and B display limits"):
            for label, view in (("A", sched_a), ("B", sched_b)):
                self.assertEqual(view.get("execution_limit"), 1, f"{label} {view}")
                self.assertEqual(view.get("parked_review_runs"), 0, f"{label} {view}")
                self.assertEqual(view.get("active_task_limit"), 1, f"{label} {view}")
        gate_b.write_text("open", encoding="utf-8")
        states = self._drive_terminal(app, [goal_a, goal_b, goal_c], timeout=25.0)
        for gid, label in ((goal_a, "A"), (goal_b, "B"), (goal_c, "C")):
            self.assertEqual(
                states[gid].get("state"),
                "completed",
                f"{label} state={states[gid].get('state')} failure={states[gid].get('failure_reason')} "
                f"agy={len(host._agy())} saved={host._saved(run_a)}",
            )
        self.assertEqual(len(host._agy()), 4, host._agy())
        self.assertEqual(slot_goals._peak_live(host.agy_span), 1, slot_goals._jsonl(host.agy_span))
        account = host._account()
        self.assertEqual(account.state, "available", account)
        self.assertIsNone(account.lease_id)

    def test_c1_summarize_status_copies_only_valid_limit_fields(self) -> None:
        """C1: asserts summarize_status copies execution_limit, parked_review_runs, and active_task_limit when they are non-bool ints >= 0, omits each when absent or invalid (bool, negative, str), and leaves the old 4 scheduler keys unchanged."""
        with self.subTest(case="valid ints are copied"):
            summary = HCR.summarize_status(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "goal-c1",
                    "scheduler": {
                        "running": None,
                        "queued_ready": "2",
                        "capacity": 1,
                        "waiting_reason": None,
                        "execution_limit": 2,
                        "parked_review_runs": 3,
                        "active_task_limit": 9,
                    },
                }
            )
            self.assertEqual(
                summary["scheduler"],
                {
                    "running": 0,
                    "queued_ready": 2,
                    "capacity": 1,
                    "waiting_reason": "",
                    "execution_limit": 2,
                    "parked_review_runs": 3,
                    "active_task_limit": 9,
                },
            )
        with self.subTest(case="absent fields stay omitted"):
            summary = HCR.summarize_status(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "goal-c1",
                    "scheduler": {
                        "running": 1,
                        "queued_ready": 1,
                        "capacity": 0,
                        "waiting_reason": "capacity",
                    },
                }
            )
            self.assertEqual(
                summary["scheduler"],
                {"running": 1, "queued_ready": 1, "capacity": 0, "waiting_reason": "capacity"},
            )
            self.assertNotIn("scheduler", HCR.summarize_status({"ok": True, "state": "running", "request_id": "g"}))
        with self.subTest(case="bool negative and str are omitted"):
            summary = HCR.summarize_status(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "goal-c1",
                    "scheduler": {
                        "running": 0,
                        "queued_ready": 1,
                        "capacity": 0,
                        "waiting_reason": "capacity",
                        "execution_limit": True,
                        "parked_review_runs": -1,
                        "active_task_limit": "2",
                    },
                }
            )
            self.assertEqual(
                summary["scheduler"],
                {"running": 0, "queued_ready": 1, "capacity": 0, "waiting_reason": "capacity"},
            )
        with self.subTest(case="one valid field is copied and invalid siblings are omitted"):
            summary = HCR.summarize_status(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "goal-c1",
                    "scheduler": {
                        "running": 0,
                        "queued_ready": 0,
                        "capacity": 1,
                        "waiting_reason": "",
                        "execution_limit": 0,
                        "parked_review_runs": False,
                    },
                }
            )
            self.assertEqual(
                summary["scheduler"],
                {
                    "running": 0,
                    "queued_ready": 0,
                    "capacity": 1,
                    "waiting_reason": "",
                    "execution_limit": 0,
                },
            )


if __name__ == "__main__":
    unittest.main()
