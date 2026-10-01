"""Scheduler-owned workspace for dependency handoff (issue #14).

AGY collect_result reports absolute artifact paths and no workspace. The
regression drives that production shape through AppCoordinator.
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
_TESTS_DIR = str(REPO / "tests")
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)

from desktop_lock_isolation import install_desktop_lock_isolation  # noqa: E402
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402
from execution_backend.linux_supervised_v1 import LinuxSupervisedExecutionBackend  # noqa: E402
from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend  # noqa: E402
from framework.app_service import CollabApplication, build_planning_request, validate_plan  # noqa: E402
from framework.artifact_handoff import (  # noqa: E402
    HandoffError,
    collect_direct_dep_artifacts,
    scheduler_task_workspace,
)
from framework.durable_api import DurableLayer  # noqa: E402
from test_contract_render import _write_fake_agy  # noqa: E402
from win_collab.core import Store  # noqa: E402

A_BODY = "alpha-from-A\n"
B_PREFIX = "beta:"
B_BODY = B_PREFIX + A_BODY


class _AbPlanner:
    name = "recording.ab_v1"

    def plan(self, goal_snapshot):
        request = build_planning_request(goal_snapshot)
        return validate_plan(
            {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "summary": "A then B",
                "tasks": [
                    {
                        "task_key": "A",
                        "title": "Write A",
                        "instruction": "Write a.txt",
                        "depends_on": [],
                        "artifacts": ["a.txt"],
                    },
                    {
                        "task_key": "B",
                        "title": "Write B",
                        "instruction": "Read a.txt and write b.txt",
                        "depends_on": ["A"],
                        "artifacts": ["b.txt"],
                    },
                ],
            },
            request,
        )


def _submit_body() -> dict:
    return {
        "idempotency_key": "handoff-ws-ab",
        "client_id": "test-suite",
        "title": "A then B",
        "goal": "Write a.txt, then b.txt from the handed-off file.",
        "boundaries": {
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
        },
        "acceptance": {"artifacts": ["b.txt"], "text": "b.txt repeats a.txt"},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }


def _drive(app: CollabApplication, goal_id: str, *, seconds: float = 8.0) -> dict:
    deadline = time.time() + seconds
    status = app.status(goal_id)
    while time.time() < deadline and status["state"] not in {"completed", "failed", "cancelled"}:
        app.coordinator.process_goal(goal_id)
        status = app.status(goal_id)
        if status["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    return status


class HandoffWorkspaceTests(unittest.TestCase):
    def test_agy_successor_receives_handoff_from_trusted_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake = _write_fake_agy(bin_dir)
            env = os.environ.copy()
            env.pop("AGY_AUTO_APPROVE", None)
            env.pop("COLLAB_AGY_AUTO_APPROVE", None)
            env["AGY_FAKE_HANDOFF"] = "1"
            env["AGY_FAKE_A_NAME"] = "a.txt"
            env["AGY_FAKE_A_BODY"] = A_BODY
            env["AGY_FAKE_B_NAME"] = "b.txt"
            env["AGY_FAKE_B_PREFIX"] = B_PREFIX
            backend = AntigravityCliExecutionBackend(
                bin_path=str(fake),
                environ=env,
                timeout_sec=10,
                poll_sec=0.05,
            )
            app = CollabApplication(root / "app", planner=_AbPlanner(), backend=backend)
            opened = app.submit(_submit_body())
            status = _drive(app, opened["goal_id"])
            self.assertEqual(status["state"], "completed", status)
            tasks = {row["title"]: row for row in status["tasks"]}
            task_a = tasks["Write A"]
            task_b = tasks["Write B"]
            self.assertEqual(task_a["status"], "succeeded", task_a)
            self.assertEqual(task_b["status"], "succeeded", task_b)
            self.assertEqual(task_a["result"].get("backend"), "antigravity.cli_v1")
            workspace_a = Path(task_a["workspace"])
            workspace_b = Path(task_b["workspace"])
            expected_a = app.coordinator.workspaces_root / opened["goal_id"] / task_a["task_id"]
            expected_b = app.coordinator.workspaces_root / opened["goal_id"] / task_b["task_id"]
            self.assertEqual(workspace_a.resolve(), expected_a.resolve())
            self.assertEqual(workspace_b.resolve(), expected_b.resolve())
            self.assertEqual((workspace_a / "a.txt").read_text(encoding="utf-8"), A_BODY)
            self.assertEqual((workspace_b / "a.txt").read_text(encoding="utf-8"), A_BODY)
            self.assertEqual((workspace_b / "b.txt").read_text(encoding="utf-8"), B_BODY)
            # Trusted binding wins over anything the backend result reports.
            self.assertEqual(Path(task_a["result"]["workspace"]).resolve(), workspace_a.resolve())
            self.assertEqual(Path(task_b["result"]["workspace"]).resolve(), workspace_b.resolve())
            # Production AGY collect does not invent workspace; the scheduler stamped it.
            self.assertTrue(all(Path(p).is_absolute() for p in task_a["result"]["artifacts"]))


class _AgyShapeBackend:
    """Absolute artifact paths, no workspace. The production AGY result shape."""

    backend_id = "fake.agy_shape_v1"

    def __init__(self) -> None:
        self.starts: list[str] = []
        self._runs: dict[str, list[str]] = {}

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for rel in artifacts or []:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if rel == "b.txt" and (root / "a.txt").is_file():
                body = "got:" + (root / "a.txt").read_text(encoding="utf-8")
            elif rel == "a.txt":
                body = A_BODY
            else:
                body = f"made-{rel}\n"
            path.write_text(body, encoding="utf-8")
            written.append(str(path.resolve()))
        run_id = f"shape-{len(self.starts) + 1}"
        self.starts.append(run_id)
        self._runs[run_id] = written
        return {"ok": True, "backend": self.backend_id, "run_id": run_id, "native_handle": run_id}

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": True}

    def collect_result(self, run_id):
        return {
            "ok": True,
            "backend": self.backend_id,
            "run_id": run_id,
            "artifacts": list(self._runs[run_id]),
        }

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


def _edit_task(app: CollabApplication, goal_id: str, task_id: str, mutate) -> None:
    with app.layer._rmw():
        snap = app.layer.goals[goal_id]
        found = None
        for task in snap["tasks"]:
            if str(task.get("task_id")) == task_id:
                found = task
                break
        if found is None:
            raise AssertionError(task_id)
        mutate(found)
        app.layer._persist_unlocked()


def _task_by_title(status: dict, title: str) -> dict:
    return next(row for row in status["tasks"] if row["title"] == title)


class LegacyAndContainmentTests(unittest.TestCase):
    def _open(self, td: str, backend: _AgyShapeBackend) -> tuple[CollabApplication, str]:
        app = CollabApplication(Path(td) / "app", planner=_AbPlanner(), backend=backend)
        opened = app.submit(_submit_body())
        first = app.coordinator.process_goal(opened["goal_id"])
        self.assertEqual(first.get("action"), "task_finished", first)
        return app, opened["goal_id"]

    def test_outside_agy_artifact_is_refused_even_if_result_workspace_points_there(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trusted = root / "trusted"
            trusted.mkdir()
            (trusted / "a.txt").write_text(A_BODY, encoding="utf-8")
            outside_dir = root / "outside"
            outside_dir.mkdir()
            outside = outside_dir / "a.txt"
            outside.write_text("OUTSIDE\n", encoding="utf-8")
            with self.assertRaises(HandoffError) as caught:
                collect_direct_dep_artifacts(
                    {"depends_on": ["a"]},
                    [
                        {
                            "task_id": "a",
                            "goal_id": "goal_x",
                            "status": "succeeded",
                            "workspace": str(trusted),
                            "expected_artifacts": ["a.txt"],
                            "result": {
                                "workspace": str(outside_dir),
                                "artifacts": [str(outside)],
                            },
                        }
                    ],
                    workspaces_root=root / "workspaces",
                )
            self.assertNotIn("unrecoverable", str(caught.exception))
            self.assertNotIn("OUTSIDE", str(caught.exception))

    def test_conflicting_result_workspace_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trusted = root / "trusted"
            trusted.mkdir()
            (trusted / "a.txt").write_text(A_BODY, encoding="utf-8")
            decoy = root / "decoy"
            decoy.mkdir()
            (decoy / "a.txt").write_text("DECOY\n", encoding="utf-8")
            items = collect_direct_dep_artifacts(
                {"depends_on": ["a"]},
                [
                    {
                        "task_id": "a",
                        "status": "succeeded",
                        "workspace": str(trusted),
                        "expected_artifacts": ["a.txt"],
                        "result": {
                            "workspace": str(decoy),
                            "artifacts": [str(trusted / "a.txt"), str(decoy / "a.txt")],
                        },
                    }
                ],
                workspaces_root=root / "workspaces",
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"].read_text(encoding="utf-8"), A_BODY)
            self.assertEqual(Path(items[0]["workspace"]).resolve(), trusted.resolve())

    def test_coordinator_refuses_outside_artifact_and_does_not_dispatch_successor(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _AgyShapeBackend()
            app, goal_id = self._open(td, backend)
            status = app.status(goal_id)
            task_a = _task_by_title(status, "Write A")
            outside_dir = Path(td) / "outside"
            outside_dir.mkdir()
            outside = outside_dir / "a.txt"
            outside.write_text("OUTSIDE\n", encoding="utf-8")

            def tamper(task: dict) -> None:
                result = dict(task["result"])
                result["workspace"] = str(outside_dir)
                result["artifacts"] = [str(outside)]
                task["result"] = result

            _edit_task(app, goal_id, task_a["task_id"], tamper)
            second = app.coordinator.process_goal(goal_id)
            # Pre-dispatch handoff failure returns finish_task as-is (no action).
            self.assertTrue(second.get("ok"), second)
            self.assertEqual(second.get("state"), "failed", second)
            self.assertEqual((second.get("task") or {}).get("status"), "failed", second)
            done = app.status(goal_id)
            task_b = _task_by_title(done, "Write B")
            self.assertEqual(task_b["status"], "failed", task_b)
            self.assertIn("dependency handoff failed:", task_b["result"]["error"])
            self.assertNotIn("OUTSIDE", task_b["result"]["error"])
            self.assertEqual(backend.starts, ["shape-1"])
            dest = app.coordinator.workspaces_root / goal_id / task_b["task_id"]
            if dest.exists():
                self.assertFalse((dest / "a.txt").exists())
            again = app.coordinator.process_goal(goal_id)
            self.assertEqual(again.get("action"), "terminal", again)
            self.assertEqual(backend.starts, ["shape-1"])

    def test_legacy_record_recovers_scheduler_directory(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _AgyShapeBackend()
            app, goal_id = self._open(td, backend)
            task_a = _task_by_title(app.status(goal_id), "Write A")
            expected = scheduler_task_workspace(
                app.coordinator.workspaces_root, goal_id, task_a["task_id"]
            )
            self.assertEqual((expected / "a.txt").read_text(encoding="utf-8"), A_BODY)

            def strip(task: dict) -> None:
                task.pop("workspace", None)
                result = dict(task.get("result") or {})
                result.pop("workspace", None)
                task["result"] = result

            _edit_task(app, goal_id, task_a["task_id"], strip)
            second = app.coordinator.process_goal(goal_id)
            self.assertEqual(second.get("action"), "task_finished", second)
            done = app.status(goal_id)
            self.assertEqual(done["state"], "completed", done)
            task_b = _task_by_title(done, "Write B")
            workspace_b = app.coordinator.workspaces_root / goal_id / task_b["task_id"]
            self.assertEqual((workspace_b / "a.txt").read_text(encoding="utf-8"), A_BODY)
            self.assertEqual((workspace_b / "b.txt").read_text(encoding="utf-8"), "got:" + A_BODY)

    def test_legacy_missing_directory_is_unrecoverable_and_not_redispatched(self):
        with tempfile.TemporaryDirectory() as td:
            backend = _AgyShapeBackend()
            app, goal_id = self._open(td, backend)
            task_a = _task_by_title(app.status(goal_id), "Write A")
            expected = scheduler_task_workspace(
                app.coordinator.workspaces_root, goal_id, task_a["task_id"]
            )
            decoy = Path(td) / "decoy"
            decoy.mkdir()
            (decoy / "a.txt").write_text("DECOY\n", encoding="utf-8")
            shutil.rmtree(expected)

            def strip(task: dict) -> None:
                task.pop("workspace", None)
                result = dict(task.get("result") or {})
                result["workspace"] = str(decoy)
                result["artifacts"] = [str(decoy / "a.txt")]
                task["result"] = result

            _edit_task(app, goal_id, task_a["task_id"], strip)
            second = app.coordinator.process_goal(goal_id)
            # Pre-dispatch handoff failure returns finish_task as-is (no action).
            self.assertTrue(second.get("ok"), second)
            self.assertEqual(second.get("state"), "failed", second)
            self.assertEqual((second.get("task") or {}).get("status"), "failed", second)
            done = app.status(goal_id)
            task_b = _task_by_title(done, "Write B")
            self.assertEqual(task_b["status"], "failed", task_b)
            self.assertIn(
                "dependency handoff failed: dependency workspace unrecoverable:",
                task_b["result"]["error"],
            )
            self.assertNotIn("DECOY", task_b["result"]["error"])
            self.assertEqual(done["state"], "failed")
            self.assertEqual(backend.starts, ["shape-1"])
            terminal = app.coordinator.process_goal(goal_id)
            self.assertEqual(terminal.get("action"), "terminal", terminal)
            self.assertEqual(backend.starts, ["shape-1"])
            self.assertEqual(_task_by_title(app.status(goal_id), "Write B")["status"], "failed")

    def test_legacy_symlink_workspace_is_unrecoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspaces = root / "workspaces"
            goal_id = "goal_sym"
            task_id = "task_sym"
            real = root / "real"
            real.mkdir()
            (real / "a.txt").write_text(A_BODY, encoding="utf-8")
            link = scheduler_task_workspace(workspaces, goal_id, task_id)
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is not available")
            with self.assertRaises(HandoffError) as caught:
                collect_direct_dep_artifacts(
                    {"depends_on": [task_id]},
                    [
                        {
                            "task_id": task_id,
                            "goal_id": goal_id,
                            "status": "succeeded",
                            "expected_artifacts": ["a.txt"],
                            "result": {"artifacts": [str(real / "a.txt")], "workspace": str(real)},
                        }
                    ],
                    workspaces_root=workspaces,
                )
            self.assertIn("dependency workspace unrecoverable:", str(caught.exception))

    def test_bind_task_run_persists_workspace_and_rejects_retarget(self):
        with tempfile.TemporaryDirectory() as td:
            layer = DurableLayer.open(td, use_cache=False)
            opened = layer.submit_goal(submit_key="ws-bind", title="t", desired_outcome="d")
            self.assertTrue(opened["ok"], opened)
            goal_id = opened["goal_id"]
            added = layer.add_child_task(
                goal_id,
                task={"task_id": "task_a", "title": "A", "status": "queued"},
            )
            self.assertTrue(added["ok"], added)
            started = layer.start_task(goal_id, "task_a")
            self.assertTrue(started["ok"], started)
            first = Path(td) / "one"
            second = Path(td) / "two"
            first.mkdir()
            second.mkdir()
            bound = layer.bind_task_run(goal_id, "task_a", run_id="run-1", workspace=str(first))
            self.assertTrue(bound["ok"], bound)
            self.assertEqual(bound["task"]["workspace"], str(first))
            same = layer.bind_task_run(goal_id, "task_a", run_id="run-1", workspace=str(first))
            self.assertTrue(same["ok"], same)
            omitted = layer.bind_task_run(goal_id, "task_a", run_id="run-1")
            self.assertTrue(omitted["ok"], omitted)
            self.assertEqual(omitted["task"]["workspace"], str(first))
            conflict = layer.bind_task_run(goal_id, "task_a", run_id="run-1", workspace=str(second))
            self.assertFalse(conflict["ok"], conflict)
            self.assertEqual(conflict["reason"], "workspace_binding_conflict")
            reloaded = DurableLayer.open(td, use_cache=False)
            stored = reloaded.get_goal(goal_id)["goal"]["tasks"][0]
            self.assertEqual(stored["workspace"], str(first))


class _SupervisedClient:
    base = "http://127.0.0.1:4398"
    instance_id = "fake-supervised-teleagent"

    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.status: dict = {}
        self.messages: dict = {}
        self.count = 0

    def call(self, method, path, body=None, workspace=None):
        if method == "POST" and path == "/session":
            self.count += 1
            sid = f"ses-{self.count}"
            self.status[sid] = {"type": "busy"}
            self.messages[sid] = []
            return {"id": sid, "permission": None if body is None else body.get("permission")}
        if path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            self.messages[sid].append({"info": {"role": "user"}})
            self.status[sid] = {"type": "busy"}
            return None
        if method == "GET" and path == "/permission":
            return copy.deepcopy(self.pending)
        if method == "GET" and path == "/question":
            return []
        if method == "GET" and path == "/session/status":
            return copy.deepcopy(self.status)
        if method == "GET" and path.endswith("/message"):
            return copy.deepcopy(self.messages[path.split("/")[2]])
        if path.startswith("/permission/") and path.endswith("/reply"):
            request_id = path.split("/")[2]
            self.pending = [row for row in self.pending if row.get("id") != request_id]
            return True
        if path.endswith("/abort"):
            self.status[path.split("/")[2]] = {"type": "idle"}
            return True
        raise AssertionError((method, path))


def _force_scan(state_dir: Path, run_id: str) -> None:
    store = Store(state_dir)
    try:
        with store.transaction():
            job = store.get(run_id)
            job["next_scan"] = 0
            store.save(job)
    finally:
        store.db.close()


class SupervisedWorkspaceBindingTests(unittest.TestCase):
    def setUp(self):
        install_desktop_lock_isolation(self)

    def _finish(self, backend_cls):
        root_td = tempfile.TemporaryDirectory()
        self.addCleanup(root_td.cleanup)
        root = Path(root_td.name)
        client = _SupervisedClient()
        state = root / "controller"
        backend = backend_cls(state_dir=state, client=client)
        app = CollabApplication(root / "app", backend=backend)
        opened = app.submit(
            {
                "idempotency_key": "sup-" + backend.backend_id,
                "client_id": "test-suite",
                "title": "Supervised",
                "goal": "Create the requested delivery artifact inside the assigned workspace.",
                "boundaries": {
                    "must": ["Stay inside the assigned workspace"],
                    "must_not": ["Do not use network or system tools"],
                },
                "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
                "budget": {"wall_sec": 30, "max_reworks": 0},
            }
        )
        goal_id = opened["goal_id"]
        self.assertEqual(app.coordinator.process_goal(goal_id)["action"], "worker_running")
        task = app.status(goal_id)["tasks"][0]
        expected = scheduler_task_workspace(app.coordinator.workspaces_root, goal_id, task["task_id"])
        self.assertEqual(Path(task["workspace"]).resolve(), expected.resolve())
        store = Store(state)
        try:
            job = store.get(task["run_id"])
        finally:
            store.db.close()
        self.assertNotEqual(Path(job["workspace"]).resolve(), expected.resolve())
        self.assertEqual(Path(job["scheduler_directory"]).resolve(), expected.resolve())
        sid = job["session_id"]
        client.pending.append(
            {
                "id": "perm-1",
                "sessionID": sid,
                "permission": "edit",
                "patterns": [str(Path(job["workspace"]) / "delivery.txt")],
                "metadata": {"diff": "create delivery"},
            }
        )
        _force_scan(state, task["run_id"])
        self.assertEqual(app.coordinator.process_goal(goal_id)["action"], "worker_running")
        decision_tick = app.coordinator.process_goal(goal_id)
        self.assertEqual(decision_tick["action"], "decision_required", decision_tick)
        decision = app.status(goal_id)["pending_decisions"][0]
        resolved = app.resolve(
            goal_id,
            decision["decision_id"],
            {"verdict": "once", "reason": "Bounded write in assigned workspace"},
        )
        self.assertTrue(resolved["ok"], resolved)
        (Path(job["workspace"]) / "delivery.txt").write_text("completed by TeleAgent\n", encoding="utf-8")
        client.status[sid] = {"type": "idle"}
        client.messages[sid].append({"info": {"role": "assistant", "finish": "stop"}, "parts": []})
        _force_scan(state, task["run_id"])
        self.assertEqual(app.coordinator.process_goal(goal_id)["action"], "worker_running")
        review_tick = app.coordinator.process_goal(goal_id)
        self.assertEqual(review_tick["action"], "decision_required", review_tick)
        review = app.status(goal_id)["pending_decisions"][0]
        resolved_review = app.resolve(
            goal_id,
            review["decision_id"],
            {"verdict": "pass", "reason": "Artifact content and tool evidence accepted"},
        )
        self.assertTrue(resolved_review["ok"], resolved_review)
        finished = app.status(goal_id)
        self.assertEqual(finished["state"], "completed", finished)
        done = finished["tasks"][0]
        self.assertEqual(Path(done["workspace"]).resolve(), expected.resolve())
        self.assertEqual(Path(done["result"]["workspace"]).resolve(), expected.resolve())
        self.assertEqual(
            (expected / "delivery.txt").read_text(encoding="utf-8"),
            "completed by TeleAgent\n",
        )
        self.assertTrue(
            any(Path(path).resolve() == (expected / "delivery.txt").resolve() for path in done["result"]["artifacts"])
        )
        return done

    def test_windows_and_linux_results_use_scheduler_workspace(self):
        for backend_cls in (WindowsSupervisedExecutionBackend, LinuxSupervisedExecutionBackend):
            with self.subTest(backend=backend_cls.backend_id):
                self._finish(backend_cls)


if __name__ == "__main__":
    unittest.main()
