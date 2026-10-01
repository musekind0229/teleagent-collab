"""#16: LeadAdapterPlanner keeps the adapter failure identity before unwrapping.

Offline only: fake adapters, no model, no network, no worker.
"""
from __future__ import annotations

import json
import tempfile
import unittest

from framework.app_service import AppError, CollabApplication, LeadAdapterPlanner
from lead_adapter.base import safe_failure
from test_app_service import _FakeLead, _request


class _FailingLead:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    def decide(self, request, *, schema, cwd, timeout_sec=180):
        self.calls += 1
        return safe_failure(self.status, "OFFLINE_SENTINEL_" + self.status)


class _RawLead:
    def __init__(self, parsed) -> None:
        self.parsed = parsed

    def decide(self, request, *, schema, cwd, timeout_sec=180):
        parsed = self.parsed(request) if callable(self.parsed) else self.parsed
        return json.dumps(parsed), parsed


class _CountingBackend:
    """Any start_run would be a bug: planning failed, no worker may start."""

    backend_id = "test.counting"

    def __init__(self) -> None:
        self.starts = 0

    def __getattr__(self, name):
        if name == "start_run":
            def start_run(*a, **k):
                self.starts += 1
                raise AssertionError("worker started after planning failure")
            return start_run
        raise AttributeError(name)


class PlannerFailureIdentityTests(unittest.TestCase):
    def test_safe_failure_status_and_message_survive(self):
        for status in ("timeout", "call_failed", "error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                planner = LeadAdapterPlanner(_FailingLead(status), cwd=td)
                app = CollabApplication(td, planner=planner)
                goal = app.submit(_request())["goal_id"]
                snap = app.layer.get_goal(goal)["goal"]
                with self.assertRaises(AppError) as cm:
                    planner.plan(snap)
                err = cm.exception
                self.assertEqual(err.code, "lead_unavailable")
                self.assertEqual(err.status, 503)
                self.assertEqual(err.extra.get("lead_status"), status)
                self.assertIn("OFFLINE_SENTINEL_" + status, str(err))
                self.assertIn(f"[{status}]", str(err))
                self.assertNotIn("did not return a plan object", str(err))

    def test_goal_status_projects_planning_failure(self):
        for status in ("timeout", "call_failed", "error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                lead = _FailingLead(status)
                app = CollabApplication(td, planner=LeadAdapterPlanner(lead, cwd=td))
                goal = app.submit(_request())["goal_id"]
                outcome = app.coordinator.process_goal(goal)
                self.assertEqual(outcome["action"], "planning_failed")
                view = app.status(goal)
                self.assertEqual(view["state"], "failed")
                self.assertEqual(view["tasks"], [])
                self.assertIn("OFFLINE_SENTINEL_" + status, view["failure_reason"])
                primary = view["primary_failure"]
                self.assertEqual(primary["stage"], "planning")
                self.assertEqual(primary["source"], "planner")
                self.assertEqual(primary["code"], "lead_unavailable")
                self.assertEqual(primary["lead_status"], status)
                self.assertEqual(primary["task_id"], "")
                self.assertFalse(primary["retryable"])
                self.assertEqual(view["failures"], [primary])
                self.assertEqual(lead.calls, 1)  # no retry

    def test_no_worker_starts_after_planning_failure(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _CountingBackend()
            app = CollabApplication(td, planner=LeadAdapterPlanner(_FailingLead("timeout"), cwd=td))
            app.coordinator.backend = backend
            goal = app.submit(_request())["goal_id"]
            app.coordinator.process_goal(goal)
            app.coordinator.process_goal(goal)
            self.assertEqual(backend.starts, 0)
            self.assertEqual(app.status(goal)["state"], "failed")

    def test_malformed_payloads_stay_invalid_plan(self):
        for parsed in (None, {}, {"structuredOutput": None, "structuredOutputError": "x"}, {"stopReason": "cancelled"}):
            with self.subTest(parsed=parsed), tempfile.TemporaryDirectory() as td:
                app = CollabApplication(td, planner=LeadAdapterPlanner(_RawLead(parsed), cwd=td))
                goal = app.submit(_request())["goal_id"]
                app.coordinator.process_goal(goal)
                view = app.status(goal)
                self.assertEqual(view["state"], "failed")
                self.assertIn("invalid_plan", view["failure_reason"])
                self.assertEqual(view["primary_failure"]["code"], "invalid_plan")
                self.assertNotIn("lead_status", view["primary_failure"])

    def test_stale_plan_still_rejected(self):
        def stale(request):
            out = _FakeLead().decide(request, schema={}, cwd="")[1]
            out["application_id"] = "plan_other"
            return out

        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, planner=LeadAdapterPlanner(_RawLead(stale), cwd=td))
            goal = app.submit(_request())["goal_id"]
            app.coordinator.process_goal(goal)
            view = app.status(goal)
            self.assertIn("stale_plan", view["failure_reason"])

    def test_valid_and_enveloped_plans_still_unwrap(self):
        def enveloped(request):
            inner = _FakeLead().decide(request, schema={}, cwd="")[1]
            return {"structuredOutput": json.dumps(inner)}

        for lead in (_FakeLead(), _RawLead(enveloped)):
            with self.subTest(lead=type(lead).__name__), tempfile.TemporaryDirectory() as td:
                app = CollabApplication(td, planner=LeadAdapterPlanner(lead, cwd=td))
                goal = app.submit(_request())["goal_id"]
                app.coordinator.process_goal(goal)
                view = app.status(goal)
                self.assertEqual(view["state"], "completed", view.get("failure_reason"))
                self.assertEqual(len(view["tasks"]), 2)

    def test_unsafe_plan_artifacts_still_rejected(self):
        def traversal(request):
            out = _FakeLead().decide(request, schema={}, cwd="")[1]
            out["tasks"][0]["artifacts"] = ["../escape.txt"]
            return out

        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, planner=LeadAdapterPlanner(_RawLead(traversal), cwd=td))
            goal = app.submit(_request())["goal_id"]
            app.coordinator.process_goal(goal)
            view = app.status(goal)
            self.assertEqual(view["state"], "failed")
            self.assertEqual(view["tasks"], [])


if __name__ == "__main__":
    unittest.main()
