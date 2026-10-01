"""Issue #13: independent tasks of one Goal dispatch up to a bounded capacity.

RED note (recorded before the scheduler change, 2026-10-01):
``test_issue13_independent_busy_runs_overlap`` failed on unmodified
``AppCoordinator``. One Goal, tasks A and B, no dependencies, fake backend
whose ``observe_run`` stays busy and whose ``list_pending_actions`` is empty.
Three ``process_goal`` ticks left ``started == ['A']`` and B ``queued``.
Cause: the running-task loop returned on the first busy ``observe_run``, and
queued dispatch returned after starting one busy task. B never overlapped A.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.app_service import CollabApplication  # noqa: E402


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Backend:
    """Fake worker. Busy runs stay current until collect or cancel.

    ``block`` holds ``start_run`` after the run is counted so two ticks can
    overlap inside the backend. ``auto_finish`` titles are idle on the next
    observe. ``fail_titles`` collect as business failures.
    """

    backend_id = "fake.concurrency_v1"

    def __init__(self) -> None:
        self.started: list[str] = []
        self.running = 0
        self.max_running = 0
        self.entered = 0
        self.cancelled: list[str] = []
        self.seen_at_start: dict[str, dict[str, str | None]] = {}
        self.intervals: dict[str, dict[str, float | None]] = {}
        self.auto_finish: set[str] = set()
        self.fail_titles: set[str] = set()
        self.bodies: dict[str, dict[str, str]] = {}
        self.block: threading.Event | None = None
        self._mu = threading.Lock()
        self._runs: dict[str, dict] = {}
        self._busy: set[str] = set()

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        label = str(title)
        snap: dict[str, str | None] = {}
        for name in ("a.txt", "b.txt"):
            path = root / name
            snap[name] = path.read_text(encoding="utf-8") if path.is_file() else None
        written: list[str] = []
        for rel in artifacts or []:
            path = root / str(rel)
            if not path.exists():
                body = self.bodies.get(label, {}).get(str(rel), f"{label}\n")
                path.write_text(body, encoding="utf-8")
            written.append(str(path.resolve()))
        with self._mu:
            self.entered += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.started.append(label)
            self.seen_at_start[label] = snap
            if label not in self.intervals:
                self.intervals[label] = {"start": time.monotonic(), "end": None}
            run_id = f"run-{len(self.started)}-{label}"
            self._runs[run_id] = {
                "title": label,
                "directory": str(root),
                "artifacts": written,
            }
            if label not in self.auto_finish:
                self._busy.add(run_id)
            blocker = self.block
        if blocker is not None:
            blocker.wait(3)
        return {
            "ok": True,
            "backend": self.backend_id,
            "run_id": run_id,
            "native_handle": run_id,
        }

    def observe_run(self, run_id, **kwargs):
        with self._mu:
            rec = self._runs.get(run_id) or {}
            title = str(rec.get("title") or "")
            # auto_finish can be widened after start; the next observe releases it.
            if title in self.auto_finish:
                self._busy.discard(run_id)
            busy = run_id in self._busy
        if busy:
            return {"busy": True, "finish_successful": False}
        title = self._runs[run_id]["title"]
        return {"busy": False, "finish_successful": title not in self.fail_titles}

    def collect_result(self, run_id):
        with self._mu:
            self._busy.discard(run_id)
            self.running = max(0, self.running - 1)
            rec = self._runs[run_id]
            title = rec["title"]
            interval = self.intervals.get(title)
            if interval is not None and interval.get("end") is None:
                interval["end"] = time.monotonic()
        if title in self.fail_titles:
            return {"ok": False, "run_id": run_id, "error": f"{title} failed", "artifacts": []}
        return {"ok": True, "run_id": run_id, "artifacts": list(rec["artifacts"])}

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        with self._mu:
            self._busy.discard(run_id)
            self.running = max(0, self.running - 1)
            self.cancelled.append(str(run_id))
            title = (self._runs.get(run_id) or {}).get("title")
            interval = self.intervals.get(title) if title else None
            if interval is not None and interval.get("end") is None:
                interval["end"] = time.monotonic()
        return 200, {"ok": True, "run_id": run_id, "state": "cancelled"}


class _LimitedBackend(_Backend):
    """Service caps are high; this backend allows one run."""

    def capabilities(self):
        return {
            "concurrency": {"max_runs": 1, "limited_by": ["custom_pool"]},
        }


def _goal_body(**overrides) -> dict:
    body = {
        "idempotency_key": "conc-1",
        "client_id": "test-suite",
        "title": "parallel goal",
        "goal": "Do the independent tasks.",
        "boundaries": {
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
        },
        "acceptance": {"artifacts": ["out.txt"]},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }
    body.update(overrides)
    return body


def _task(
    task_id: str,
    title: str,
    *,
    depends_on: list[str] | None = None,
    workdir: str = "",
    artifacts: list[str] | None = None,
) -> dict:
    inputs: dict = {"instruction": f"do {title}"}
    if workdir:
        inputs["workdir"] = workdir
    names = list(artifacts or [f"{title.lower()}.txt"])
    return {
        "task_id": task_id,
        "title": title,
        "status": "queued",
        "depends_on": list(depends_on or []),
        "inputs": inputs,
        "expected_artifacts": names,
        "done_when": {"artifacts": names},
    }


def _statuses(app: CollabApplication, goal_id: str) -> dict[str, str]:
    return {row["title"]: row["status"] for row in app.status(goal_id)["tasks"]}


def _by_title(app: CollabApplication, goal_id: str, title: str) -> dict:
    return next(row for row in app.status(goal_id)["tasks"] if row["title"] == title)


def _backdate(app: CollabApplication, goal_id: str, *, seconds: float) -> None:
    with app.layer._rmw():
        snap = app.layer.goals[goal_id]
        snap["created_at"] = time.time() - seconds
        app.layer._persist_unlocked()


class Issue13ReproTests(unittest.TestCase):
    def _open(self, td: str, backend, *, key: str = "conc-1", **limits) -> tuple[CollabApplication, str]:
        app = CollabApplication(td, backend=backend, **limits)
        opened = app.submit(_goal_body(idempotency_key=key))
        self.assertTrue(opened.get("ok", True), opened)
        return app, opened["goal_id"]

    def _add(self, app: CollabApplication, goal_id: str, *tasks: dict) -> None:
        for task in tasks:
            added = app.layer.add_child_task(goal_id, task=task)
            self.assertTrue(added.get("ok"), added)

    def test_issue13_independent_busy_runs_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            for _ in range(3):
                app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A", "B"])
            self.assertGreaterEqual(backend.max_running, 2)
            self.assertIsNone(backend.intervals["A"]["end"])
            self.assertLess(backend.intervals["A"]["start"], backend.intervals["B"]["start"])
            self.assertEqual(_statuses(app, goal_id), {"A": "running", "B": "running"})
            status = app.status(goal_id)
            self.assertEqual(status["state"], "running")
            self.assertIsNone(status["failure"])
            self.assertNotIn("primary_failure", status)

    def test_successor_starts_only_after_both_parents_and_receives_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            backend.auto_finish = {"A", "B", "C"}
            backend.bodies = {
                "A": {"a.txt": "from-A\n"},
                "B": {"b.txt": "from-B\n"},
                "C": {"c.txt": "from-C\n"},
            }
            app, goal_id = self._open(td, backend)
            self._add(
                app,
                goal_id,
                _task("A", "A", artifacts=["a.txt"]),
                _task("B", "B", artifacts=["b.txt"]),
                _task("C", "C", depends_on=["A", "B"], artifacts=["c.txt"]),
            )
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A", "B", "C"])
            seen = backend.seen_at_start["C"]
            self.assertEqual(seen["a.txt"], "from-A\n")
            self.assertEqual(seen["b.txt"], "from-B\n")
            status = app.status(goal_id)
            self.assertEqual(status["state"], "completed", status)
            task_c = _by_title(app, goal_id, "C")
            workspace = Path(task_c["workspace"])
            self.assertEqual((workspace / "a.txt").read_text(encoding="utf-8"), "from-A\n")
            self.assertEqual((workspace / "b.txt").read_text(encoding="utf-8"), "from-B\n")
            self.assertEqual(Path(task_c["result"]["workspace"]).resolve(), workspace.resolve())

    def test_capacity_one_stays_serial_and_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend, max_parallel_per_goal=1)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            for _ in range(3):
                app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A"])
            self.assertEqual(backend.max_running, 1)
            self.assertEqual(_statuses(app, goal_id)["A"], "running")
            self.assertEqual(_statuses(app, goal_id)["B"], "queued")
            status = app.status(goal_id)
            self.assertEqual(status["state"], "running")
            self.assertIsNone(status["failure"])
            self.assertNotIn("primary_failure", status)
            scheduler = status["scheduler"]
            self.assertEqual(scheduler["running"], 1)
            self.assertEqual(scheduler["queued_ready"], 1)
            self.assertEqual(scheduler["waiting_reason"], "capacity")
            self.assertGreaterEqual(scheduler["capacity"], 1)
            self.assertLessEqual(scheduler["running"], scheduler["capacity"])

    def test_backend_max_runs_one_serializes_even_if_service_caps_are_high(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _LimitedBackend()
            app, goal_id = self._open(
                td,
                backend,
                max_parallel_per_goal=4,
                max_parallel_global=4,
            )
            caps = app.capabilities()["concurrency"]
            self.assertEqual(caps["effective"], 1)
            self.assertIn("custom_pool", caps["limited_by"])
            self.assertNotIn("max_parallel_per_goal", caps["limited_by"])
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A"])
            self.assertEqual(app.status(goal_id)["scheduler"]["waiting_reason"], "capacity")
            self.assertIsNone(app.status(goal_id)["failure"])

    def test_same_workdir_serializes_inside_a_goal_and_across_goals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            shared = str(Path(td) / "shared")
            other = str(Path(td) / "other")
            app = CollabApplication(td, backend=backend, max_parallel_per_goal=4, max_parallel_global=4)
            first = app.submit(_goal_body(idempotency_key="wd-1"))
            second = app.submit(_goal_body(idempotency_key="wd-2", title="other goal"))
            goal_a = first["goal_id"]
            goal_b = second["goal_id"]
            self._add(
                app,
                goal_a,
                _task("A", "A", workdir=shared),
                _task("B", "B", workdir=shared),
            )
            self._add(app, goal_b, _task("D", "D", workdir=shared), _task("E", "E", workdir=other))
            app.coordinator.process_goal(goal_a)
            self.assertEqual(backend.started, ["A"])
            status_a = app.status(goal_a)
            self.assertEqual(status_a["scheduler"]["waiting_reason"], "workdir_claim")
            self.assertEqual(_statuses(app, goal_a)["B"], "queued")
            self.assertIsNone(status_a["failure"])
            app.coordinator.process_goal(goal_b)
            self.assertEqual(backend.started, ["A", "E"])
            status_b = app.status(goal_b)
            self.assertEqual(_statuses(app, goal_b)["D"], "queued")
            self.assertEqual(_statuses(app, goal_b)["E"], "running")
            self.assertEqual(status_b["scheduler"]["waiting_reason"], "workdir_claim")
            self.assertIsNone(status_b["failure"])

    def test_global_cap_one_blocks_a_second_goal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app = CollabApplication(td, backend=backend, max_parallel_per_goal=2, max_parallel_global=1)
            goal_a = app.submit(_goal_body(idempotency_key="g-1"))["goal_id"]
            goal_b = app.submit(_goal_body(idempotency_key="g-2"))["goal_id"]
            self._add(app, goal_a, _task("A", "A"))
            self._add(app, goal_b, _task("B", "B"))
            app.coordinator.process_goal(goal_a)
            app.coordinator.process_goal(goal_b)
            self.assertEqual(backend.started, ["A"])
            self.assertEqual(_statuses(app, goal_b)["B"], "queued")
            status_b = app.status(goal_b)
            self.assertEqual(status_b["scheduler"]["waiting_reason"], "capacity")
            # Admitting a child task already moves the Goal to running. A full
            # global cap leaves it there; it does not fail the Goal.
            self.assertEqual(status_b["state"], "running")
            self.assertIsNone(status_b["failure"])

    def test_two_threads_do_not_double_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            backend.block = threading.Event()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            errors: list[BaseException] = []

            def tick() -> None:
                try:
                    app.coordinator.process_goal(goal_id)
                except BaseException as exc:  # noqa: BLE001 - fail the test from the main thread
                    errors.append(exc)

            threads = [threading.Thread(target=tick), threading.Thread(target=tick)]
            for thread in threads:
                thread.start()
            deadline = time.time() + 3
            while time.time() < deadline and backend.entered < 2:
                time.sleep(0.01)
            self.assertGreaterEqual(backend.entered, 2, backend.started)
            backend.block.set()
            for thread in threads:
                thread.join(4)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sorted(backend.started), ["A", "B"])
            self.assertGreaterEqual(backend.max_running, 2)
            self.assertEqual(_statuses(app, goal_id), {"A": "running", "B": "running"})

    def test_two_threads_start_one_task_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            backend.block = threading.Event()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"))
            errors: list[BaseException] = []

            def tick() -> None:
                try:
                    app.coordinator.process_goal(goal_id)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            first = threading.Thread(target=tick)
            second = threading.Thread(target=tick)
            first.start()
            deadline = time.time() + 3
            while time.time() < deadline and backend.entered < 1:
                time.sleep(0.01)
            self.assertEqual(backend.entered, 1)
            second.start()
            second.join(3)
            self.assertFalse(second.is_alive())
            self.assertEqual(backend.entered, 1, backend.started)
            backend.block.set()
            first.join(3)
            self.assertFalse(first.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(backend.started, ["A"])
            task = _by_title(app, goal_id, "A")
            self.assertEqual(task["status"], "running")
            self.assertTrue(str(task.get("run_id") or ""))

    def test_failed_parent_blocks_successor_while_sibling_continues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            backend.auto_finish = {"A"}
            backend.fail_titles = {"A"}
            app, goal_id = self._open(td, backend)
            self._add(
                app,
                goal_id,
                _task("A", "A", artifacts=["a.txt"]),
                _task("B", "B", artifacts=["b.txt"]),
                _task("C", "C", depends_on=["A", "B"], artifacts=["c.txt"]),
            )
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A", "B"])
            self.assertEqual(_statuses(app, goal_id)["A"], "failed")
            self.assertEqual(_statuses(app, goal_id)["B"], "running")
            self.assertEqual(_statuses(app, goal_id)["C"], "queued")
            midway = app.status(goal_id)
            self.assertEqual(midway["state"], "running", midway)
            self.assertIsNone(midway["failure"])
            backend.auto_finish.add("B")
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A", "B"])
            self.assertEqual(_statuses(app, goal_id)["C"], "queued")
            self.assertEqual(_statuses(app, goal_id)["B"], "succeeded")
            done = app.status(goal_id)
            self.assertEqual(done["state"], "failed", done)
            self.assertIsNone(done["failure"])

    def test_cancel_stops_both_running_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            app.coordinator.process_goal(goal_id)
            run_ids = {
                _by_title(app, goal_id, "A")["run_id"],
                _by_title(app, goal_id, "B")["run_id"],
            }
            cancelled = app.cancel(goal_id, reason="stop both")
            self.assertEqual(cancelled.get("state"), "cancelled", cancelled)
            self.assertEqual(set(backend.cancelled), run_ids)
            self.assertEqual(_statuses(app, goal_id), {"A": "cancelled", "B": "cancelled"})
            self.assertEqual(app.status(goal_id)["state"], "cancelled")

    def test_restart_observes_bound_runs_without_starting_again(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            app.coordinator.process_goal(goal_id)
            before = list(backend.started)
            bound = {
                title: _by_title(app, goal_id, title)["run_id"]
                for title in ("A", "B")
            }
            restored = CollabApplication(td, backend=backend)
            restored.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, before)
            self.assertEqual(
                {title: _by_title(restored, goal_id, title)["run_id"] for title in ("A", "B")},
                bound,
            )
            self.assertEqual(_statuses(restored, goal_id), {"A": "running", "B": "running"})

    def test_wall_budget_stops_both_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend)
            self._add(
                app,
                goal_id,
                _task("A", "A"),
                _task("B", "B"),
                _task("D", "D", depends_on=["A"]),
            )
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A", "B"])
            run_ids = {
                _by_title(app, goal_id, "A")["run_id"],
                _by_title(app, goal_id, "B")["run_id"],
            }
            _backdate(app, goal_id, seconds=100)
            outcome = app.coordinator.process_goal(goal_id)
            self.assertEqual(outcome.get("action"), "budget_exhausted", outcome)
            self.assertEqual(set(backend.cancelled), run_ids)
            status = app.status(goal_id)
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["failure"]["phase"], "budget")
            self.assertIn("wall_sec", status["failure"]["error"])
            self.assertEqual(status["primary_failure"]["source"], "worker_timeout")
            self.assertEqual(_statuses(app, goal_id)["A"], "failed")
            self.assertEqual(_statuses(app, goal_id)["B"], "failed")
            self.assertEqual(_statuses(app, goal_id)["D"], "queued")
            self.assertIsNotNone(backend.intervals["A"]["end"])
            self.assertIsNotNone(backend.intervals["B"]["end"])

    def test_max_reworks_zero_does_not_trip_until_retries_exist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"))
            with app.layer._rmw():
                snap = app.layer.goals[goal_id]
                history = list(snap.get("history") or [])
                history.append({"op": "retry_task", "task_id": "A"})
                snap["history"] = history
                app.layer._persist_unlocked()
            outcome = app.coordinator.process_goal(goal_id)
            self.assertEqual(outcome.get("action"), "budget_exhausted", outcome)
            self.assertEqual(backend.started, [])
            status = app.status(goal_id)
            self.assertEqual(status["state"], "failed")
            self.assertIn("max_reworks", status["failure"]["error"])
            self.assertNotIn("wall", status["failure"]["error"])
            self.assertEqual(_statuses(app, goal_id)["A"], "queued")
            # No task failed; the goal-level budget stop is projected without
            # inventing a task id (#12/#16).
            primary = status["primary_failure"]
            self.assertEqual(primary["task_id"], "")
            self.assertEqual(primary["stage"], "budget")
            self.assertEqual(primary["source"], "coordination")
            self.assertIn("max_reworks", primary["error"])

    def test_task_question_does_not_block_an_unrelated_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend, max_parallel_per_goal=1)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            app.coordinator.process_goal(goal_id)
            opened = app.layer.open_decision(
                goal_id,
                kind="question",
                task_id="A",
                run_id=_by_title(app, goal_id, "A")["run_id"],
                title="which name",
            )
            self.assertTrue(opened.get("ok"), opened)
            status = app.status(goal_id)
            self.assertEqual(status["scheduler"]["waiting_reason"], "capacity")
            self.assertEqual(_statuses(app, goal_id)["B"], "queued")
            wide = CollabApplication(
                td,
                backend=backend,
                max_parallel_per_goal=2,
                max_parallel_global=4,
            )
            # Same persist, higher cap: the question on A must not gate B.
            wide.coordinator.process_goal(goal_id)
            self.assertIn("B", backend.started)
            self.assertEqual(_statuses(wide, goal_id)["B"], "running")
            self.assertNotEqual(wide.status(goal_id)["scheduler"]["waiting_reason"], "global_approval")

    def test_global_approval_gates_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = _Backend()
            app, goal_id = self._open(td, backend)
            self._add(app, goal_id, _task("A", "A"), _task("B", "B"))
            opened = app.layer.open_decision(goal_id, kind="plan_review", task_id="", title="approve plan")
            self.assertTrue(opened.get("ok"), opened)
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, [])
            status = app.status(goal_id)
            self.assertEqual(status["scheduler"]["waiting_reason"], "global_approval")
            self.assertEqual(status["scheduler"]["queued_ready"], 2)
            self.assertIsNone(status["failure"])
            self.assertEqual(status["state"], "running")
            self.assertEqual(_statuses(app, goal_id), {"A": "queued", "B": "queued"})


class ConcurrencyReportTests(unittest.TestCase):
    def test_inprocess_windows_linux_and_agy_caps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inprocess = CollabApplication(root / "inproc")
            caps = inprocess.capabilities()["concurrency"]
            self.assertEqual(caps["max_parallel_per_goal"], 2)
            self.assertEqual(caps["max_parallel_global"], 4)
            self.assertEqual(caps["backend_max_runs"], 8)
            self.assertEqual(caps["effective"], 2)
            self.assertEqual(caps["limited_by"], ["max_parallel_per_goal"])

            from execution_backend.linux_supervised_v1 import LinuxSupervisedExecutionBackend
            from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend

            windows = CollabApplication(
                root / "win-app",
                backend=WindowsSupervisedExecutionBackend(state_dir=root / "win-state", client=object()),
            )
            win_caps = windows.capabilities()["concurrency"]
            self.assertEqual(win_caps["effective"], 1)
            self.assertEqual(win_caps["backend_max_runs"], 1)
            self.assertEqual(win_caps["limited_by"], ["desktop_session_lock"])

            linux = CollabApplication(
                root / "linux-app",
                backend=LinuxSupervisedExecutionBackend(state_dir=root / "linux-state", client=object()),
            )
            linux_caps = linux.capabilities()["concurrency"]
            self.assertEqual(linux_caps["effective"], 1)
            self.assertEqual(linux_caps["limited_by"], ["desktop_session_lock"])

            from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend

            bare = AntigravityCliExecutionBackend(bin_path="agy", environ={})
            self.assertEqual(bare.capabilities()["concurrency"]["max_runs"], 1)
            invalid = root / "bad-pool.json"
            invalid.write_text("not-json", encoding="utf-8")
            broken = AntigravityCliExecutionBackend(
                bin_path="agy",
                environ={},
                account_pool_path=str(invalid),
            )
            self.assertEqual(broken.capabilities()["concurrency"]["max_runs"], 1)
            pool_path = root / "pool.json"
            homes = [root / "h1", root / "h2", root / "h3"]
            pool_path.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {"id": "acct-1", "home": str(homes[0])},
                            {"id": "acct-2", "home": str(homes[1])},
                            {"id": "acct-3", "home": str(homes[2])},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pooled = CollabApplication(
                root / "agy-app",
                backend=AntigravityCliExecutionBackend(
                    bin_path="agy",
                    environ={},
                    account_pool_path=str(pool_path),
                ),
                max_parallel_per_goal=8,
                max_parallel_global=8,
            )
            agy_caps = pooled.capabilities()["concurrency"]
            self.assertEqual(agy_caps["backend_max_runs"], 3)
            self.assertEqual(agy_caps["effective"], 3)
            self.assertEqual(agy_caps["limited_by"], ["agy_account_pool"])

            unknown = CollabApplication(
                root / "unknown-app",
                backend=_UnknownBackend(),
            )
            unknown_caps = unknown.capabilities()["concurrency"]
            self.assertEqual(unknown_caps["backend_max_runs"], "unknown")
            self.assertEqual(unknown_caps["effective"], 2)
            self.assertEqual(unknown_caps["limited_by"], ["max_parallel_per_goal"])

    def test_service_flags_default_to_two_and_four(self) -> None:
        mod = _load_script("collab_service_concurrency_mod", ROOT / "bin" / "collab-service.py")
        args = mod.build_parser().parse_args([])
        self.assertEqual(args.max_parallel_per_goal, 2)
        self.assertEqual(args.max_parallel_global, 4)

    def test_client_summary_includes_scheduler_only_when_present(self) -> None:
        mod = _load_script("hermes_collab_request_concurrency", ROOT / "bin" / "hermes-collab-request.py")
        absent = mod.summarize_status({"ok": True, "state": "running", "request_id": "g"})
        self.assertNotIn("scheduler", absent)
        present = mod.summarize_status(
            {
                "ok": True,
                "state": "running",
                "request_id": "g",
                "scheduler": {
                    "running": None,
                    "queued_ready": "2",
                    "capacity": 1,
                    "waiting_reason": None,
                },
            }
        )
        self.assertEqual(
            present["scheduler"],
            {"running": 0, "queued_ready": 2, "capacity": 1, "waiting_reason": ""},
        )
        nested = mod.summarize_status(
            {
                "ok": True,
                "state": "running",
                "request_id": "g",
                "report": {
                    "scheduler": {
                        "running": 1,
                        "queued_ready": 1,
                        "capacity": 2,
                        "waiting_reason": "capacity",
                    }
                },
            }
        )
        self.assertEqual(nested["scheduler"]["waiting_reason"], "capacity")
        self.assertEqual(nested["scheduler"]["running"], 1)


class _UnknownBackend:
    backend_id = "fake.unknown_concurrency_v1"

    def capabilities(self):
        return {"concurrency": {"max_runs": "unknown", "limited_by": ["mystery"]}}


if __name__ == "__main__":
    unittest.main()
