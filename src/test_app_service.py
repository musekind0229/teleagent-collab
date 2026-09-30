from __future__ import annotations

import json
import copy
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from jsonschema import Draft202012Validator

# tests/ is not a package. The shared lock-isolation helper lives there.
_TESTS_DIR = str(Path(__file__).resolve().parents[1] / "tests")
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)

from desktop_lock_isolation import install_desktop_lock_isolation
from framework.app_service import (
    AppError,
    CollabApplication,
    CollabHttpServer,
    GOAL_HTTP_TERMINAL,
    LeadAdapterPlanner,
    TASK_HTTP_TERMINAL,
    allows_aigc_marks,
    gate_collected_artifacts,
    lead_error_retryable,
    project_external_inputs,
    project_forbidden_tools,
    review_contamination_summary,
    split_task_musts,
    task_acceptance_criteria,
    worker_charter_for_task,
    build_planning_request,
)
from framework.artifact_handoff import (
    HandoffError,
    collect_direct_dep_artifacts,
    safe_relative_name,
    stage_handoff_files,
)
from execution_backend.base import BackendError, BackendStatus
from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend
from win_collab.core import ArtifactContaminatedError, Store

GOAL_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "contracts" / "goal.schema.json").read_text(encoding="utf-8")
)


def _task_finish_events(events_payload, task_id: str = "") -> list:
    """History rows that record one task finish (``finish_task`` / ``task_finished``)."""
    rows = []
    if isinstance(events_payload, dict):
        rows = events_payload.get("events") or []
    elif isinstance(events_payload, list):
        rows = events_payload
    hits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        markers = {
            str(row.get("action") or ""),
            str(row.get("op") or ""),
            str(row.get("event_kind") or ""),
        }
        if "task_finished" not in markers and "finish_task" not in markers:
            continue
        row_task = str(row.get("task_id") or "")
        if task_id and row_task and row_task != task_id:
            continue
        hits.append(row)
    return hits


def _request() -> dict:
    return {
        "idempotency_key": "pilot-1",
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


class _FakeLead:
    def decide(self, request, *, schema, cwd, timeout_sec=180):
        out = {
            "application_id": request["application_id"],
            "context_summary": request["context_summary"],
            "summary": "two ordered tasks",
            "tasks": [
                {
                    "task_key": "prepare",
                    "title": "Prepare",
                    "instruction": "Create prep.txt",
                    "depends_on": [],
                    "artifacts": ["prep.txt"],
                },
                {
                    "task_key": "deliver",
                    "title": "Deliver",
                    "instruction": "Create delivery.txt",
                    "depends_on": ["prepare"],
                    "artifacts": ["delivery.txt"],
                },
            ],
        }
        return json.dumps(out), out


class _TwoStepLead(_FakeLead):
    def decide(self, request, *, schema, cwd, timeout_sec=180):
        if request.get("kind") == "permission":
            out = {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "decision": "once",
                "reason": "Write is confined to the assigned workspace",
            }
            return json.dumps(out), out
        if request.get("kind") == "review":
            out = {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "verdict": "pass",
                "reason": "Required artifact and evidence are present",
            }
            return json.dumps(out), out
        return super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)


class _RecordingLead(_TwoStepLead):
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def decide(self, request, *, schema, cwd, timeout_sec=180):
        self.requests.append(request)
        return super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)


class _TimeoutThenPassLead(_TwoStepLead):
    def __init__(self) -> None:
        self.review_calls = 0

    def decide(self, request, *, schema, cwd, timeout_sec=180):
        if request.get("kind") == "review":
            self.review_calls += 1
            if self.review_calls == 1:
                return "TIMEOUT", {"_lead_status": "timeout", "error": "grok_cli timeout"}
        return super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)


class _QuotaLead(_TwoStepLead):
    def __init__(self) -> None:
        self.review_calls = 0

    def decide(self, request, *, schema, cwd, timeout_sec=180):
        if request.get("kind") == "review":
            self.review_calls += 1
            return "", {"_lead_status": "call_failed", "error": "HTTP 429 quota exceeded"}
        return super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)


class _BrokenPlanner:
    name = "broken"

    def plan(self, goal_snapshot):
        raise RuntimeError("simulated planner outage")


class _AutoLead:
    def decide(self, request, *, schema, cwd, timeout_sec=180):
        if request["kind"] == "permission":
            out = {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "decision": "once",
                "reason": "Write is confined to the assigned workspace",
            }
        elif request["kind"] == "review":
            out = {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "verdict": "pass",
                "reason": "Required artifact and evidence are present",
            }
        else:
            artifacts = list((request.get("acceptance_criteria") or {}).get("artifacts") or [])
            out = {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "summary": "one task",
                "tasks": [
                    {
                        "task_key": "deliver",
                        "title": "Deliver",
                        "instruction": request["task_goal"],
                        "depends_on": [],
                        "artifacts": artifacts,
                    }
                ],
            }
        return json.dumps(out), out


class _AsyncBackend:
    backend_id = "fake.async_v1"

    def __init__(self) -> None:
        self.starts = 0
        self.polls = 0
        self.directory = Path(".")

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        self.starts += 1
        self.directory = Path(directory)
        self.artifacts = list(artifacts or [])
        return {
            "ok": True,
            "backend": self.backend_id,
            "run_id": "async-run-1",
            "native_handle": "native-async-run-1",
        }

    def observe_run(self, run_id, **kwargs):
        self.polls += 1
        return {"busy": self.polls == 1, "finish_successful": self.polls > 1}

    def collect_result(self, run_id):
        written = []
        for rel in self.artifacts:
            path = self.directory / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("async worker result\n", encoding="utf-8")
            written.append(str(path))
        return {"ok": True, "run_id": run_id, "artifacts": written}

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


class _RecordingBackend:
    backend_id = "fake.recording_v1"

    def __init__(self) -> None:
        self.starts: list[dict] = []
        self._runs: dict[str, dict] = {}

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        root = Path(directory)
        seen = {}
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    seen[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        self.starts.append(
            {
                "title": title,
                "directory": str(root),
                "instruction": instruction,
                "charter": dict(charter or {}),
                "files": seen,
            }
        )
        written = []
        for rel in artifacts or []:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"produced by {title}\n", encoding="utf-8")
            written.append(str(path))
        run_id = f"rec-{len(self.starts)}"
        self._runs[run_id] = {"written": written, "workspace": str(root)}
        return {"ok": True, "backend": self.backend_id, "run_id": run_id, "native_handle": run_id}

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": True}

    def collect_result(self, run_id):
        rec = self._runs[run_id]
        return {"ok": True, "run_id": run_id, "artifacts": list(rec["written"]), "workspace": rec["workspace"]}

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


class _FailFirstBackend(_RecordingBackend):
    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        if not self.starts:
            self.starts.append({"title": title, "files": {}, "charter": dict(charter or {})})
            return {"ok": False, "error": "producer failed", "run_id": "fail-a"}
        return super().start_run(
            title=title,
            directory=directory,
            instruction=instruction,
            artifacts=artifacts,
            charter=charter,
        )


class _TeleAgentClient:
    base = "http://127.0.0.1:4398"
    instance_id = "fake-windows-teleagent"

    def __init__(self) -> None:
        self.pending = []
        self.questions = []
        self.status = {}
        self.messages = {}
        self.calls = []
        self.count = 0

    def call(self, method, path, body=None, workspace=None):
        self.calls.append((method, path, copy.deepcopy(body), workspace))
        if method == "POST" and path == "/session":
            self.count += 1
            sid = f"ses-{self.count}"
            self.status[sid] = {"type": "busy"}
            self.messages[sid] = []
            return {"id": sid, "permission": body.get("permission")}
        if path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            self.messages[sid].append({"info": {"role": "user"}})
            self.status[sid] = {"type": "busy"}
            return None
        if method == "GET" and path == "/permission":
            return copy.deepcopy(self.pending)
        if method == "GET" and path == "/question":
            return copy.deepcopy(self.questions)
        if method == "GET" and path == "/session/status":
            return copy.deepcopy(self.status)
        if method == "GET" and path.endswith("/message"):
            return copy.deepcopy(self.messages[path.split("/")[2]])
        if path.startswith("/permission/") and path.endswith("/reply"):
            request_id = path.split("/")[2]
            self.pending = [p for p in self.pending if p.get("id") != request_id]
            return True
        if path.endswith("/abort"):
            self.status[path.split("/")[2]] = {"type": "idle"}
            return True
        raise AssertionError((method, path))


class _ContentBackend:
    """collect_result writes one text file and reports ok. The service gate scans it."""

    backend_id = "fake.content_v1"

    def __init__(self, content: str) -> None:
        self.content = content
        self.directory = Path(".")
        self.artifacts: list[str] = []

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        self.directory = Path(directory)
        self.artifacts = [str(x) for x in (artifacts or [])]
        return {
            "ok": True,
            "backend": self.backend_id,
            "run_id": "content-run-1",
            "native_handle": "content-run-1",
        }

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": True}

    def collect_result(self, run_id):
        written = []
        for rel in self.artifacts:
            path = self.directory / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.content, encoding="utf-8")
            written.append(str(path))
        return {
            "ok": True,
            "run_id": run_id,
            "artifacts": written,
            "workspace": str(self.directory),
        }

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


class _ContaminatedReviewBackend:
    backend_id = "fake.review_contam_v1"

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        return {"ok": True, "backend": self.backend_id, "run_id": "rev-contam", "native_handle": "rev-contam"}

    def observe_run(self, run_id, **kwargs):
        return {"busy": True}

    def list_pending_actions(self, *, session_id=None):
        return 200, [
            {
                "request_id": "req-contam-1",
                "kind": "review",
                "context_hash": "abc",
                "payload": {
                    "artifacts": {
                        "hello.txt": {
                            "bytes": 20,
                            "contamination": {
                                "contaminated": True,
                                "encoding": "utf-8",
                                "invisible": {"U+200B": 4, "U+200D": 4},
                                "aigc_marks": {"AI生成": 1},
                                "first_offset": 0,
                                "scanned": True,
                            },
                        }
                    },
                    "finish": "stop",
                    "policy_violations": [],
                },
            }
        ]

    def collect_result(self, run_id):
        return {"ok": False, "run_id": run_id, "error": "still in review"}

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


_DIRTY_ARTIFACT = "hello decision\n\nAI生成\n" + ("\u200b\u200d" * 3)


class _PendingCancelBackend(_AsyncBackend):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel(self, run_id):
        self.cancel_calls += 1
        if self.cancel_calls == 1:
            return 200, {"ok": False, "run_id": run_id, "state": "stopping", "pending": True}
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


def _decision_goal(decision_id: str = "d1", backend_request_id: str = "backend-req") -> dict:
    return {
        "ok": True,
        "submitter_id": "api:test",
        "goal": {
            "state": "running",
            "pending_decisions": [
                {
                    "decision_id": decision_id,
                    "kind": "artifact_review",
                    "return_to_upper": False,
                    "details": {
                        "backend_request_id": backend_request_id,
                        "backend_kind": "review",
                    },
                }
            ],
            "resolved_decisions": [],
        },
    }


class _RaisingBackend:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.backend_id = "test.raise"
        self.calls: list = []

    def resolve_decision(self, request_id, *, verdict, reason, answers=None):
        self.calls.append((request_id, verdict, reason, answers))
        raise self._exc


class AppServiceTests(unittest.TestCase):
    def setUp(self):
        install_desktop_lock_isolation(self)

    @staticmethod
    def _force_controller_scan(state_dir: Path, run_id: str) -> None:
        store = Store(state_dir)
        try:
            with store.transaction():
                job = store.get(run_id)
                job["next_scan"] = 0
                store.save(job)
        finally:
            store.db.close()

    def _complete_running_windows_file(self, *, app, client, controller_state, goal_id, rel, content):
        task = next(t for t in app.status(goal_id)["tasks"] if t.get("status") == "running")
        run_id = task["run_id"]
        store = Store(controller_state)
        try:
            job = store.get(run_id)
        finally:
            store.db.close()
        sid = job["session_id"]
        workspace = Path(job["workspace"])
        client.pending.append(
            {
                "id": f"perm-{rel}",
                "sessionID": sid,
                "permission": "edit",
                "patterns": [str(workspace / rel)],
            }
        )
        self._force_controller_scan(controller_state, run_id)
        app.coordinator.process_goal(goal_id)
        resolved = app.coordinator.process_goal(goal_id)
        self.assertEqual(resolved.get("action"), "decision_resolved", resolved)
        (workspace / rel).write_text(content, encoding="utf-8")
        client.status[sid] = {"type": "idle"}
        client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
        self._force_controller_scan(controller_state, run_id)
        app.coordinator.process_goal(goal_id)
        review = app.coordinator.process_goal(goal_id)
        self.assertEqual(review.get("action"), "decision_resolved", review)
        finished = app.coordinator.process_goal(goal_id)
        self.assertEqual(finished.get("action"), "task_finished", finished)
        return job

    def test_external_request_plans_runs_and_reports_without_agent_addressing(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            opened = app.submit(_request())
            self.assertTrue(opened["created"])
            self.assertEqual(app.status(opened["goal_id"])["tasks"], [])
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertTrue(tick["ok"], tick)
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["tasks"][0]["status"], "succeeded")
            artifact = Path(td) / "workspaces" / opened["goal_id"] / status["tasks"][0]["task_id"] / "delivery.txt"
            self.assertTrue(artifact.is_file())
            self.assertTrue(app.report(opened["goal_id"])["ok"])
            duplicate = app.submit(_request())
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["goal_id"], opened["goal_id"])

    def test_submit_key_content_conflict_is_not_silent(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            app.submit(_request())
            changed = _request()
            changed["goal"] = "A different request under the same key"
            with self.assertRaises(AppError) as cm:
                app.submit(changed)
            self.assertEqual(cm.exception.status, 409)

    def test_planner_failure_is_visible_and_durable(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, planner=_BrokenPlanner())
            opened = app.submit(_request())
            outcome = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(outcome["action"], "planning_failed")
            self.assertFalse(outcome["ok"])
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["tasks"], [])

    def test_lead_planner_preserves_dependencies_and_binding(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, planner=LeadAdapterPlanner(_FakeLead(), cwd=td))
            opened = app.submit(_request())
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertTrue(first["ok"], first)
            mid = app.status(opened["goal_id"])
            self.assertEqual([t["status"] for t in mid["tasks"]], ["succeeded", "queued"])
            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertTrue(second["ok"], second)
            done = app.status(opened["goal_id"])
            self.assertEqual(done["state"], "completed")
            self.assertEqual([t["status"] for t in done["tasks"]], ["succeeded", "succeeded"])

    def test_async_worker_run_is_persisted_and_resumed_without_redispatch(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _AsyncBackend()
            app = CollabApplication(td, backend=backend)
            opened = app.submit(_request())
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "worker_running")
            running = app.status(opened["goal_id"])["tasks"][0]
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["run_id"], "async-run-1")
            self.assertEqual(running["backend"], backend.backend_id)
            self.assertEqual(running["assignee_role"], "executor")

            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(second["action"], "task_finished")
            self.assertEqual(app.status(opened["goal_id"])["state"], "completed")
            self.assertEqual(backend.starts, 1)

    def test_cancel_running_request_stops_backend_before_terminal_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _AsyncBackend()
            app = CollabApplication(td, backend=backend)
            opened = app.submit(_request())
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            cancelled = app.cancel(opened["goal_id"], "test cancellation")
            self.assertTrue(cancelled["ok"], cancelled)
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertTrue(cancelled["backend_cancellations"][0]["ok"])
            self.assertEqual(app.status(opened["goal_id"])["state"], "cancelled")

    def test_pending_backend_cancel_is_rechecked_by_coordinator(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _PendingCancelBackend()
            app = CollabApplication(td, backend=backend)
            opened = app.submit(_request())
            app.coordinator.process_goal(opened["goal_id"])
            requested = app.cancel(opened["goal_id"], "test pending cancellation")
            self.assertEqual(requested["state"], "cancel_requested")
            self.assertFalse(requested["backend_cancellations"][0]["ok"])
            effected = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(effected["action"], "cancelled")
            self.assertEqual(app.status(opened["goal_id"])["state"], "cancelled")

    def test_windows_controller_bridge_handles_permission_review_and_completion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            app = CollabApplication(root / "app", backend=backend)
            opened = app.submit(_request())

            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "worker_running")
            task = app.status(opened["goal_id"])["tasks"][0]
            run_id = task["run_id"]
            store = Store(controller_state)
            try:
                job = store.get(run_id)
            finally:
                store.db.close()
            sid = job["session_id"]
            self.assertEqual(client.count, 1)

            client.pending.append(
                {
                    "id": "perm-1",
                    "sessionID": sid,
                    "permission": "edit",
                    "patterns": [str(Path(job["workspace"]) / "delivery.txt")],
                    "metadata": {"diff": "create delivery"},
                }
            )
            self._force_controller_scan(controller_state, run_id)
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            decision_tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(decision_tick["action"], "decision_required")
            decision = app.status(opened["goal_id"])["pending_decisions"][0]
            resolved = app.resolve(
                opened["goal_id"],
                decision["decision_id"],
                {"verdict": "once", "reason": "Bounded write in assigned workspace"},
            )
            self.assertTrue(resolved["ok"])
            self.assertEqual(client.pending, [])

            artifact = Path(job["workspace"]) / "delivery.txt"
            artifact.write_text("completed by TeleAgent\n", encoding="utf-8")
            client.status[sid] = {"type": "idle"}
            client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
            self._force_controller_scan(controller_state, run_id)
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            review_tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(review_tick["action"], "decision_required")
            review = app.status(opened["goal_id"])["pending_decisions"][0]
            self.assertEqual(review["kind"], "artifact_review")
            resolved_review = app.resolve(
                opened["goal_id"],
                review["decision_id"],
                {"verdict": "pass", "reason": "Artifact content and tool evidence accepted"},
            )
            self.assertTrue(resolved_review["ok"], resolved_review)
            self.assertEqual(resolved_review["tick"]["action"], "task_finished", resolved_review)
            task_id = str(task["task_id"])
            finishes = _task_finish_events(app.events(opened["goal_id"]), task_id)
            self.assertEqual(len(finishes), 1, finishes)
            final = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(final["action"], "terminal", final)
            finishes_after = _task_finish_events(app.events(opened["goal_id"]), task_id)
            self.assertEqual(len(finishes_after), 1, finishes_after)
            self.assertEqual(app.status(opened["goal_id"])["state"], "completed")
            self.assertEqual(client.count, 1)

    def test_pluggable_lead_auto_resolves_routine_worker_gates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            planner = LeadAdapterPlanner(_AutoLead(), cwd=root / "lead")
            app = CollabApplication(root / "app", backend=backend, planner=planner)
            opened = app.submit(_request())
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            task = app.status(opened["goal_id"])["tasks"][0]
            run_id = task["run_id"]
            store = Store(controller_state)
            try:
                job = store.get(run_id)
            finally:
                store.db.close()
            sid = job["session_id"]
            client.pending.append(
                {
                    "id": "auto-perm",
                    "sessionID": sid,
                    "permission": "edit",
                    "patterns": [str(Path(job["workspace"]) / "delivery.txt")],
                }
            )
            self._force_controller_scan(controller_state, run_id)
            app.coordinator.process_goal(opened["goal_id"])
            auto_permission = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(auto_permission["action"], "decision_resolved")
            self.assertEqual(app.status(opened["goal_id"])["pending_decisions"], [])

            (Path(job["workspace"]) / "delivery.txt").write_text("done\n", encoding="utf-8")
            client.status[sid] = {"type": "idle"}
            client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
            self._force_controller_scan(controller_state, run_id)
            app.coordinator.process_goal(opened["goal_id"])
            auto_review = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(auto_review["action"], "decision_resolved")
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "task_finished")
            self.assertEqual(app.status(opened["goal_id"])["state"], "completed")

    def test_project_forbidden_tools_uses_explicit_field_only(self):
        self.assertEqual(
            project_forbidden_tools(
                payload={},
                goal_text="Use a file-writing tool only. No shell.",
                must=["Write only the assigned workspace"],
                must_not=["Do not use the network"],
            ),
            [],
        )
        self.assertEqual(
            project_forbidden_tools(
                payload={"goal": "Create delivery.txt. You may use PowerShell."},
            ),
            [],
        )
        explicit = project_forbidden_tools(
            payload={"forbidden_tools": ["powershell", "bash"]},
            goal_text="No shell",
            must=[],
            must_not=[],
        )
        self.assertEqual(explicit, ["powershell", "bash"])
        with self.assertRaises(AppError):
            project_forbidden_tools(payload={"forbidden_tools": ["powershell", "powershell"]})
        with self.assertRaises(AppError):
            project_forbidden_tools(payload={"forbidden_tools": ["powershell", ""]})
        with self.assertRaises(AppError):
            project_forbidden_tools(payload={"forbidden_tools": "powershell"})
        with self.assertRaises(AppError):
            project_forbidden_tools(payload={"forbidden_tools": ["tool"] * 33})

    def test_worker_charter_keeps_task_instruction_and_goal_constraints(self):
        charter = worker_charter_for_task(
            goal={
                "desired_outcome": "Create two files. You may use PowerShell.",
                "boundaries": {
                    "must": ["Stay inside the assigned workspace"],
                    "must_not": ["Do not use the network"],
                },
                "budget": {"wall_sec": 30, "max_reworks": 0},
                "forbidden_tools": ["bash"],
            },
            task={
                "title": "Prepare",
                "inputs": {"instruction": "Create prep.txt"},
                "done_when": {"artifacts": ["prep.txt"]},
            },
        )
        self.assertEqual(charter["goal"], "Create prep.txt")
        self.assertNotIn("Create two files", charter["goal"])
        self.assertNotIn("PowerShell", charter["goal"])
        self.assertEqual(charter["must"], ["Stay inside the assigned workspace"])
        self.assertEqual(charter["must_not"], ["Do not use the network"])
        self.assertEqual(charter["forbidden_tools"], ["bash"])
        self.assertEqual(charter["timeout_sec"], 30)
        self.assertEqual(charter["max_redos"], 0)
        with_inputs = worker_charter_for_task(
            goal={"desired_outcome": "x", "boundaries": {"must": [], "must_not": []}},
            task={
                "title": "Deliver",
                "inputs": {"instruction": "Create delivery.txt", "input_files": ["prep.txt"]},
                "done_when": {"artifacts": ["delivery.txt"]},
            },
        )
        self.assertEqual(with_inputs["input_files"], ["prep.txt"])

    def test_windows_dispatch_uses_task_instruction_and_forbidden_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            planner = LeadAdapterPlanner(_FakeLead(), cwd=root / "lead")
            app = CollabApplication(root / "app", backend=backend, planner=planner)
            body = _request()
            body["goal"] = "Create two files. Use a file-writing tool only. You may use PowerShell. No shell."
            body["forbidden_tools"] = ["powershell", "bash", "shell"]
            opened = app.submit(body)
            self.assertEqual(app.status(opened["goal_id"])["goal"].get("forbidden_tools"), ["powershell", "bash", "shell"])
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "worker_running", first)
            task = app.status(opened["goal_id"])["tasks"][0]
            store = Store(controller_state)
            try:
                job = store.get(task["run_id"])
            finally:
                store.db.close()
            self.assertEqual(job["charter"]["goal"], "Create prep.txt")
            self.assertNotIn("Create two files", job["charter"]["goal"])
            self.assertEqual(job["charter"]["forbidden_tools"], ["powershell", "bash", "shell"])
            self.assertEqual(job["charter"]["artifacts"], ["prep.txt"])
            self.assertEqual(job["charter"]["must"], ["Stay inside the assigned workspace"])
            self.assertEqual(job["charter"]["must_not"], ["Do not use network or system tools"])
            self.assertEqual(job["charter"]["timeout_sec"], 30)
            self.assertEqual(job["charter"]["max_redos"], 0)

    def test_submit_rejects_invalid_forbidden_tools_and_ignores_shell_wording(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            wording = _request()
            wording["goal"] = "Create delivery.txt. You may use PowerShell."
            opened = app.submit(wording)
            self.assertIsNone(app.status(opened["goal_id"])["goal"].get("forbidden_tools"))
            dup = _request()
            dup["idempotency_key"] = "pilot-dup"
            dup["forbidden_tools"] = ["powershell", "powershell"]
            with self.assertRaises(AppError):
                app.submit(dup)

    def test_goal_schema_validates_forbidden_tools_contract(self):
        validator = Draft202012Validator(GOAL_SCHEMA)
        self.assertIn("forbidden_tools", GOAL_SCHEMA["properties"])
        self.assertEqual(GOAL_SCHEMA["properties"]["forbidden_tools"]["maxItems"], 32)
        self.assertTrue(GOAL_SCHEMA["properties"]["forbidden_tools"]["uniqueItems"])
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            body = _request()
            body["forbidden_tools"] = ["powershell", "bash"]
            opened = app.submit(body)
            goal = app.status(opened["goal_id"])["goal"]
        validator.validate(goal)
        self.assertEqual(goal["forbidden_tools"], ["powershell", "bash"])
        without = dict(goal)
        without.pop("forbidden_tools")
        validator.validate(without)
        extra = dict(goal)
        extra["not_a_contract_field"] = "nope"
        self.assertTrue(list(validator.iter_errors(extra)))
        dup = dict(goal)
        dup["forbidden_tools"] = ["powershell", "powershell"]
        self.assertTrue(list(validator.iter_errors(dup)))
        empty = dict(goal)
        empty["forbidden_tools"] = [""]
        self.assertTrue(list(validator.iter_errors(empty)))
        too_many = dict(goal)
        too_many["forbidden_tools"] = [f"tool-{i}" for i in range(33)]
        self.assertTrue(list(validator.iter_errors(too_many)))

    def test_external_inputs_reach_charter_and_absent_has_no_key(self):
        digest = "ab" * 32
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = str((root / "not-created.bin").resolve())
            pin = {"path": path, "sha256": digest}
            self.assertFalse(Path(path).exists())
            backend = _RecordingBackend()
            app = CollabApplication(root / "rec", backend=backend)
            body = _request()
            body["external_inputs"] = [dict(pin)]
            opened = app.submit(body)
            goal = app.status(opened["goal_id"])["goal"]
            self.assertEqual(goal.get("external_inputs"), [pin])
            Draft202012Validator(GOAL_SCHEMA).validate(goal)
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "task_finished", first)
            self.assertEqual(backend.starts[0]["charter"]["external_inputs"], [pin])
            charter = worker_charter_for_task(
                goal=goal,
                task={
                    "title": "t",
                    "inputs": {"instruction": "read the pin"},
                    "done_when": {"artifacts": ["delivery.txt"]},
                },
            )
            self.assertEqual(charter["external_inputs"], [pin])
            passed = WindowsSupervisedExecutionBackend._charter(
                title="t",
                instruction="read the pin",
                artifacts=["delivery.txt"],
                charter=charter,
            )
            self.assertEqual(passed["external_inputs"], [pin])

            plain = _request()
            plain["idempotency_key"] = "pilot-absent"
            opened_plain = app.submit(plain)
            goal_plain = app.status(opened_plain["goal_id"])["goal"]
            self.assertNotIn("external_inputs", goal_plain)
            charter_plain = worker_charter_for_task(
                goal=goal_plain,
                task={"title": "t", "inputs": {"instruction": "x"}, "done_when": {}},
            )
            self.assertNotIn("external_inputs", charter_plain)
            self.assertEqual(project_external_inputs({}), [])
            self.assertEqual(project_external_inputs({"external_inputs": None}), [])

            empty = _request()
            empty["idempotency_key"] = "pilot-empty-pins"
            empty["external_inputs"] = []
            opened_empty = app.submit(empty)
            self.assertNotIn("external_inputs", app.status(opened_empty["goal_id"])["goal"])

            inproc = CollabApplication(root / "inproc")
            in_body = _request()
            in_body["idempotency_key"] = "pilot-inproc"
            in_body["external_inputs"] = [dict(pin)]
            opened_in = inproc.submit(in_body)
            tick = inproc.coordinator.process_goal(opened_in["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            self.assertEqual(inproc.status(opened_in["goal_id"])["state"], "completed")

    def test_submit_rejects_invalid_external_inputs(self):
        digest = "cd" * 32
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = str((root / "pinned.bin").resolve())
            app = CollabApplication(root / "app")
            cases = [
                [{"path": "relative.txt", "sha256": digest}],
                [{"path": path, "sha256": digest, "note": "extra"}],
                {"path": path, "sha256": digest},
                [{"path": path, "sha256": "abcd"}],
                [{"path": path, "sha256": digest}] * 9,
                [{"path": path, "sha256": digest}, {"path": path, "sha256": "ef" * 32}],
                [{"path": "", "sha256": digest}],
            ]
            for index, bad in enumerate(cases):
                body = _request()
                body["idempotency_key"] = f"pilot-bad-{index}"
                body["external_inputs"] = bad
                with self.assertRaises(AppError) as cm:
                    app.submit(body)
                self.assertEqual(cm.exception.code, "invalid_external_inputs", bad)
            self.assertEqual(app.list_requests()["requests"], [])
            schema = GOAL_SCHEMA["properties"]["external_inputs"]
            self.assertEqual(schema["maxItems"], 8)
            self.assertTrue(schema["items"]["additionalProperties"] is False)
            self.assertEqual(set(schema["items"]["required"]), {"path", "sha256"})

    def test_successor_reads_direct_dep_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _RecordingBackend()
            app = CollabApplication(td, planner=LeadAdapterPlanner(_FakeLead(), cwd=td), backend=backend)
            opened = app.submit(_request())
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "task_finished", first)
            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(second["action"], "task_finished", second)
            self.assertEqual(len(backend.starts), 2)
            self.assertEqual(backend.starts[1]["files"].get("prep.txt"), "produced by Prepare\n")
            self.assertEqual(backend.starts[1]["charter"].get("input_files"), ["prep.txt"])
            self.assertEqual(backend.starts[1]["instruction"], "Create delivery.txt")
            deliver = next(t for t in app.status(opened["goal_id"])["tasks"] if t["title"] == "Deliver")
            self.assertTrue((Path(backend.starts[1]["directory"]) / "prep.txt").is_file())
            self.assertEqual(deliver["inputs"]["instruction"], "Create delivery.txt")

    def test_handoff_rejects_same_named_source_outside_run_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "run"
            workspace.mkdir()
            (workspace / "a.txt").write_text("inside", encoding="utf-8")
            outside = root / "a.txt"
            outside.write_text("outside", encoding="utf-8")
            with self.assertRaises(HandoffError):
                collect_direct_dep_artifacts(
                    {"depends_on": ["a"]},
                    [{"task_id": "a", "status": "succeeded",
                      "expected_artifacts": ["a.txt"],
                      "result": {"workspace": str(workspace), "artifacts": [str(outside)]}}],
                )

    def test_handoff_retry_preserves_identical_file_and_rejects_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            artifact = source / "a.txt"
            artifact.write_text("accepted", encoding="utf-8")
            items = [{"relative": "a.txt", "source": artifact,
                      "workspace": str(source), "from_task": "a"}]
            dest = root / "dest"
            self.assertEqual(stage_handoff_files(items, dest), stage_handoff_files(items, dest))
            (dest / "a.txt").write_text("new worker output", encoding="utf-8")
            with self.assertRaises(HandoffError):
                stage_handoff_files(items, dest)
            self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "new worker output")

    def test_failed_dependency_does_not_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            leaked = root / "failed" / "secret.txt"
            leaked.parent.mkdir()
            leaked.write_text("should not copy\n", encoding="utf-8")
            items = collect_direct_dep_artifacts(
                {"task_id": "task_b", "depends_on": ["task_a"]},
                [
                    {
                        "task_id": "task_a",
                        "status": "failed",
                        "expected_artifacts": ["secret.txt"],
                        "result": {"ok": False, "artifacts": [str(leaked)]},
                    }
                ],
            )
            self.assertEqual(items, [])
            dest = root / "successor"
            self.assertEqual(stage_handoff_files(items, dest), [])
            self.assertFalse((dest / "secret.txt").exists())
            backend = _FailFirstBackend()
            app = CollabApplication(td, planner=LeadAdapterPlanner(_FakeLead(), cwd=td), backend=backend)
            opened = app.submit(_request())
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first.get("state"), "failed", first)
            self.assertEqual(app.status(opened["goal_id"])["tasks"][0]["status"], "failed")
            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(second.get("action"), "terminal", second)
            self.assertEqual(second.get("state"), "failed", second)
            self.assertEqual(len(backend.starts), 1)
            deliver = next(t for t in app.status(opened["goal_id"])["tasks"] if t["title"] == "Deliver")
            self.assertEqual(deliver["status"], "queued")
            self.assertFalse((Path(td) / "workspaces" / opened["goal_id"] / deliver["task_id"] / "prep.txt").exists())

    def test_handoff_rejects_illegal_paths_and_overwrite(self):
        with self.assertRaises(HandoffError):
            safe_relative_name("../x.txt")
        with self.assertRaises(HandoffError):
            safe_relative_name("/tmp/x.txt")
        with self.assertRaises(HandoffError):
            safe_relative_name(r"C:\Windows\x.txt")
        with self.assertRaises(HandoffError):
            safe_relative_name(".env")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "a" / "prep.txt"
            src.parent.mkdir()
            src.write_text("from A\n", encoding="utf-8")
            dest = root / "b"
            dest.mkdir()
            (dest / "prep.txt").write_text("new-result\n", encoding="utf-8")
            with self.assertRaises(HandoffError):
                stage_handoff_files([{"relative": "prep.txt", "source": src, "from_task": "a"}], dest)
            self.assertEqual((dest / "prep.txt").read_text(encoding="utf-8"), "new-result\n")
            extra = root / "a" / "extra.txt"
            extra.write_text("undeclared\n", encoding="utf-8")
            items = collect_direct_dep_artifacts(
                {"depends_on": ["t1"]},
                [
                    {
                        "task_id": "t1",
                        "status": "succeeded",
                        "expected_artifacts": ["prep.txt"],
                        "result": {"workspace": str(src.parent), "artifacts": [str(src), str(extra)]},
                    }
                ],
            )
            self.assertEqual([row["relative"] for row in items], ["prep.txt"])
            src2 = root / "a2" / "prep.txt"
            src2.parent.mkdir()
            src2.write_text("A2\n", encoding="utf-8")
            with self.assertRaises(HandoffError):
                collect_direct_dep_artifacts(
                    {"depends_on": ["t1", "t2"]},
                    [
                        {
                            "task_id": "t1",
                            "status": "succeeded",
                            "expected_artifacts": ["prep.txt"],
                            "result": {"workspace": str(src.parent), "artifacts": [str(src)]},
                        },
                        {
                            "task_id": "t2",
                            "status": "succeeded",
                            "expected_artifacts": ["prep.txt"],
                            "result": {"workspace": str(src2.parent), "artifacts": [str(src2)]},
                        },
                    ],
                )
            outside = root / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            link = root / "a" / "link.txt"
            try:
                link.symlink_to(outside)
                linked = True
            except OSError:
                linked = False
            if linked:
                with self.assertRaises(HandoffError):
                    stage_handoff_files(
                        [{"relative": "link.txt", "source": link, "from_task": "a"}],
                        root / "d",
                    )

    def test_windows_backend_copies_staged_inputs_into_uuid_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staging = root / "staging"
            staging.mkdir()
            (staging / "prep.txt").write_text("from A\n", encoding="utf-8")
            (staging / "secrets.env").write_text("nope\n", encoding="utf-8")
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            launched = backend.start_run(
                title="Deliver",
                directory=str(staging),
                instruction="Create delivery.txt",
                artifacts=["delivery.txt"],
                charter={
                    "goal": "Create delivery.txt",
                    "must": ["Stay inside the assigned workspace"],
                    "must_not": ["Do not use the network"],
                    "done_when": {"artifacts": ["delivery.txt"]},
                    "input_files": ["prep.txt"],
                },
            )
            self.assertTrue(launched["ok"], launched)
            store = Store(controller_state)
            try:
                job = store.get(launched["run_id"])
            finally:
                store.db.close()
            workspace = Path(job["workspace"])
            self.assertNotEqual(workspace.resolve(), staging.resolve())
            self.assertEqual((workspace / "prep.txt").read_text(encoding="utf-8"), "from A\n")
            self.assertFalse((workspace / "secrets.env").exists())
            self.assertEqual(job["charter"]["input_files"], ["prep.txt"])
            self.assertEqual(job["charter"]["input_file_paths"], [str(workspace / "prep.txt")])

    def test_windows_successor_workspace_receives_accepted_dep_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            planner = LeadAdapterPlanner(_TwoStepLead(), cwd=root / "lead")
            app = CollabApplication(root / "app", backend=backend, planner=planner)
            opened = app.submit(_request())
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            self._complete_running_windows_file(
                app=app,
                client=client,
                controller_state=controller_state,
                goal_id=opened["goal_id"],
                rel="prep.txt",
                content="accepted A\n",
            )
            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(second["action"], "worker_running", second)
            deliver = next(t for t in app.status(opened["goal_id"])["tasks"] if t["title"] == "Deliver")
            store = Store(controller_state)
            try:
                job_b = store.get(deliver["run_id"])
            finally:
                store.db.close()
            self.assertEqual((Path(job_b["workspace"]) / "prep.txt").read_text(encoding="utf-8"), "accepted A\n")
            self.assertEqual(job_b["charter"].get("input_files"), ["prep.txt"])
            self.assertIn(str(Path(job_b["workspace"]) / "prep.txt"), job_b["charter"].get("input_file_paths") or [])

    def test_http_terminal_vocab_matches_status_contract(self):
        self.assertEqual(GOAL_HTTP_TERMINAL, frozenset({"completed", "failed", "cancelled"}))
        self.assertEqual(TASK_HTTP_TERMINAL, frozenset({"succeeded", "failed", "cancelled"}))

    def test_http_api_requires_configured_bearer_and_runs_tick(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            server = CollabHttpServer(("127.0.0.1", 0), app, api_token="test-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(f"{base}/v1/requests", timeout=2)
                self.assertEqual(cm.exception.code, 401)
                body = _request()
                body["title"] = "应用入口烟测"
                raw = json.dumps(body).encode("utf-8")
                req = urllib.request.Request(
                    f"{base}/v1/requests",
                    data=raw,
                    method="POST",
                    headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"},
                )
                opened = json.loads(urllib.request.urlopen(req, timeout=2).read())
                tick = urllib.request.Request(
                    f"{base}/v1/coordinator/tick",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"},
                )
                self.assertTrue(json.loads(urllib.request.urlopen(tick, timeout=2).read())["ok"])
                encoded_goal_id = urllib.parse.quote(opened["goal_id"], safe="")
                status_req = urllib.request.Request(
                    f"{base}/v1/requests/{encoded_goal_id}",
                    headers={"Authorization": "Bearer test-token"},
                )
                status = json.loads(urllib.request.urlopen(status_req, timeout=2).read())
                self.assertEqual(status["state"], "completed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_split_task_musts_defers_later_task_only_constraints(self):
        task_a = {"task_id": "task_a", "title": "Create seed.json"}
        task_b = {"task_id": "task_b", "title": "Create derived.json from seed.json"}
        musts = [
            "Write only the assigned task workspace",
            "Create seed.json in Task A before derived.json in Task B",
            "Task B must read seed.json with a file-read tool",
        ]
        scoped, deferred = split_task_musts(musts=musts, task=task_a, siblings=[task_a, task_b])
        self.assertEqual(scoped, [
            "Write only the assigned task workspace",
            "Create seed.json in Task A before derived.json in Task B",
        ])
        self.assertEqual(deferred, ["Task B must read seed.json with a file-read tool"])
        scoped_b, deferred_b = split_task_musts(musts=musts, task=task_b, siblings=[task_a, task_b])
        self.assertIn("Task B must read seed.json with a file-read tool", scoped_b)
        self.assertEqual(deferred_b, [])
        accept = task_acceptance_criteria({
            "expected_artifacts": ["seed.json"],
            "done_when": {"artifacts": ["seed.json"]},
        })
        self.assertEqual(accept["artifacts"], ["seed.json"])
        self.assertTrue(lead_error_retryable(type("E", (Exception,), {"code": "timeout"})("lead call timed out")))

    def test_task_review_does_not_require_later_task_goal_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            lead = _RecordingLead()
            planner = LeadAdapterPlanner(lead, cwd=td)
            snap = {
                "goal_id": "goal_two",
                "goal": {
                    "desired_outcome": "Plan exactly two Tasks. Task B must read seed.json.",
                    "boundaries": {
                        "must": [
                            "Write only the assigned task workspace",
                            "Create seed.json in Task A before derived.json in Task B",
                            "Task B must read seed.json with a file-read tool",
                        ],
                        "must_not": ["Do not use powershell, bash, or shell"],
                    },
                    "acceptance": {
                        "artifacts": ["seed.json", "derived.json"],
                        "text": "Task B tool trace includes a completed read of seed.json",
                    },
                },
                "tasks": [
                    {
                        "task_id": "task_a",
                        "title": "Create seed.json",
                        "status": "running",
                        "expected_artifacts": ["seed.json"],
                        "done_when": {"artifacts": ["seed.json"]},
                        "inputs": {"instruction": "Create only seed.json"},
                        "run_id": "run-a",
                    },
                    {
                        "task_id": "task_b",
                        "title": "Create derived.json from seed.json",
                        "status": "queued",
                        "expected_artifacts": ["derived.json"],
                        "depends_on": ["task_a"],
                    },
                ],
            }
            out = planner.decide_action(
                snap,
                snap["tasks"][0],
                {"kind": "review", "payload": {"artifacts": {"seed.json": {"bytes": 29}}}},
            )
            self.assertEqual(out["verdict"], "pass")
            req = next(row for row in lead.requests if row.get("kind") == "review")
            self.assertEqual(req["task_goal"], "Create only seed.json")
            self.assertEqual(req["acceptance_criteria"]["artifacts"], ["seed.json"])
            self.assertNotIn("derived.json", req["acceptance_criteria"]["artifacts"])
            self.assertEqual(req["extra"]["goal_acceptance"]["artifacts"], ["seed.json", "derived.json"])
            self.assertIn("Task B must read seed.json with a file-read tool", req["extra"]["goal_must"])
            self.assertIn("Task B must read seed.json with a file-read tool", req["extra"]["deferred_must"])
            self.assertNotIn("Task B must read seed.json with a file-read tool", req["authorized_scope"])
            self.assertEqual(req["extra"]["review_scope"], "task")
            self.assertIn("not Goal completion", req["extra"]["allow_hint"])

    def test_pending_review_retries_after_lead_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            lead = _TimeoutThenPassLead()
            planner = LeadAdapterPlanner(lead, cwd=root / "lead")
            app = CollabApplication(root / "app", backend=backend, planner=planner)
            opened = app.submit(_request())
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            task = app.status(opened["goal_id"])["tasks"][0]
            run_id = task["run_id"]
            store = Store(controller_state)
            try:
                job = store.get(run_id)
            finally:
                store.db.close()
            sid = job["session_id"]
            (Path(job["workspace"]) / "prep.txt").write_text("accepted\n", encoding="utf-8")
            client.status[sid] = {"type": "idle"}
            client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
            self._force_controller_scan(controller_state, run_id)
            app.coordinator.process_goal(opened["goal_id"])
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "decision_required", first)
            self.assertEqual((first.get("lead_error") or {}).get("code"), "timeout")
            pending = app.status(opened["goal_id"])["pending_decisions"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["kind"], "artifact_review")
            self.assertEqual((pending[0].get("lead_error") or {}).get("code"), "timeout")
            retry = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(retry["action"], "decision_resolved", retry)
            self.assertEqual(lead.review_calls, 2)
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "task_finished")

    def test_quota_lead_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            client = _TeleAgentClient()
            controller_state = root / "controller"
            backend = WindowsSupervisedExecutionBackend(state_dir=controller_state, client=client)
            lead = _QuotaLead()
            planner = LeadAdapterPlanner(lead, cwd=root / "lead")
            app = CollabApplication(root / "app", backend=backend, planner=planner)
            opened = app.submit(_request())
            self.assertEqual(app.coordinator.process_goal(opened["goal_id"])["action"], "worker_running")
            task = app.status(opened["goal_id"])["tasks"][0]
            run_id = task["run_id"]
            store = Store(controller_state)
            try:
                job = store.get(run_id)
            finally:
                store.db.close()
            sid = job["session_id"]
            (Path(job["workspace"]) / "prep.txt").write_text("accepted\n", encoding="utf-8")
            client.status[sid] = {"type": "idle"}
            client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
            self._force_controller_scan(controller_state, run_id)
            app.coordinator.process_goal(opened["goal_id"])
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first["action"], "decision_required", first)
            self.assertFalse((first.get("lead_error") or {}).get("retryable"))
            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(second["action"], "decision_required", second)
            self.assertEqual(lead.review_calls, 1)
            self.assertEqual(len(app.status(opened["goal_id"])["pending_decisions"]), 1)

    def test_contaminated_artifact_fails_successful_collect(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_ContentBackend(_DIRTY_ARTIFACT))
            opened = app.submit(_request())
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "failed")
            task = status["tasks"][0]
            self.assertEqual(task["status"], "failed")
            error = task["result"]["error"]
            self.assertTrue(error.startswith("artifact_contaminated:"), error)
            self.assertIn("AI生成x1", error)
            self.assertIn("U+200Bx3", error)
            self.assertIn("delivery.txt", task["result"]["artifact_contamination"])

    def test_clean_artifact_collect_still_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_ContentBackend("hello\n"))
            opened = app.submit(_request())
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["tasks"][0]["status"], "succeeded")
            self.assertNotIn("artifact_contamination", status["tasks"][0]["result"])

    def test_allow_aigc_marks_on_goal_acceptance_opts_out(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_ContentBackend(_DIRTY_ARTIFACT))
            body = _request()
            body["acceptance"]["allow_aigc_marks"] = True
            opened = app.submit(body)
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            status = app.status(opened["goal_id"])
            self.assertEqual(status["state"], "completed", status.get("tasks"))
            self.assertEqual(status["tasks"][0]["status"], "succeeded")

    def test_content_gate_opt_out_inputs_relative_path_and_charter_passthrough(self):
        self.assertFalse(allows_aigc_marks({"acceptance": {"allow_aigc_marks": "true"}}, {}))
        self.assertTrue(allows_aigc_marks({}, {"inputs": {"allow_aigc_marks": True}}))
        charter = worker_charter_for_task(
            goal={
                "desired_outcome": "x",
                "boundaries": {"must": [], "must_not": []},
                "acceptance": {"artifacts": ["a.txt"], "allow_aigc_marks": True},
            },
            task={
                "title": "t",
                "inputs": {"instruction": "do"},
                "done_when": {"artifacts": ["a.txt"]},
            },
        )
        self.assertIs(charter["allow_aigc_marks"], True)
        passed = WindowsSupervisedExecutionBackend._charter(
            title="t",
            instruction="i",
            artifacts=["a.txt"],
            charter={"allow_aigc_marks": True, "forbidden_tools": ["write"]},
        )
        self.assertIs(passed["allow_aigc_marks"], True)
        self.assertEqual(passed["forbidden_tools"], ["write"])
        absent = WindowsSupervisedExecutionBackend._charter(
            title="t",
            instruction="i",
            artifacts=["a.txt"],
            charter={},
        )
        self.assertNotIn("allow_aigc_marks", absent)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "hello.txt").write_text("AI 生成\n", encoding="utf-8")
            blocked = gate_collected_artifacts(
                {"ok": True, "artifacts": ["hello.txt", "missing.txt"], "workspace": str(root)}
            )
            self.assertFalse(blocked["ok"])
            self.assertTrue(blocked["error"].startswith("artifact_contaminated:"))
            self.assertIn("hello.txt", blocked["artifact_contamination"])
            self.assertNotIn("missing.txt", blocked["artifact_contamination"])
            allowed = gate_collected_artifacts(
                {"ok": True, "artifacts": ["hello.txt"], "workspace": str(root)},
                allow_aigc_marks=True,
            )
            self.assertTrue(allowed["ok"])
            self.assertNotIn("error", allowed)
        summary = review_contamination_summary(
            {
                "artifacts": {
                    "hello.txt": {
                        "contamination": {
                            "contaminated": True,
                            "aigc_marks": {"AI生成": 1},
                            "invisible": {"U+200B": 2},
                        }
                    }
                }
            }
        )
        self.assertEqual(summary, "CONTAMINATED hello.txt: AI生成x1, U+200Bx2")

    def test_review_decision_title_marks_contamination(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_ContaminatedReviewBackend())
            opened = app.submit(_request())
            first = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(first.get("action"), "worker_running", first)
            second = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(second.get("action"), "decision_required", second)
            pending = app.status(opened["goal_id"])["pending_decisions"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["title"], "TeleAgent review (CONTAMINATED)")
            self.assertEqual(
                pending[0]["details"]["summary"],
                "CONTAMINATED hello.txt: AI生成x1, U+200Bx4, U+200Dx4",
            )

    def _raising_app(self, td: str, exc: BaseException) -> tuple[CollabApplication, _RaisingBackend]:
        backend = _RaisingBackend(exc)
        app = CollabApplication(td, backend=backend)
        app.layer.get_goal = lambda goal_id: _decision_goal()
        return app, backend

    def test_resolve_contaminated_decision_is_409_with_counts(self):
        findings = {
            "label.txt": {
                "contaminated": True,
                "encoding": "utf-8",
                "aigc_marks": {"AI生成": 1},
                "invisible": {"U+200B": 2},
                "first_offset": 3,
                "preview": "AI生成 secret text",
                "scanned": True,
            },
            "clean.txt": {
                "contaminated": False,
                "encoding": "utf-8",
                "aigc_marks": {},
                "invisible": {},
            },
        }
        summary = "CONTAMINATED label.txt: AI生成x1, U+200Bx2"
        exc = ArtifactContaminatedError(summary, findings)
        with tempfile.TemporaryDirectory() as td:
            app, backend = self._raising_app(td, exc)
            with self.assertRaises(AppError) as cm:
                app.resolve("g1", "d1", {"verdict": "pass"})
        err = cm.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "artifact_contaminated")
        self.assertEqual(
            str(err),
            "artifact contaminated: "
            + summary
            + ". Fail this review so the worker can redo it, or re-open the request "
            "with acceptance.allow_aigc_marks=true if the marks are intended.",
        )
        self.assertEqual(err.extra["hint"], "allow_aigc_marks")
        label = err.extra["contamination"]["label.txt"]
        self.assertEqual(label["aigc_marks"], {"AI生成": 1})
        self.assertEqual(label["invisible"], {"U+200B": 2})
        self.assertEqual(label["encoding"], "utf-8")
        self.assertNotIn("preview", label)
        self.assertNotIn("first_offset", label)
        self.assertNotIn("secret", json.dumps(err.extra["contamination"]))
        self.assertEqual(set(err.extra["contamination"]["clean.txt"]), {"aigc_marks", "invisible", "encoding"})
        self.assertEqual(backend.calls[0][0], "backend-req")
        self.assertEqual(backend.calls[0][1], "pass")

        class DuckContaminated(ValueError):
            """Same shape, different class name: detected via ``findings``."""

            def __init__(self) -> None:
                self.summary = "CONTAMINATED duck.txt: AI生成x1"
                self.findings = {
                    "duck.txt": {
                        "aigc_marks": {"AI生成": 1, "note": "not-a-count"},
                        "invisible": {"U+200B": True},
                        "encoding": "utf-8",
                        "preview": "hidden",
                    }
                }
                super().__init__("plain value error text")

        with tempfile.TemporaryDirectory() as td:
            app, _backend = self._raising_app(td, DuckContaminated())
            with self.assertRaises(AppError) as cm:
                app.resolve("g1", "d1", {"verdict": "pass"})
        self.assertEqual(cm.exception.code, "artifact_contaminated")
        duck = cm.exception.extra["contamination"]["duck.txt"]
        self.assertEqual(duck["aigc_marks"], {"AI生成": 1})
        self.assertEqual(duck["invisible"], {})
        self.assertNotIn("hidden", json.dumps(cm.exception.extra))
        self.assertNotIn("plain value error text", str(cm.exception))

    def test_resolve_other_value_error_names_the_reason(self):
        with tempfile.TemporaryDirectory() as td:
            app, _backend = self._raising_app(td, ValueError("Worker is still running"))
            with self.assertRaises(AppError) as cm:
                app.resolve("g1", "d1", {"verdict": "pass"})
        err = cm.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "worker_decision_rejected")
        self.assertEqual(str(err), "worker decision rejected: Worker is still running")
        self.assertEqual(err.extra, {})

        secret = "blocked token=abc123sk-abcdefghijklmnopqrstuvwxyz012345 tail"
        with tempfile.TemporaryDirectory() as td:
            app, _backend = self._raising_app(td, ValueError(secret))
            with self.assertRaises(AppError) as cm:
                app.resolve("g1", "d1", {"verdict": "pass"})
        self.assertEqual(cm.exception.code, "worker_decision_rejected")
        self.assertIn("worker decision rejected: blocked [redacted] tail", str(cm.exception))
        self.assertNotIn("abc123", str(cm.exception))

    def test_resolve_backend_error_keeps_sanitized_message(self):
        exc = BackendError(
            BackendStatus.FAILED,
            "illegal verdict token=abc123sk-abcdefghijklmnopqrstuvwxyz012345 for review",
            capability="resolve_decision",
        )
        with tempfile.TemporaryDirectory() as td:
            app, _backend = self._raising_app(td, exc)
            with self.assertRaises(AppError) as cm:
                app.resolve("g1", "d1", {"verdict": "nope"})
        err = cm.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "worker_decision_failed")
        self.assertEqual(
            str(err),
            "worker decision failed: illegal verdict [redacted] for review",
        )
        self.assertNotIn("abc123", str(err))

    def test_resolve_unexpected_exception_is_type_name_only(self):
        class Boom(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as td:
            app, _backend = self._raising_app(td, Boom("token=should-not-leak secret text"))
            with self.assertRaises(AppError) as cm:
                app.resolve("g1", "d1", {"verdict": "pass"})
        err = cm.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "worker_decision_failed")
        self.assertEqual(str(err), "worker decision failed: Boom")
        self.assertNotIn("should-not-leak", str(err))
        self.assertNotIn("token", str(err))

    def test_http_contaminated_decision_body_keeps_counts_and_core_keys(self):
        findings = {
            "label.txt": {
                "encoding": "utf-8",
                "aigc_marks": {"AI生成": 1},
                "invisible": {},
                "preview": "do not leak this body",
            }
        }
        exc = ArtifactContaminatedError("CONTAMINATED label.txt: AI生成x1", findings)
        with tempfile.TemporaryDirectory() as td:
            app, _backend = self._raising_app(td, exc)

            def resolve_with_override(goal_id, decision_id, payload):
                try:
                    return CollabApplication.resolve(app, goal_id, decision_id, payload)
                except AppError as err:
                    err.extra = {
                        **err.extra,
                        "ok": True,
                        "code": "not-this",
                        "error": "not-this",
                    }
                    raise

            app.resolve = resolve_with_override  # type: ignore[method-assign]
            server = CollabHttpServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                req = urllib.request.Request(
                    f"{base}/v1/requests/g1/decisions/d1",
                    data=json.dumps({"verdict": "pass"}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=2)
                self.assertEqual(cm.exception.code, 409)
                body = json.loads(cm.exception.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "artifact_contaminated")
        self.assertTrue(str(body["error"]).startswith("artifact contaminated: CONTAMINATED label.txt: AI生成x1."))
        self.assertEqual(body["hint"], "allow_aigc_marks")
        self.assertEqual(body["contamination"]["label.txt"]["aigc_marks"], {"AI生成": 1})
        self.assertEqual(body["contamination"]["label.txt"]["invisible"], {})
        self.assertEqual(body["contamination"]["label.txt"]["encoding"], "utf-8")
        self.assertNotIn("preview", body["contamination"]["label.txt"])
        self.assertNotIn("do not leak", json.dumps(body))
        self.assertNotEqual(body["error"], "not-this")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
