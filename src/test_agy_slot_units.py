#!/usr/bin/env python3
"""Unit tests for Spec S: pending_review_runs, rework wait, event caps.

S1 and S2 drive AntigravityCliExecutionBackend with the fake agy shim and a
one-account pool. S3 calls LeadReviewRunner._append_event in process.
No real agy, grok, or codex binary is invoked.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.agy_account_pool import load_pool  # noqa: E402
from execution_backend.agy_review_store import AgyReviewStore  # noqa: E402
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402
from framework.lead_review_runner import LeadReviewRunner, _sanitize  # noqa: E402

FAKE_AGY = SRC / "testdata" / "fake_agy_review.py"
ACCEPTANCE = "Prose acceptance: a lead must judge whether the report explains the change."
BODY = "hello review\n"
REASON = "redo the report"
ACCOUNT = "only"
_SKIP_NO_ACCOUNT = "rework skipped: no free agy account before deadline"


def _saved(path: Path, run_id: str) -> dict:
    rec = AgyReviewStore(path).get(run_id)
    if rec is None:
        raise AssertionError(f"no review record for {run_id} in {path}")
    return rec


def _write_shim(directory: Path, script: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "agy"
    shim.write_text(
        "#!/bin/sh\n" + f'exec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _LiveProc:
    """poll() is None while ``alive``. Not a real process."""

    def __init__(self) -> None:
        self.alive = True
        self.pid = 1 << 30

    def poll(self):
        return None if self.alive else 0


@unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy is spawned through /bin/sh")
class AgySlotCapabilityTests(unittest.TestCase):
    """S1: pending_review_runs (max_runs formula not pinned: pending coordinator slot decision)."""

    def setUp(self) -> None:
        self._backends: list[AntigravityCliExecutionBackend] = []
        self._fake_procs: list[_LiveProc] = []
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-slot-s1-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.registry = self.root / "persist" / "agy-runs.json"
        self.registry.parent.mkdir(parents=True)
        self.store = self.root / "explicit" / "agy-reviews.json"
        self.store.parent.mkdir()
        self.log = self.root / "fake-agy.jsonl"
        self.script = self.root / "agy-steps.json"
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.shim = _write_shim(self.root / "bin", FAKE_AGY)
        self.assertTrue(FAKE_AGY.is_file(), FAKE_AGY)

    def tearDown(self) -> None:
        for proc in self._fake_procs:
            proc.alive = False
        for be in self._backends:
            try:
                be.close()
            except Exception:
                pass

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "AGY_BIN",
            "COLLAB_AGY_ACCOUNT_POOL",
            "COLLAB_AGY_REVIEW_STORE",
            "COLLAB_AGY_POOL_PRECHECK",
            "AGY_AUTO_APPROVE",
            "COLLAB_AGY_AUTO_APPROVE",
            "COLLAB_AGY_RUN_REGISTRY",
        ):
            env.pop(key, None)
        env["FAKE_AGY_SCRIPT"] = str(self.script)
        env["FAKE_AGY_LOG"] = str(self.log)
        return env

    def _pool(self) -> Path:
        home = self.root / "home-only"
        home.mkdir()
        path = self.root / "pool.json"
        path.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": ACCOUNT,
                            "home": str(home),
                            "state": "available",
                            "email_mask": "o***@example.com",
                            "notes": "single test account",
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _backend(self, pool_path: Path | None = None) -> AntigravityCliExecutionBackend:
        kwargs = {
            "bin_path": str(self.shim),
            "environ": self._env(),
            "poll_sec": 0.05,
            "timeout_sec": 60,
            "run_registry_path": str(self.registry),
            "review_store_path": str(self.store),
        }
        if pool_path is not None:
            kwargs["account_pool_path"] = str(pool_path)
        be = AntigravityCliExecutionBackend(**kwargs)
        self._backends.append(be)
        return be

    def _steps(self, steps: list) -> None:
        self.script.write_text(json.dumps(steps), encoding="utf-8")

    def _start(self, be: AntigravityCliExecutionBackend) -> dict:
        started = be.start_run(
            title="lead-review",
            directory=str(self.ws),
            instruction="Write report.md",
            artifacts=["report.md"],
            charter={
                "acceptance": ACCEPTANCE,
                "max_redos": 1,
                "timeout_sec": 180,
                "goal": "Write the report",
            },
        )
        self.assertTrue(started.get("ok"), started)
        return started

    def _wait_pending(self, be, run_id: str, timeout: float = 8.0) -> list:
        deadline = time.monotonic() + timeout
        last_code = None
        last_rows = None
        while time.monotonic() < deadline:
            last_code, last_rows = be.list_pending_actions(session_id=run_id)
            if last_code == 200 and last_rows:
                return last_rows
            time.sleep(0.05)
        self.fail(f"no review action within {timeout}s code={last_code} rows={last_rows}")

    def test_s1_pending_review_runs_is_zero_when_none(self) -> None:
        """S1: asserts pending_review_runs is 0 with no runs, and max_runs/limited_by stay the pool cap."""
        pool = self._pool()
        be = self._backend(pool)
        conc = be.capabilities()["concurrency"]
        self.assertEqual(conc.get("limited_by"), ["agy_account_pool"], conc)
        self.assertEqual(conc.get("max_runs"), 1, conc)
        self.assertEqual(
            conc.get("pending_review_runs"),
            0,
            f"capabilities concurrency missing pending_review_runs: {conc}",
        )

    def test_s1_pending_review_runs_counts_parked_only(self) -> None:
        """S1: asserts pending_review_runs counts awaiting_review with no live proc, not a live proc or rework_waiting. max_runs is not asserted here (its semantics await the coordinator slot decision)."""
        pool = self._pool()
        be = self._backend(pool)
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = self._start(be)
        run_id = started["run_id"]
        self._wait_pending(be, run_id)
        conc = be.capabilities()["concurrency"]
        self.assertEqual(conc.get("limited_by"), ["agy_account_pool"], conc)
        self.assertEqual(
            conc.get("pending_review_runs"),
            1,
            f"parked awaiting_review was not counted: {conc}",
        )
        live = _LiveProc()
        self._fake_procs.append(live)
        be._runs["synthetic-live"] = {
            "run_id": "synthetic-live",
            "review": {"state": "awaiting_review"},
            "proc": live,
        }
        be._runs["synthetic-waiting"] = {
            "run_id": "synthetic-waiting",
            "review": {"state": "rework_waiting"},
            "proc": None,
        }
        conc = be.capabilities()["concurrency"]
        self.assertEqual(
            conc.get("pending_review_runs"),
            1,
            f"rework_waiting or a live proc was counted: {conc}",
        )


@unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy is spawned through /bin/sh")
class AgyReworkWaitTests(unittest.TestCase):
    """S2: a rework that cannot get the only account waits instead of failing."""

    def setUp(self) -> None:
        self._backends: list[AntigravityCliExecutionBackend] = []
        self._fake_procs: list[_LiveProc] = []
        self._gates: list[Path] = []
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-slot-s2-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.registry = self.root / "persist" / "agy-runs.json"
        self.registry.parent.mkdir(parents=True)
        self.store = self.root / "explicit" / "agy-reviews.json"
        self.store.parent.mkdir()
        self.log = self.root / "fake-agy.jsonl"
        self.script = self.root / "agy-steps.json"
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.hold_ws = self.root / "hold"
        self.hold_ws.mkdir()
        self.shim = _write_shim(self.root / "bin", FAKE_AGY)
        self.assertTrue(FAKE_AGY.is_file(), FAKE_AGY)

    def tearDown(self) -> None:
        for proc in self._fake_procs:
            proc.alive = False
        for gate in self._gates:
            try:
                gate.write_text("open", encoding="utf-8")
            except OSError:
                pass
        for be in self._backends:
            try:
                be.close()
            except Exception:
                pass
        me = os.getpid()
        if self.log.is_file():
            for line in self.log.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    pid = json.loads(line).get("pid")
                except json.JSONDecodeError:
                    continue
                if isinstance(pid, int) and pid > 1 and pid != me and _pid_alive(pid):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "AGY_BIN",
            "COLLAB_AGY_ACCOUNT_POOL",
            "COLLAB_AGY_REVIEW_STORE",
            "COLLAB_AGY_POOL_PRECHECK",
            "AGY_AUTO_APPROVE",
            "COLLAB_AGY_AUTO_APPROVE",
            "COLLAB_AGY_RUN_REGISTRY",
        ):
            env.pop(key, None)
        env["FAKE_AGY_SCRIPT"] = str(self.script)
        env["FAKE_AGY_LOG"] = str(self.log)
        return env

    def _pool(self) -> Path:
        home = self.root / "home-only"
        home.mkdir()
        path = self.root / "pool.json"
        path.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": ACCOUNT,
                            "home": str(home),
                            "state": "available",
                            "email_mask": "o***@example.com",
                            "notes": "single test account",
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _backend(self, pool_path: Path | None) -> AntigravityCliExecutionBackend:
        kwargs = {
            "bin_path": str(self.shim),
            "environ": self._env(),
            "poll_sec": 0.05,
            "timeout_sec": 60,
            "run_registry_path": str(self.registry),
            "review_store_path": str(self.store),
        }
        if pool_path is not None:
            kwargs["account_pool_path"] = str(pool_path)
        be = AntigravityCliExecutionBackend(**kwargs)
        self._backends.append(be)
        return be

    def _steps(self, steps: list) -> None:
        self.script.write_text(json.dumps(steps), encoding="utf-8")

    def _log_rows(self) -> list[dict]:
        if not self.log.is_file():
            return []
        rows = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _pool_states(self, pool_path: Path) -> dict[str, str]:
        pool = load_pool(pool_path)
        return {acc.id: acc.state for acc in pool.accounts}

    def _lease_id(self, pool_path: Path) -> str | None:
        pool = load_pool(pool_path)
        return pool.accounts[0].lease_id

    def _gate(self, name: str) -> Path:
        path = self.root / name
        self._gates.append(path)
        return path

    def _wait_pending(self, be, run_id: str, timeout: float = 8.0) -> list:
        deadline = time.monotonic() + timeout
        last_code = None
        last_rows = None
        while time.monotonic() < deadline:
            last_code, last_rows = be.list_pending_actions(session_id=run_id)
            if last_code == 200 and last_rows:
                return last_rows
            time.sleep(0.05)
        self.fail(f"no review action within {timeout}s code={last_code} rows={last_rows}")

    def _wait_log(self, n: int, timeout: float = 8.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        rows: list[dict] = []
        while time.monotonic() < deadline:
            rows = self._log_rows()
            if len(rows) >= n:
                return rows
            time.sleep(0.05)
        self.fail(f"fake agy log has {len(rows)} lines, want {n}: {rows}")

    def _start_review(self, be) -> str:
        started = be.start_run(
            title="lead-review",
            directory=str(self.ws),
            instruction="Write report.md",
            artifacts=["report.md"],
            charter={
                "acceptance": ACCEPTANCE,
                "max_redos": 1,
                "timeout_sec": 180,
                "goal": "Write the report",
            },
        )
        self.assertTrue(started.get("ok"), started)
        self.assertEqual(started.get("agy_profile"), ACCOUNT, started)
        return str(started["run_id"])

    def _start_holder(self, be, gate: Path) -> str:
        started = be.start_run(
            title="holder",
            directory=str(self.hold_ws),
            instruction="Hold the account until the gate opens.",
            artifacts=[],
            charter={"goal": "Hold the account.", "timeout_sec": 180},
        )
        self.assertTrue(started.get("ok"), started)
        self.assertEqual(started.get("agy_profile"), ACCOUNT, started)
        return str(started["run_id"])

    def _hold_the_account(self):
        """Review run A is parked; holder B owns the only account. Harness only."""
        pool = self._pool()
        be = self._backend(pool)
        be.enable_lead_review()
        gate = self._gate("hold-gate")
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "block_until", "path": str(gate), "sec": 25, "write": {"hold.txt": "held\n"}},
                {"do": "sleep", "sec": 1.0, "write": {"report.md": BODY}},
            ]
        )
        run_a = self._start_review(be)
        pending = self._wait_pending(be, run_a)
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "available"})
        self.assertEqual(len(self._log_rows()), 1, self._log_rows())
        run_b = self._start_holder(be, gate)
        self._wait_log(2)
        holder = be._runs[run_b].get("proc")
        self.assertIsNotNone(holder, "holder did not spawn")
        self.assertIsNone(holder.poll(), "holder exited before the review fail")
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "busy"})
        lease = self._lease_id(pool)
        self.assertTrue(lease, "holder lease id missing")
        resolved = be.resolve_decision(
            pending[0]["request_id"],
            verdict="fail",
            reason=REASON,
            answers=[{"by": "lead"}],
        )
        self.assertEqual(
            self._lease_id(pool),
            lease,
            "resolving the fail took or dropped the holder's lease",
        )
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "busy"})
        saved = _saved(self.store, run_a)
        return be, pool, run_a, run_b, gate, lease, resolved, saved

    def _assert_waiting(self, be, run_a: str, saved: dict) -> None:
        self.assertEqual(
            saved.get("state"),
            "rework_waiting",
            "rework did not wait for a free account: "
            f"state={saved.get('state')} unavailable={saved.get('unavailable_reason')}",
        )
        self.assertEqual(saved.get("redos"), 1, saved)
        self.assertIsInstance(saved.get("waiting_since"), (int, float), saved)
        self.assertEqual(saved.get("pending_rework"), {"reason": REASON, "previous": []}, saved)
        self.assertNotIn("rework_spawn_failed", json.dumps(saved))
        rec = be._runs[run_a]
        self.assertEqual(rec.get("state"), "running", rec.get("state"))
        self.assertEqual(rec.get("activity"), "waiting_account", rec.get("activity"))
        proc = rec.get("proc")
        alive = proc is not None and callable(getattr(proc, "poll", None)) and proc.poll() is None
        self.assertFalse(alive, "rework_waiting must not hold a live process")

    def _release_holder(self, be, run_b: str, gate: Path, pool: Path) -> None:
        gate.write_text("open", encoding="utf-8")
        proc = be._runs[run_b].get("proc")
        self.assertIsNotNone(proc, "holder has no process")
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(proc.poll(), "holder did not exit after the gate opened")
        out = be.collect_result(run_b)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "available"})
        self.assertIsNone(self._lease_id(pool))

    def test_s2_other_spawn_error_is_still_unavailable(self) -> None:
        """S2: asserts a rework spawn error other than no free account stays review_unavailable with rework_spawn_failed."""
        pool = self._pool()
        be = self._backend(pool)
        be.enable_lead_review()
        self._steps(
            [
                {"do": "ok", "write": {"report.md": BODY}},
                {"do": "ok", "write": {"report.md": BODY}},
            ]
        )
        run_a = self._start_review(be)
        pending = self._wait_pending(be, run_a)
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "available"})
        be.bin_path = str(self.root / "missing-agy")
        resolved = be.resolve_decision(
            pending[0]["request_id"],
            verdict="fail",
            reason=REASON,
            answers=[{"by": "lead"}],
        )
        saved = _saved(self.store, run_a)
        self.assertEqual(resolved.get("controller_state"), "review_unavailable", resolved)
        self.assertEqual(saved.get("state"), "review_unavailable", saved)
        self.assertIn("rework_spawn_failed", str(saved.get("unavailable_reason") or ""), saved)
        self.assertEqual(saved.get("error_source"), "spawn", saved)
        self.assertNotEqual(saved.get("state"), "rework_waiting", saved)
        self.assertEqual(len(self._log_rows()), 1, self._log_rows())
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "available"})

    def test_s2_busy_account_waits_and_retries_once(self) -> None:
        """S2: asserts a fail while the only account is busy enters rework_waiting, stays there across observe, then spawns exactly one rework when the account is free."""
        be, pool, run_a, run_b, gate, _lease, resolved, saved = self._hold_the_account()
        self.assertEqual(resolved.get("controller_state"), "rework_waiting", resolved)
        self._assert_waiting(be, run_a, saved)
        before = len(self._log_rows())
        obs = be.observe_run(run_a)
        self.assertTrue(obs.get("busy"), obs)
        self.assertFalse(obs.get("errored"), obs)
        self.assertFalse(obs.get("cancelled"), obs)
        self.assertEqual(obs.get("activity"), "waiting_account", obs)
        code, rows = be.list_pending_actions(session_id=run_a)
        self.assertEqual(code, 200)
        self.assertEqual(rows, [])
        self.assertEqual(len(self._log_rows()), before)
        self.assertEqual(_saved(self.store, run_a).get("state"), "rework_waiting")
        self._release_holder(be, run_b, gate, pool)
        be.observe_run(run_a)
        self._wait_log(3)
        logged = self._log_rows()
        self.assertEqual(len(logged), 3, logged)
        self.assertEqual([row.get("rework") for row in logged], [False, False, True], logged)
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "busy"})
        pending = self._wait_pending(be, run_a)
        self.assertTrue(str(pending[0].get("request_id") or "").endswith(":r2"), pending)
        again = _saved(self.store, run_a)
        self.assertEqual(again.get("redos"), 1, again)
        self.assertEqual(again.get("round"), 2, again)
        self.assertEqual(again.get("state"), "awaiting_review", again)
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "available"})
        self.assertIsNone(self._lease_id(pool))

    def test_s2_deadline_rejects_while_waiting(self) -> None:
        """S2: asserts observe while rework_waiting with under 30s left becomes rejected_final and does not spawn."""
        be, _pool, run_a, _run_b, _gate, _lease, _resolved, saved = self._hold_the_account()
        self._assert_waiting(be, run_a, saved)
        deadline = time.time() + 10
        rec = be._runs[run_a]
        rec["review"]["deadline"] = deadline
        be._review._store.update(run_a, deadline=deadline)
        before = len(self._log_rows())
        be.observe_run(run_a)
        after = _saved(self.store, run_a)
        self.assertEqual(after.get("state"), "rejected_final", after)
        self.assertEqual(after.get("skip_reason"), _SKIP_NO_ACCOUNT, after)
        self.assertEqual(len(self._log_rows()), before)
        self.assertEqual(be.list_pending_actions(session_id=run_a)[1], [])

    def test_s2_cancel_while_waiting_does_not_release_a_lease(self) -> None:
        """S2: asserts cancel of rework_waiting is cancelled, the store forgets the run, and no lease release runs."""
        import execution_backend.antigravity_cli_v1 as agy_mod

        be, pool, run_a, _run_b, _gate, lease, _resolved, saved = self._hold_the_account()
        self._assert_waiting(be, run_a, saved)
        calls: list[bool] = []
        original = agy_mod.finish_account_lease

        def wrapped(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        agy_mod.finish_account_lease = wrapped
        try:
            code, body = be.cancel(run_a)
        finally:
            agy_mod.finish_account_lease = original
        self.assertEqual(code, 200, body)
        self.assertEqual(body.get("state"), "cancelled", body)
        self.assertEqual(calls, [], "cancel of rework_waiting released a lease")
        self.assertIsNone(AgyReviewStore(self.store).get(run_a))
        self.assertEqual(self._lease_id(pool), lease)
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "busy"})

    def test_s2_restart_restores_waiting_and_retries_on_observe(self) -> None:
        """S2: asserts a stored rework_waiting run is restored with no proc and the next observe_run spawns the rework once."""
        be, pool, run_a, _run_b, _gate, _lease, _resolved, saved = self._hold_the_account()
        self._assert_waiting(be, run_a, saved)
        be.close()
        self.assertEqual(self._pool_states(pool), {ACCOUNT: "available"})
        be2 = self._backend(pool)
        be2.enable_lead_review()
        restored = _saved(self.store, run_a)
        self.assertEqual(
            restored.get("state"),
            "rework_waiting",
            f"restart did not keep rework_waiting: {restored}",
        )
        self.assertIn(run_a, be2._runs, "rework_waiting record was not restored")
        rec = be2._runs[run_a]
        self.assertIsNone(rec.get("proc"))
        self.assertEqual(len(self._log_rows()), 2, self._log_rows())
        be2.observe_run(run_a)
        self._wait_log(3)
        self.assertEqual(len(self._log_rows()), 3, self._log_rows())
        self.assertTrue(self._log_rows()[-1].get("rework"), self._log_rows())
        be2.observe_run(run_a)
        self.assertEqual(len(self._log_rows()), 3, self._log_rows())

    def test_s2_retry_while_proc_is_live_is_noop(self) -> None:
        """S2: asserts retry_waiting while a proc is live does not spawn a second process."""
        be = self._backend(None)
        be.enable_lead_review()
        self._steps([{"do": "ok", "write": {"report.md": BODY}}])
        started = be.start_run(
            title="lead-review",
            directory=str(self.ws),
            instruction="Write report.md",
            artifacts=["report.md"],
            charter={
                "acceptance": ACCEPTANCE,
                "max_redos": 1,
                "timeout_sec": 180,
                "goal": "Write the report",
            },
        )
        self.assertTrue(started.get("ok"), started)
        run_id = str(started["run_id"])
        self._wait_pending(be, run_id)
        rec = be._runs[run_id]
        review = rec["review"]
        review["state"] = "rework_waiting"
        review["pending_rework"] = {"reason": REASON, "previous": []}
        review["redos"] = 1
        review["round"] = 2
        live = _LiveProc()
        self._fake_procs.append(live)
        previous = rec.get("proc")
        rec["proc"] = live
        try:
            retry = getattr(be._review, "retry_waiting", None)
            self.assertTrue(callable(retry), "AgyReviewController.retry_waiting is missing")
            before = len(self._log_rows())
            retry(rec)
            self.assertEqual(len(self._log_rows()), before, self._log_rows())
            self.assertIs(rec.get("proc"), live)
            self.assertEqual(review.get("state"), "rework_waiting", review.get("state"))
            be.observe_run(run_id)
            be.list_pending_actions(session_id=run_id)
            self.assertEqual(len(self._log_rows()), before, self._log_rows())
        finally:
            live.alive = False
            rec["proc"] = previous


class LeadReviewEventCapTests(unittest.TestCase):
    """S3: details.lead_review.events count, field, byte, and redaction caps."""

    def _runner(self) -> LeadReviewRunner:
        return LeadReviewRunner(layer=object(), planner=object(), backend=object())

    def test_s3_events_keep_the_newest_10(self) -> None:
        """S3: asserts _append_event keeps only the newest 10 events."""
        runner = self._runner()
        lead: dict = {}
        for index in range(12):
            runner._append_event(lead, "lead_review_stopped", job_id=f"job-{index:02d}")
        events = lead.get("events") or []
        self.assertEqual(len(events), 10, events)
        self.assertEqual(events[0].get("job_id"), "job-02", events)
        self.assertEqual(events[-1].get("job_id"), "job-11", events)

    def test_s3_string_fields_are_capped_at_300(self) -> None:
        """S3: asserts every string field on an event is sanitized and capped at 300 characters."""
        import framework.lead_review_runner as runner_mod

        runner = self._runner()
        lead: dict = {}
        original = "x" * 500
        runner._append_event(lead, "lead_review_stale_result", error=original)
        stored = lead["events"][-1]["error"]
        self.assertIsInstance(stored, str)
        self.assertLessEqual(len(stored), 300, stored)
        self.assertEqual(stored, _sanitize(original, 300))
        self.assertEqual(getattr(runner_mod, "_EVENT_FIELD_CAP", None), 300)

    def test_s3_token_and_bearer_values_are_redacted(self) -> None:
        """S3: asserts a token= value and a Bearer value in an event field are redacted."""
        runner = self._runner()
        lead: dict = {}
        secret = "token=supersecretvalue Bearer abcdefghijklmnop"
        runner._append_event(lead, "lead_review_stale_result", error=secret)
        stored = str(lead["events"][-1]["error"])
        self.assertNotIn("supersecretvalue", stored, stored)
        self.assertNotIn("abcdefghijklmnop", stored, stored)
        self.assertIn("[redacted]", stored, stored)
        self.assertEqual(stored, _sanitize(secret, 300))

    def test_s3_non_scalar_fields_are_sanitized_strings(self) -> None:
        """S3: asserts a non-scalar event field becomes its sanitized string, capped at 300 characters."""
        runner = self._runner()
        lead: dict = {}
        payload = {"note": "token=sekret Bearer zy9876543210 " + ("p" * 400)}
        runner._append_event(lead, "lead_review_stopped", lead_processes=payload)
        stored = lead["events"][-1]["lead_processes"]
        self.assertIsInstance(stored, str, stored)
        self.assertNotIn("sekret", stored, stored)
        self.assertNotIn("zy9876543210", stored, stored)
        self.assertLessEqual(len(stored), 300, stored)
        self.assertEqual(stored, _sanitize(str(payload), 300))

    def test_s3_events_drop_oldest_to_fit_byte_cap(self) -> None:
        """S3: asserts events json stays within 4096 bytes by dropping the oldest, and the newest event is kept."""
        import framework.lead_review_runner as runner_mod

        runner = self._runner()
        lead: dict = {}
        blob = "m" * 300
        for index in range(10):
            runner._append_event(
                lead,
                "lead_review_stale_result",
                job_id=f"job-{index:02d}",
                request_id="agyrev:agy_0123456789ab:r1",
                error=blob,
                detail=blob,
                extra=blob,
            )
        events = lead.get("events") or []
        raw = json.dumps(events)
        self.assertLessEqual(len(raw), 4096, f"events json is {len(raw)} bytes")
        self.assertTrue(events, lead)
        self.assertEqual(events[-1].get("job_id"), "job-09", events)
        ids = [row.get("job_id") for row in events]
        self.assertEqual(ids, [f"job-{index:02d}" for index in range(10 - len(ids), 10)], events)
        self.assertLess(len(events), 10, events)
        self.assertEqual(getattr(runner_mod, "_EVENTS_MAX_BYTES", None), 4096)


if __name__ == "__main__":
    unittest.main()
