from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from framework.app_service import (
    AppError,
    CollabApplication,
    CollabHttpServer,
    LeadAdapterPlanner,
    build_planning_request,
)


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


class _BrokenPlanner:
    name = "broken"

    def plan(self, goal_snapshot):
        raise RuntimeError("simulated planner outage")


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

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


class AppServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
