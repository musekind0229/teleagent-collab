"""stop_all must join pending _kill_later threads (order F-T14).

Reuses the in-process fakes and park/step helpers from
test_lead_review_runner. stop_goal stays non-blocking; stop_all must wait
until the stopper has patched lead_processes.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest

from tests.desktop_lock_isolation import install_desktop_lock_isolation

from test_lead_review_runner import (
    FakeAsyncBackend,
    FakePlanner,
    _action_of,
    _lead_review_events,
    _load_decision,
    _load_runner,
)
from framework.app_service import CollabApplication, DeterministicPlanner


def _stopper_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if str(thread.name or "").startswith("collab-review-stop-")
    ]


class LeadReviewRunnerStopJoinTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        install_desktop_lock_isolation(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._planners: list[FakePlanner] = []
        self._runners: list = []
        self.addCleanup(self._shutdown)

    def _shutdown(self) -> None:
        for planner in self._planners:
            planner.release_all()
        for runner in list(self._runners):
            stop = getattr(runner, "stop_all", None)
            if not callable(stop):
                continue
            box: dict = {}

            def _run(stop=stop) -> None:
                try:
                    box["out"] = stop("test cleanup")
                except BaseException as exc:  # noqa: BLE001 — cleanup must not mask the test
                    box["exc"] = exc

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(4.0)
        for planner in self._planners:
            planner.release_all()
            planner.wait_completed(planner.calls, timeout=1.0)

    def _planner(self, script, *, timeout_sec: float = 1.0) -> FakePlanner:
        planner = FakePlanner(script, timeout_sec=timeout_sec)
        self._planners.append(planner)
        return planner

    def _make_app(self, backend) -> CollabApplication:
        return CollabApplication(
            self._tmp.name,
            planner=DeterministicPlanner(),
            backend=backend,
            max_parallel_per_goal=4,
            max_parallel_global=8,
        )

    def _make_runner(self, app, planner, backend, *, token: str):
        lead_review_runner = _load_runner()
        runner = lead_review_runner(
            layer=app.layer,
            planner=planner,
            backend=backend,
            token=token,
        )
        self._runners.append(runner)
        hook = getattr(runner, "_on_backend_resolved", None)
        register = getattr(backend, "add_review_listener", None)
        if callable(hook) and callable(register):
            register(hook)
        return runner

    def _park(self, app, key: str):
        submitted = app.submit(
            {
                "idempotency_key": key,
                "client_id": "test-suite",
                "title": "Application entry pilot",
                "goal": "Create the requested delivery artifact inside the assigned workspace.",
                "boundaries": {
                    "must": ["Stay inside the assigned workspace"],
                    "must_not": ["Do not use network or system tools"],
                },
                "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
                "budget": {"wall_sec": 30, "max_reworks": 0},
            }
        )
        goal_id = submitted["goal_id"]
        last = None
        snap = {}
        for _ in range(8):
            last = app.coordinator.process_all()
            snap = app.layer.get_goal(goal_id)["goal"]
            pending = [row for row in (snap.get("pending_decisions") or []) if isinstance(row, dict)]
            reviews = [
                row
                for row in pending
                if isinstance((row.get("details") or {}), dict)
                and (row.get("details") or {}).get("backend_kind") == "review"
            ]
            if len(pending) == 1 and len(reviews) == 1:
                return goal_id, snap, reviews[0]
            if snap.get("state") in {"failed", "completed", "cancelled"}:
                break
        self.fail(
            f"pending review not parked: state={snap.get('state')} "
            f"failure={snap.get('failure')} last={last} pending={snap.get('pending_decisions')}"
        )

    def _step(self, runner, snap, task, decision, action, *, timeout: float = 1.0):
        box: dict = {}

        def _run() -> None:
            try:
                box["out"] = runner.step(snap, task, decision=decision, action=action)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the test thread
                box["exc"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.fail(f"step blocked longer than {timeout}s")
        if "exc" in box:
            raise box["exc"]
        return box["out"]

    def test_stop_all_joins_kill_later_and_patches_lead_processes(self):
        """asserts stop_all waits for the stop_goal stopper thread and the lead_processes patch is written."""
        backend = FakeAsyncBackend()
        app = self._make_app(backend)
        # honor_cancel=False so scope.cancel does not set job.done; the timer does.
        planner = self._planner(["block_pass"], timeout_sec=2.0)
        goal_id, _snap, decision = self._park(app, "ft14-stop-join")
        action = _action_of(decision)
        decision_id = decision["decision_id"]
        runner = self._make_runner(app, planner, backend, token="tokA")
        snap, task, decision = _load_decision(app, goal_id, decision_id)
        self._step(runner, snap, task, decision, action)
        self.assertTrue(planner.wait_blocked(action["request_id"], timeout=1.0))
        self.assertEqual(runner.live_jobs(), 1)
        with runner._lock:
            jobs = list(runner._jobs.values())
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        done = job["done"]
        self.assertIsInstance(done, threading.Event)
        self.assertFalse(done.is_set())

        started = time.monotonic()
        runner.stop_goal(goal_id, "cancel during review")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(runner.live_jobs(), 0)
        stoppers = _stopper_threads()
        self.assertTrue(stoppers, "stop_goal must start a collab-review-stop thread")
        self.assertTrue(any(thread.is_alive() for thread in stoppers), [t.name for t in stoppers])
        self.assertFalse(done.is_set())

        timer = threading.Timer(0.5, done.set)
        timer.daemon = True
        self.addCleanup(timer.cancel)
        timer.start()

        started = time.monotonic()
        rows = runner.stop_all("service shutdown")
        elapsed = time.monotonic() - started
        self.assertIsInstance(rows, list)
        self.assertEqual(rows, [])
        self.assertLess(elapsed, 4.0)
        self.assertGreaterEqual(elapsed, 0.35)

        alive = [thread for thread in _stopper_threads() if thread.is_alive()]
        self.assertEqual(alive, [], [thread.name for thread in alive])
        for thread in stoppers:
            self.assertFalse(thread.is_alive(), thread.name)

        events = [
            row
            for row in _lead_review_events(app, goal_id, decision_id)
            if row.get("event") == "lead_review_stopped"
        ]
        self.assertTrue(events, "lead_review_stopped event missing")
        self.assertIn("lead_processes", events[-1])
        self.assertIsInstance(events[-1].get("lead_processes"), list)


if __name__ == "__main__":
    unittest.main()
