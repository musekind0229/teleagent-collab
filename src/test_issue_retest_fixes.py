"""Fixes found while re-testing issues #2/#5/#9/#11/#12 on the box. Offline fakes only."""
from __future__ import annotations

import copy
import json
import threading
import tempfile
import unittest
from pathlib import Path

from framework.app_service import (
    AppError,
    CollabApplication,
    LeadAdapterPlanner,
    acceptance_status_view,
    build_planning_request,
    validate_plan,
)
from test_app_service import _AsyncBackend, _FakeLead, _request


class _TimeoutWithFilesBackend(_AsyncBackend):
    """Writes the artifact, then reports a wall-clock timeout (Win #12 shape)."""

    backend_id = "fake.timeout_v1"

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": False}

    def collect_result(self, run_id):
        for rel in self.artifacts:
            (self.directory / rel).write_text("candidate\n", encoding="utf-8")
        return {"ok": False, "run_id": run_id, "error": "timeout", "missing": []}


class AcceptanceStatusTests(unittest.TestCase):
    def test_completed_is_not_business_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            goal = app.submit(_request())["goal_id"]
            app.coordinator.process_goal(goal)
            view = app.status(goal)
            self.assertEqual(view["state"], "completed")
            acc = view["acceptance_status"]
            self.assertEqual(acc["execution"], "succeeded")
            self.assertEqual(acc["artifacts"], "complete")
            self.assertEqual(acc["business_acceptance"], "not_performed")
            self.assertEqual(acc["deployed"], "not_tracked")
            self.assertIn(acc["technical_review"], {"not_available", "unsupported", "via_lead_gate"})

    def test_exact_content_check_is_independent_check(self):
        caps = {"acceptance": {"lead_review": False}}
        passed = [{"status": "succeeded", "result": {"ok": True, "review": {"status": "passed", "source": "agy_exact_content"}}}]
        failed = [{"status": "failed", "result": {"ok": False, "error": "acceptance_failed: x", "review": {"status": "failed", "source": "agy_exact_content"}}}]
        prose = [{"status": "succeeded", "result": {"ok": True, "review": {"status": "unsupported", "source": "none"}}}]
        self.assertEqual(acceptance_status_view("completed", passed, caps)["independent_checks"], "passed")
        self.assertEqual(acceptance_status_view("failed", failed, caps)["independent_checks"], "failed")
        prose_view = acceptance_status_view("completed", prose, caps)
        self.assertEqual(prose_view["independent_checks"], "not_run")
        self.assertEqual(prose_view["technical_review"], "unsupported")

    def test_missing_artifacts_reported(self):
        tasks = [{"status": "failed", "result": {"ok": False, "missing": ["a.txt", "b.txt"]}}]
        view = acceptance_status_view("failed", tasks, {})
        self.assertEqual(view["artifacts"], "incomplete")
        self.assertEqual(view["missing_artifacts"], ["a.txt", "b.txt"])


class CandidateOnFailureTests(unittest.TestCase):
    def test_timeout_with_files_reports_unreviewed_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_TimeoutWithFilesBackend())
            goal = app.submit(_request())["goal_id"]
            for _ in range(3):
                app.coordinator.process_goal(goal)
            view = app.status(goal)
            self.assertEqual(view["state"], "failed")
            primary = view["primary_failure"]
            self.assertTrue(view["failure_reason"])
            self.assertEqual(primary["stage"], "worker")
            self.assertIs(primary["candidate_available"], True)
            self.assertEqual(primary["candidate_artifacts"], ["delivery.txt"])
            self.assertEqual(primary["review_status"], "not_requested")
            self.assertEqual(view["acceptance_status"]["business_acceptance"], "not_performed")

    def test_symlinked_or_escaping_candidate_is_not_listed(self):
        from framework.app_service import _candidate_artifacts

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            ws.mkdir()
            outside = Path(td) / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            (ws / "link.txt").symlink_to(outside)
            (ws / "ok.txt").write_text("y", encoding="utf-8")
            task = {"workspace": str(ws), "expected_artifacts": ["link.txt", "ok.txt", "../outside.txt", "/etc/passwd", "gone.txt"]}
            self.assertEqual(_candidate_artifacts(task), ["ok.txt"])


class PlanningPhaseTests(unittest.TestCase):
    def test_progress_says_planning_while_lead_call_in_flight(self):
        gate = threading.Event()
        seen = {}

        class _SlowLead(_FakeLead):
            def decide(self, request, *, schema, cwd, timeout_sec=180):
                seen["view"] = app.status(goal_holder["id"])["progress"]
                gate.set()
                return super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)

        goal_holder = {}
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, planner=LeadAdapterPlanner(_SlowLead(), cwd=td))
            goal_holder["id"] = app.submit(_request())["goal_id"]
            before = app.status(goal_holder["id"])["progress"]
            self.assertNotEqual(before["phase"], "planning")
            app.coordinator.process_goal(goal_holder["id"])
            self.assertTrue(gate.is_set())
            self.assertEqual(seen["view"]["phase"], "planning")
            self.assertEqual(seen["view"]["source"], "coordinator")
            self.assertIsNone(seen["view"]["last_heartbeat_at"])
            after = app.status(goal_holder["id"])["progress"]
            self.assertNotEqual(after["phase"], "planning")


class SubmitAndPlanValidationTests(unittest.TestCase):
    def test_unknown_acceptance_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            body = _request()
            body["acceptance"] = {"artifacts": ["delivery.txt"], "must_contain": "x"}
            with self.assertRaises(AppError) as cm:
                app.submit(body)
            self.assertEqual(cm.exception.status, 400)
            self.assertIn("must_contain", str(cm.exception))
            ok = _request()
            ok["acceptance"] = {"artifacts": ["delivery.txt"], "text": "t", "allow_aigc_marks": True}
            self.assertTrue(app.submit(ok)["created"])

    def test_unsafe_row_artifacts_are_invalid_plan(self):
        snap = {"goal_id": "g1", "goal": {"desired_outcome": "x", "acceptance": {"artifacts": ["a.txt"]}}}
        request = build_planning_request(snap)
        for bad in (["/etc/passwd"], ["../x.txt"], ["..\\\\x.txt"], ["C:/x.txt"]):
            plan = {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "summary": "s",
                "tasks": [{"task_key": "a", "title": "a", "instruction": "a", "depends_on": [], "artifacts": bad}],
            }
            with self.subTest(bad=bad), self.assertRaises(AppError) as cm:
                validate_plan(copy.deepcopy(plan), request)
            self.assertEqual(cm.exception.code, "invalid_plan")


if __name__ == "__main__":
    unittest.main()
