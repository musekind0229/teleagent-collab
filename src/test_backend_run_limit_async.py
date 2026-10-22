#!/usr/bin/env python3
"""Spec R: coordinator adds parked agy reviews; backend max_runs stays the pool cap.

T1–T3 call AppCoordinator._backend_run_limit on stub backends. T4 injects run
records into a real AntigravityCliExecutionBackend (no spawn). T5 reuses the
single-account CollabApplication harness. No real agy, grok, or codex binary.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import test_agy_single_account_slots as slot_goals  # noqa: E402
import test_agy_slot_units as slot_units  # noqa: E402
from framework.app_service import AppCoordinator  # noqa: E402

_MISSING = object()


def _run_limit(backend):
    """Call the real coordinator method. It only reads ``self.backend``."""
    return AppCoordinator._backend_run_limit(SimpleNamespace(backend=backend))


def _concurrency(max_runs=_MISSING, pending=_MISSING) -> dict:
    conc: dict = {}
    if max_runs is not _MISSING:
        conc["max_runs"] = max_runs
    if pending is not _MISSING:
        conc["pending_review_runs"] = pending
    return conc


class _ModeBackend:
    """Stub whose capabilities report a fixed concurrency block."""

    def __init__(self, concurrency: dict, mode):
        self._concurrency = dict(concurrency)
        self._mode = mode

    @property
    def lead_review_mode(self):
        return self._mode

    def capabilities(self) -> dict:
        return {"concurrency": dict(self._concurrency)}


class _NoModeBackend:
    """Same caps, but no lead_review_mode attribute at all."""

    def __init__(self, concurrency: dict):
        self._concurrency = dict(concurrency)

    def capabilities(self) -> dict:
        return {"concurrency": dict(self._concurrency)}


class _RaisingModeBackend:
    """lead_review_mode raises. The coordinator must treat that as not opted in."""

    def __init__(self, concurrency: dict):
        self._concurrency = dict(concurrency)

    @property
    def lead_review_mode(self):
        raise RuntimeError("lead_review_mode unavailable")

    def capabilities(self) -> dict:
        return {"concurrency": dict(self._concurrency)}


class BackendRunLimitAsyncTests(unittest.TestCase):
    def _bind(self, case_cls, method_name: str):
        """Run an existing TestCase setUp, with its cleanups on this test."""
        host = case_cls(method_name)
        host.addCleanup = self.addCleanup
        try:
            host.setUp()
        except Exception:
            self.addCleanup(host.tearDown)
            raise
        self.addCleanup(host.tearDown)
        return host

    def test_t1_async_v1_adds_pending_review_runs(self) -> None:
        """T1: asserts async_v1 limit is max_runs 1 plus pending 2 equals 3, and pending 0 or missing stays 1."""
        caps_zero = _concurrency(max_runs=1, pending=0)
        caps_missing = _concurrency(max_runs=1)
        caps_two = _concurrency(max_runs=1, pending=2)
        with self.subTest(case="pending 0"):
            got = _run_limit(_ModeBackend(caps_zero, "async_v1"))
            self.assertEqual(got, 1, f"pending 0 changed the base limit; got {got}")
        with self.subTest(case="pending missing"):
            got = _run_limit(_ModeBackend(caps_missing, "async_v1"))
            self.assertEqual(got, 1, f"missing pending_review_runs changed the base limit; got {got}")
        with self.subTest(case="pending 2"):
            got = _run_limit(_ModeBackend(caps_two, "async_v1"))
            self.assertEqual(
                got,
                3,
                f"async_v1 did not add pending_review_runs to max_runs; got {got}",
            )

    def test_t2_not_opted_in_ignores_pending_review_runs(self) -> None:
        """T2: asserts a backend that is not async_v1 keeps max_runs 1 when pending_review_runs is 2."""
        caps = _concurrency(max_runs=1, pending=2)
        cases = (
            ("no attribute", _NoModeBackend(caps)),
            ("empty string", _ModeBackend(caps, "")),
            ("sync", _ModeBackend(caps, "sync")),
            ("none", _ModeBackend(caps, None)),
            ("property raising", _RaisingModeBackend(caps)),
        )
        for name, backend in cases:
            with self.subTest(case=name):
                got = _run_limit(backend)
                self.assertEqual(
                    got,
                    1,
                    f"{name} opted in or enlarged the limit with pending_review_runs; got {got}",
                )

    def test_t3_abnormal_pending_does_not_enlarge_and_invalid_max_runs_is_none(self) -> None:
        """T3: asserts abnormal pending on async_v1 stays at max_runs 1, and missing or invalid max_runs is None."""
        abnormal = (-1, True, False, 1.5, "2", None, [], {})
        for pending in abnormal:
            with self.subTest(pending=pending):
                backend = _ModeBackend(_concurrency(max_runs=1, pending=pending), "async_v1")
                got = _run_limit(backend)
                self.assertEqual(
                    got,
                    1,
                    f"pending {pending!r} enlarged the async_v1 limit; got {got}",
                )
        invalid_max = (
            ("missing", _MISSING),
            ("none", None),
            ("zero", 0),
            ("negative", -1),
            ("true", True),
            ("string", "1"),
            ("float", 1.5),
            ("list", []),
            ("dict", {}),
        )
        for name, max_runs in invalid_max:
            with self.subTest(max_runs=name):
                conc = _concurrency(pending=5) if max_runs is _MISSING else _concurrency(max_runs=max_runs, pending=5)
                got = _run_limit(_ModeBackend(conc, "async_v1"))
                self.assertIsNone(
                    got,
                    f"invalid max_runs {name} became {got} after pending_review_runs",
                )

    @unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy is spawned through /bin/sh")
    def test_t4_max_runs_is_pool_limit_pending_is_parked_only(self) -> None:
        """T4: asserts max_runs stays the pool limit, pending counts one parked review, and a running rework or rework_waiting counts 0."""
        host = self._bind(
            slot_units.AgySlotCapabilityTests,
            "test_s1_pending_review_runs_is_zero_when_none",
        )
        pool = host._pool()
        be = host._backend(pool)
        pool_limit = be._account_pool_run_limit()
        self.assertGreaterEqual(pool_limit, 1, pool_limit)

        with self.subTest(case="no runs"):
            conc = be.capabilities()["concurrency"]
            self.assertEqual(conc.get("max_runs"), pool_limit, conc)
            self.assertEqual(conc.get("limited_by"), ["agy_account_pool"], conc)
            self.assertEqual(conc.get("pending_review_runs"), 0, conc)

        rec = {
            "run_id": "synthetic-parked",
            "review": {"state": "awaiting_review"},
            "proc": None,
        }
        be._runs[rec["run_id"]] = rec
        with self.subTest(case="parked awaiting_review"):
            conc = be.capabilities()["concurrency"]
            self.assertEqual(conc.get("pending_review_runs"), 1, conc)
            self.assertEqual(conc.get("limited_by"), ["agy_account_pool"], conc)
            self.assertEqual(
                conc.get("max_runs"),
                pool_limit,
                f"parked review was folded into max_runs; concurrency={conc} pool_limit={pool_limit}",
            )

        live = slot_units._LiveProc()
        host._fake_procs.append(live)
        rec["review"] = {"state": "running"}
        rec["proc"] = live
        with self.subTest(case="running rework"):
            conc = be.capabilities()["concurrency"]
            self.assertEqual(
                conc.get("pending_review_runs"),
                0,
                f"running rework was counted in pending_review_runs; concurrency={conc}",
            )
            self.assertEqual(
                conc.get("max_runs"),
                pool_limit,
                f"running rework changed max_runs; concurrency={conc} pool_limit={pool_limit}",
            )

        live.alive = False
        rec["review"] = {"state": "rework_waiting"}
        rec["proc"] = None
        with self.subTest(case="rework_waiting"):
            conc = be.capabilities()["concurrency"]
            self.assertEqual(
                conc.get("pending_review_runs"),
                0,
                f"rework_waiting was counted in pending_review_runs; concurrency={conc}",
            )
            self.assertEqual(conc.get("max_runs"), pool_limit, conc)

    @unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy and lead are spawned through /bin/sh")
    def test_t5_parked_review_opens_a_slot_then_rework_closes_it(self) -> None:
        """T5: asserts a parked review raises the coordinator limit so B can run, a rework drops it so a third goal is not dispatched, live agy procs stay at most 1 with one lease, and A spawns twice while B spawns once."""
        host = self._bind(
            slot_goals.AgySingleAccountSlotTests,
            "test_s4b_rework_waits_for_the_single_account",
        )
        gate_lead = host._gate("t5-lead")
        gate_b = host._gate("t5-agy")
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
            ]
        )
        host._lead_steps(
            [
                f"block_until_fail:{gate_lead}|needs work",
                "pass:round two",
            ]
        )
        app = host._build()
        goal_a = host._submit("t5-a", wall=180, max_reworks=1)
        status_a = self._drive(host, app, goal_a, host._review_running, timeout=12.0)
        self._sample(host, "A awaiting review")
        self.assertTrue(
            host._review_running(status_a),
            f"goal A never entered lead review; state={status_a.get('state')} "
            f"pending={status_a.get('pending_decisions')}",
        )
        self.assertGreaterEqual(len(host._wait_agy(1)), 1, host._agy())
        run_a = host._run_id(status_a)
        saved = host._saved(run_a)
        self.assertEqual(
            (saved or {}).get("state"),
            "awaiting_review",
            f"backend review was not parked; saved={saved}",
        )
        coord = app.coordinator
        parked_limit = coord._backend_run_limit()
        parked_conc = host.backend.capabilities()["concurrency"]
        self.assertEqual(
            parked_limit,
            2,
            f"awaiting_review coordinator limit={parked_limit} concurrency={parked_conc}",
        )
        goal_b = host._submit("t5-b", prose=False, artifacts=["side.md"], max_reworks=0, wall=180)
        slots_b = coord._slots_for(goal_b)
        self.assertGreaterEqual(
            slots_b,
            1,
            f"other goal has no slot while A is awaiting review; slots={slots_b} limit={parked_limit}",
        )
        self._sample(host, "before B starts")

        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and len(host._agy()) < 2:
            slot_goals._process_all(app)
            self._sample(host, "B starting")
            time.sleep(0.05)
        rows = host._agy()
        self.assertEqual(len(rows), 2, rows)
        ws_a = str(Path(rows[0]["cwd"]).resolve())
        ws_b = str(Path(rows[1]["cwd"]).resolve())
        self.assertNotEqual(ws_a, ws_b, rows)
        b_pid = int(rows[1]["pid"])
        self.assertTrue(slot_goals._pid_alive(b_pid), b_pid)
        self._sample(host, "B holding the account")

        gate_lead.write_text("open", encoding="utf-8")
        saved = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            slot_goals._process_all(app)
            self._sample(host, "A review failing")
            saved = host._saved(run_a)
            state = str((saved or {}).get("state") or "")
            redos = int((saved or {}).get("redos") or 0)
            if state == "rework_waiting" or (state == "running" and redos >= 1):
                break
            if state in {"review_unavailable", "rejected_final"}:
                break
            time.sleep(0.05)
        self.assertIsNotNone(saved, "no review record after the lead fail")
        self.assertIn(
            (saved or {}).get("state"),
            {"rework_waiting", "running"},
            "A did not enter a rework after the lead fail: "
            f"state={(saved or {}).get('state')} unavailable={(saved or {}).get('unavailable_reason')}",
        )
        self._sample(host, "A rework")
        rework_limit = coord._backend_run_limit()
        rework_conc = host.backend.capabilities()["concurrency"]
        self.assertEqual(
            rework_limit,
            1,
            f"rework coordinator limit={rework_limit} concurrency={rework_conc} saved={saved}",
        )
        self.assertTrue(slot_goals._pid_alive(b_pid), "B exited before the third-goal window")

        agy_before = len(host._agy())
        goal_c = host._submit("t5-c", prose=False, artifacts=["extra.md"], max_reworks=0, wall=180)
        self.assertEqual(
            coord._slots_for(goal_c),
            0,
            f"third goal has a slot while the rework limit is {coord._backend_run_limit()}",
        )
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            slot_goals._process_all(app)
            self._sample(host, "third goal")
            time.sleep(0.05)
        self.assertEqual(len(host._agy()), agy_before, host._agy())
        self.assertTrue(slot_goals._pid_alive(b_pid), b_pid)
        status_c = app.status(goal_c)
        for task in status_c.get("tasks") or []:
            if isinstance(task, dict):
                self.assertFalse(task.get("run_id"), status_c)
                self.assertNotEqual(task.get("status"), "running", status_c)
        self._sample(host, "third goal not dispatched")
        if str(status_c.get("state") or "") not in slot_goals._TERMINAL:
            app.cancel(goal_c, "third goal must not run")
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                slot_goals._process_all(app)
                self._sample(host, "cancel third goal")
                status_c = app.status(goal_c)
                if str(status_c.get("state") or "") in slot_goals._TERMINAL:
                    break
                time.sleep(0.05)
        self.assertEqual(len(host._agy()), agy_before, host._agy())

        gate_b.write_text("open", encoding="utf-8")
        status_b = self._drive(host, app, goal_b, lambda status: False, timeout=12.0)
        status_a = self._drive(host, app, goal_a, lambda status: False, timeout=15.0)
        self._sample(host, "A and B finished")
        self.assertEqual(status_b["state"], "completed", status_b)
        self.assertEqual(status_a["state"], "completed", status_a)
        a_rows = host._for_cwd(ws_a)
        b_rows = host._for_cwd(ws_b)
        self.assertEqual(len(a_rows), 2, host._agy())
        self.assertEqual(sum(1 for row in a_rows if row.get("rework")), 1, a_rows)
        self.assertEqual(len(b_rows), 1, host._agy())
        self.assertFalse(any(row.get("rework") for row in b_rows), b_rows)
        self.assertLessEqual(slot_goals._peak_live(host.agy_span), 1, slot_goals._jsonl(host.agy_span))
        account = host._account()
        self.assertEqual(account.state, "available", account)
        self.assertIsNone(account.lease_id)

    def _drive(self, host, app, goal_id: str, pred, timeout: float):
        deadline = time.monotonic() + timeout
        last = app.status(goal_id)
        while time.monotonic() < deadline:
            slot_goals._process_all(app)
            self._sample(host, f"drive {goal_id}")
            last = app.status(goal_id)
            if pred(last):
                return last
            if str(last.get("state") or "") in slot_goals._TERMINAL:
                return last
            time.sleep(0.05)
        return last

    def _sample(self, host, moment: str) -> None:
        live = 0
        for row in host._agy():
            if slot_goals._pid_alive(row.get("pid")):
                live += 1
        account = host._account()
        leases = 0 if not account.lease_id else 1
        self.assertLessEqual(live, 1, f"{moment}: live agy procs={live}")
        self.assertLessEqual(
            leases,
            1,
            f"{moment}: account leases={leases} state={account.state} lease_id={account.lease_id}",
        )


if __name__ == "__main__":
    unittest.main()
