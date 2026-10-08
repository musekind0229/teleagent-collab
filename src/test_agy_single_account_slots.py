#!/usr/bin/env python3
"""Spec S4: one agy account, two Goals. Coordinator end-to-end with the fakes.

(a) A parked in lead review does not stop B from running. At most one live agy.
(b) A's rework waits while B holds the account, then spawns once after B exits.
No real agy, grok, or codex binary is invoked.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_TESTS = str(SRC.parent / "tests")
if _TESTS not in sys.path:
    sys.path.append(_TESTS)

from desktop_lock_isolation import install_desktop_lock_isolation  # noqa: E402
from execution_backend.agy_account_pool import load_pool  # noqa: E402
from execution_backend.agy_review_store import AgyReviewStore  # noqa: E402
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402
from framework.app_service import CollabApplication, LeadAdapterPlanner  # noqa: E402
from test_app_agy_lead_review_loop import (  # noqa: E402
    FAKE_AGY,
    FAKE_LEAD,
    PROSE,
    _DROP,
    _TERMINAL,
    _jsonl,
    _pid_alive,
    _process_all,
    _write_shim,
)

ACCOUNT = "only"


def _peak_live(path: Path) -> int:
    """Max overlapping fake-agy processes from FAKE_AGY_SPAN start/end events."""
    events = []
    for row in _jsonl(path):
        kind = row.get("event")
        if kind not in {"start", "end"}:
            continue
        try:
            stamp = float(row.get("t"))
        except (TypeError, ValueError):
            continue
        events.append((stamp, 0 if kind == "end" else 1, kind))
    events.sort()
    live = 0
    peak = 0
    for _stamp, _order, kind in events:
        if kind == "start":
            live += 1
            if live > peak:
                peak = live
        else:
            live = max(0, live - 1)
    return peak


@unittest.skipIf(os.name == "nt", "POSIX shell shim; fake agy and lead are spawned through /bin/sh")
class AgySingleAccountSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        install_desktop_lock_isolation(self)
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-slot-s4-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.persist = self.root / "persist"
        self.persist.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.registry = self.persist / "agy-runs.json"
        self.store_path = self.persist / "agy-reviews.json"
        self.agy_script = self.root / "agy-steps.json"
        self.lead_script = self.root / "lead-steps.json"
        self.agy_log = self.root / "agy.jsonl"
        self.lead_log = self.root / "lead.jsonl"
        self.agy_span = self.root / "agy-span.jsonl"
        self.pool_path = self._write_pool()
        self.assertTrue(FAKE_AGY.is_file(), FAKE_AGY)
        self.assertTrue(FAKE_LEAD.is_file(), FAKE_LEAD)
        self.agy_shim = _write_shim(self.root / "bin", FAKE_AGY, "agy")
        self.lead_shim = _write_shim(self.root / "bin", FAKE_LEAD, "lead")
        self.lead_timeout = 15.0
        self._apps: list[CollabApplication] = []
        self._backends: list[AntigravityCliExecutionBackend] = []
        self._gates: list[Path] = []
        for key in _DROP + ("FAKE_LEAD_SHAPE", "FAKE_AGY_SPAN"):
            os.environ.pop(key, None)
        os.environ["FAKE_AGY_SCRIPT"] = str(self.agy_script)
        os.environ["FAKE_AGY_LOG"] = str(self.agy_log)
        os.environ["FAKE_AGY_SPAN"] = str(self.agy_span)
        os.environ["FAKE_LEAD_SCRIPT"] = str(self.lead_script)
        os.environ["FAKE_LEAD_LOG"] = str(self.lead_log)
        self.app: CollabApplication | None = None
        self.backend: AntigravityCliExecutionBackend | None = None

    def tearDown(self) -> None:
        for gate in self._gates:
            try:
                gate.parent.mkdir(parents=True, exist_ok=True)
                gate.write_text("open", encoding="utf-8")
            except OSError:
                pass
        for app in self._apps:
            try:
                app.coordinator.stop_all_planning("test cleanup")
            except Exception:
                pass
        for backend in self._backends:
            try:
                backend.close()
            except Exception:
                pass
        me = os.getpid()
        for path in (self.agy_log, self.lead_log):
            for row in _jsonl(path):
                pid = row.get("pid")
                if isinstance(pid, int) and pid > 1 and pid != me and _pid_alive(pid):
                    try:
                        os.killpg(pid, 9)
                    except OSError:
                        try:
                            os.kill(pid, 9)
                        except OSError:
                            pass

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _write_pool(self) -> Path:
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

    def _gate(self, name: str) -> Path:
        path = self.root / name
        self._gates.append(path)
        return path

    def _agy_steps(self, steps: list) -> None:
        self.agy_script.write_text(json.dumps(steps), encoding="utf-8")

    def _lead_steps(self, steps: list) -> None:
        self.lead_script.write_text(json.dumps(steps), encoding="utf-8")

    def _ok(self, rel: str, text: str) -> dict:
        return {"do": "ok", "write": {rel: text}}

    def _backend(self) -> AntigravityCliExecutionBackend:
        env = os.environ.copy()
        for key in _DROP:
            env.pop(key, None)
        env["FAKE_AGY_SCRIPT"] = str(self.agy_script)
        env["FAKE_AGY_LOG"] = str(self.agy_log)
        env["FAKE_AGY_SPAN"] = str(self.agy_span)
        return AntigravityCliExecutionBackend(
            bin_path=str(self.agy_shim),
            run_registry_path=str(self.registry),
            account_pool_path=str(self.pool_path),
            poll_sec=0.05,
            environ=env,
        )

    def _planner(self):
        from lead_adapter.grok_cli import GrokCliLeadAdapter

        os.environ.pop("FAKE_LEAD_SHAPE", None)
        adapter = GrokCliLeadAdapter(bin_path=str(self.lead_shim))
        return LeadAdapterPlanner(adapter, cwd=self.work, timeout_sec=self.lead_timeout)

    def _build(self) -> CollabApplication:
        backend = self._backend()
        self._backends.append(backend)
        app = CollabApplication(self.persist, planner=self._planner(), backend=backend)
        self._apps.append(app)
        self.app = app
        self.backend = backend
        return app

    def _request(
        self,
        key: str,
        *,
        prose: bool = True,
        artifacts: list[str] | None = None,
        max_reworks: int = 1,
        wall: int = 180,
    ) -> dict:
        arts = ["delivery.md"] if artifacts is None else list(artifacts)
        acceptance: dict = {"artifacts": arts}
        if prose:
            acceptance["text"] = PROSE
        return {
            "idempotency_key": key,
            "client_id": "test-suite",
            "title": key,
            "goal": "Produce the requested delivery artifact in the assigned workspace.",
            "boundaries": {
                "must": ["Stay inside the assigned workspace"],
                "must_not": ["Do not use network or system tools"],
            },
            "acceptance": acceptance,
            "budget": {"wall_sec": wall, "max_reworks": max_reworks},
        }

    def _submit(self, key: str, **kwargs) -> str:
        assert self.app is not None
        opened = self.app.submit(self._request(key, **kwargs))
        self.assertTrue(opened.get("ok"), opened)
        self.assertTrue(opened.get("goal_id"), opened)
        return str(opened["goal_id"])

    def _agy(self) -> list[dict]:
        return _jsonl(self.agy_log)

    def _reviews(self) -> list[dict]:
        return [row for row in _jsonl(self.lead_log) if row.get("kind") == "review"]

    def _wait_agy(self, count: int, timeout: float = 8.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        rows = self._agy()
        while len(rows) < count and time.monotonic() < deadline:
            time.sleep(0.05)
            rows = self._agy()
        return rows

    def _for_cwd(self, workspace: str) -> list[dict]:
        root = str(Path(workspace).resolve())
        return [row for row in self._agy() if str(Path(str(row.get("cwd") or "")).resolve()) == root]

    def _task(self, status: dict) -> dict:
        tasks = [row for row in (status.get("tasks") or []) if isinstance(row, dict)]
        self.assertEqual(len(tasks), 1, status)
        return tasks[0]

    def _result(self, status: dict) -> dict:
        result = self._task(status).get("result")
        self.assertIsInstance(result, dict, status)
        return result

    def _run_id(self, status: dict) -> str:
        task = self._task(status)
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        run_id = str(task.get("run_id") or result.get("run_id") or "")
        self.assertTrue(run_id.startswith("agy_"), status)
        return run_id

    def _lead_review_of(self, status: dict) -> dict | None:
        pending = status.get("pending_decisions") or []
        if not pending or not isinstance(pending[0], dict):
            return None
        details = pending[0].get("details") if isinstance(pending[0].get("details"), dict) else {}
        lead = details.get("lead_review")
        return lead if isinstance(lead, dict) else None

    def _review_running(self, status: dict) -> bool:
        if str(status.get("state") or "") in _TERMINAL:
            return False
        lead = self._lead_review_of(status)
        return bool(lead) and lead.get("status") == "running"

    def _review_logged(self, status: dict) -> bool:
        # Status flips to running before the fake lead process appends its log line.
        return self._review_running(status) and bool(self._reviews())

    def _saved(self, run_id: str) -> dict | None:
        if not self.store_path.is_file():
            return None
        return AgyReviewStore(self.store_path).get(run_id)

    def _account(self):
        pool = load_pool(self.pool_path)
        self.assertEqual(len(pool.accounts), 1, pool.accounts)
        return pool.accounts[0]

    def _drive_until(self, app, goal_id: str, pred, timeout: float = 12.0) -> dict:
        deadline = time.monotonic() + timeout
        last = app.status(goal_id)
        while time.monotonic() < deadline:
            _process_all(app)
            last = app.status(goal_id)
            if pred(last):
                return last
            if str(last.get("state") or "") in _TERMINAL:
                return last
            time.sleep(0.05)
        return last

    def _tick_until_terminal(self, app, goal_id: str, timeout: float = 12.0) -> dict:
        deadline = time.monotonic() + timeout
        last = app.status(goal_id)
        while time.monotonic() < deadline:
            _process_all(app)
            last = app.status(goal_id)
            if str(last.get("state") or "") in _TERMINAL:
                return last
            time.sleep(0.05)
        return last

    def test_s4a_parked_review_lets_other_goal_run(self) -> None:
        """S4(a): asserts A awaiting review on one account still lets B spawn and complete, with at most one live agy process."""
        gate = self._gate("s4a-lead")
        self._agy_steps(
            [
                self._ok("delivery.md", "from-a\n"),
                self._ok("side.md", "from-b\n"),
            ]
        )
        self._lead_steps([f"block_until:{gate}|90"])
        app = self._build()
        goal_a = self._submit("s4a-a", wall=180, max_reworks=1)
        status_a = self._drive_until(app, goal_a, self._review_running, timeout=12.0)
        self.assertTrue(
            self._review_running(status_a),
            f"goal A never entered lead review; state={status_a.get('state')} "
            f"pending={status_a.get('pending_decisions')}",
        )
        self.assertGreaterEqual(len(self._wait_agy(1)), 1, self._agy())
        ws_a = str(Path(self._agy()[0]["cwd"]).resolve())
        self.assertEqual(self._account().state, "available")
        assert self.backend is not None
        conc = self.backend.capabilities()["concurrency"]
        self.assertEqual(conc.get("limited_by"), ["agy_account_pool"], conc)
        goal_b = self._submit("s4a-b", prose=False, artifacts=["side.md"], max_reworks=0, wall=180)
        status_b = self._tick_until_terminal(app, goal_b, timeout=12.0)
        self.assertEqual(status_b["state"], "completed", status_b)
        status_a = app.status(goal_a)
        self.assertNotIn(status_a.get("state"), _TERMINAL, status_a)
        self.assertTrue(self._review_running(status_a), status_a)
        rows = self._agy()
        self.assertEqual(len(rows), 2, rows)
        ws_b = str(Path(rows[1]["cwd"]).resolve())
        self.assertNotEqual(ws_a, ws_b, rows)
        self.assertEqual(len(self._for_cwd(ws_a)), 1, rows)
        self.assertEqual(len(self._for_cwd(ws_b)), 1, rows)
        self.assertFalse(any(row.get("rework") for row in rows), rows)
        self.assertEqual(_peak_live(self.agy_span), 1, _jsonl(self.agy_span))
        account = self._account()
        self.assertEqual(account.state, "available")
        self.assertIsNone(account.lease_id)

    def test_s4b_rework_waits_for_the_single_account(self) -> None:
        """S4(b): asserts A's rework waits while B holds the only account, then spawns once; A completes with one rework and no lease left."""
        gate_lead = self._gate("s4b-lead")
        gate_b = self._gate("s4b-agy")
        self._agy_steps(
            [
                self._ok("delivery.md", "round1\n"),
                {
                    "do": "block_until",
                    "path": str(gate_b),
                    "sec": 45,
                    "write": {"side.md": "side\n"},
                },
                self._ok("delivery.md", "round2\n"),
            ]
        )
        self._lead_steps(
            [
                f"block_until_fail:{gate_lead}|needs work",
                "pass:round two",
            ]
        )
        app = self._build()
        goal_a = self._submit("s4b-a", wall=180, max_reworks=1)
        status_a = self._drive_until(app, goal_a, self._review_logged, timeout=12.0)
        self.assertTrue(
            self._review_running(status_a),
            f"goal A never entered lead review; state={status_a.get('state')} "
            f"pending={status_a.get('pending_decisions')}",
        )
        self.assertGreaterEqual(len(self._reviews()), 1, _jsonl(self.lead_log))
        self.assertGreaterEqual(len(self._wait_agy(1)), 1, self._agy())
        ws_a = str(Path(self._agy()[0]["cwd"]).resolve())
        run_a = self._run_id(status_a)
        goal_b = self._submit("s4b-b", prose=False, artifacts=["side.md"], max_reworks=0, wall=180)
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and len(self._agy()) < 2:
            _process_all(app)
            time.sleep(0.05)
        rows = self._agy()
        self.assertEqual(len(rows), 2, rows)
        ws_b = str(Path(rows[1]["cwd"]).resolve())
        self.assertNotEqual(ws_a, ws_b, rows)
        b_pid = int(rows[1]["pid"])
        self.assertTrue(_pid_alive(b_pid), b_pid)
        self.assertEqual(self._account().state, "busy")
        self.assertEqual(len(self._for_cwd(ws_a)), 1, rows)
        gate_lead.write_text("open", encoding="utf-8")
        saved = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            _process_all(app)
            saved = self._saved(run_a)
            state = str((saved or {}).get("state") or "")
            redos = int((saved or {}).get("redos") or 0)
            if state in {"rework_waiting", "review_unavailable", "rejected_final"}:
                break
            if redos >= 1 and state == "running":
                break
            time.sleep(0.05)
        status_a = app.status(goal_a)
        self.assertIsNotNone(saved, f"no review record after the lead fail; status={status_a}")
        self.assertEqual(
            (saved or {}).get("state"),
            "rework_waiting",
            "rework did not wait for a free account: "
            f"state={(saved or {}).get('state')} unavailable={(saved or {}).get('unavailable_reason')} "
            f"goal={status_a.get('state')} failure={status_a.get('failure_reason')}",
        )
        self.assertNotIn("rework_spawn_failed", json.dumps(saved))
        self.assertEqual(len(self._for_cwd(ws_a)), 1, self._agy())
        self.assertTrue(_pid_alive(b_pid), b_pid)
        self.assertNotIn(status_a.get("state"), _TERMINAL, status_a)
        self.assertEqual(_peak_live(self.agy_span), 1, _jsonl(self.agy_span))
        gate_b.write_text("open", encoding="utf-8")
        status_b = self._tick_until_terminal(app, goal_b, timeout=12.0)
        status_a = self._tick_until_terminal(app, goal_a, timeout=15.0)
        self.assertEqual(status_b["state"], "completed", status_b)
        self.assertEqual(status_a["state"], "completed", status_a)
        rework = self._result(status_a).get("rework") or {}
        self.assertEqual(rework.get("used"), 1, rework)
        a_rows = self._for_cwd(ws_a)
        b_rows = self._for_cwd(ws_b)
        self.assertEqual(len(a_rows), 2, self._agy())
        self.assertEqual(sum(1 for row in a_rows if row.get("rework")), 1, a_rows)
        self.assertEqual(len(b_rows), 1, self._agy())
        self.assertFalse(any(row.get("rework") for row in b_rows), b_rows)
        self.assertEqual(_peak_live(self.agy_span), 1, _jsonl(self.agy_span))
        account = self._account()
        self.assertEqual(account.state, "available", account)
        self.assertIsNone(account.lease_id)


if __name__ == "__main__":
    unittest.main()
