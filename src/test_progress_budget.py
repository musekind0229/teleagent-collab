"""Progress heartbeats and usage budgets. No real model. No token logging."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402
from execution_backend.base import default_capabilities  # noqa: E402
from execution_backend.inprocess_v1 import InProcessExecutionBackend  # noqa: E402
from execution_backend.windows_supervised_v1 import supervised_progress  # noqa: E402
from framework.app_service import AppError, CollabApplication  # noqa: E402
from framework.progress_budget import (  # noqa: E402
    budget_enforcement_capability,
    derive_goal_progress,
    metering_capability,
    progress_capability,
    utc_iso,
)
from test_contract_render import _write_fake_agy  # noqa: E402


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _caps(
    *,
    live: bool = False,
    tokens: str = "unsupported",
    calls: str = "unsupported",
    no_progress: str = "enforced",
    usage_at_end: bool = False,
) -> dict:
    caps = default_capabilities(backend_id="fake.progress_v1", kind="fake")
    caps["progress"] = progress_capability(
        available=True,
        heartbeat="runner_process",
        artifact_checkpoint=True,
        subagent_observability=False,
    )
    reported = live or usage_at_end or tokens != "unsupported"
    caps["metering"] = metering_capability(
        live_usage=live,
        usage_at_end=usage_at_end or tokens == "post_hoc",
        tool_calls=calls == "enforced_live",
        fields=["total_tokens"] if tokens != "unsupported" else [],
        source="worker_self_reported" if reported else "none",
    )
    caps["budget_enforcement"] = budget_enforcement_capability(
        max_tokens=tokens,
        max_tool_calls=calls,
        no_progress_sec=no_progress,
    )
    caps["usage"] = {"source": caps["metering"]["source"] if reported else "unknown"}
    caps["concurrency"] = {"max_runs": 4, "limited_by": ["fake"]}
    return caps


class _Fake:
    """Busy until auto_finish. Usage numbers stay on the run record, not stdout."""

    backend_id = "fake.progress_v1"

    def __init__(self, caps: dict, clock: _Clock) -> None:
        self._caps = caps
        self.clock = clock
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.auto_finish = False
        self.stuck_titles: set[str] = set()
        self.origin: float | None = None
        self.tokens = 0
        self.tool_calls: int | None = None
        self.usage_on_collect: dict | None = None
        self.write_partial = True
        self.report_progress = True
        self._runs: dict[str, dict] = {}

    def capabilities(self) -> dict:
        return self._caps

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        del instruction, artifacts, charter
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        if self.write_partial:
            (root / "partial.txt").write_text("partial-artifact\n", encoding="utf-8")
        run_id = f"run-{len(self.started) + 1}-{title}"
        self.started.append(str(title))
        self._runs[run_id] = {"title": str(title), "directory": str(root)}
        if self.origin is None:
            self.origin = self.clock()
        return {"ok": True, "backend": self.backend_id, "run_id": run_id, "native_handle": run_id}

    def observe_run(self, run_id, **kwargs):
        del kwargs
        rec = self._runs[run_id]
        if not self.report_progress:
            return {"busy": not self.auto_finish, "finish_successful": self.auto_finish}
        title = rec["title"]
        if title in self.stuck_titles and self.origin is not None:
            progress_at = self.origin
        else:
            progress_at = self.clock()
        progress: dict = {
            "phase": "executing",
            "last_heartbeat_at": utc_iso(self.clock()),
            "last_progress_at": utc_iso(progress_at),
            "events": ["state executing"],
            "source": "runner",
        }
        if self.tokens:
            progress["usage"] = {"total_tokens": self.tokens}
        if self.tool_calls is not None:
            progress["tool_calls"] = self.tool_calls
        busy = not self.auto_finish
        out: dict = {"busy": busy, "finish_successful": self.auto_finish, "progress": progress}
        if "usage" in progress:
            out["usage"] = dict(progress["usage"])
        return out

    def collect_result(self, run_id):
        rec = self._runs[run_id]
        out = {
            "ok": True,
            "run_id": run_id,
            "artifacts": [str(Path(rec["directory"]) / "partial.txt")],
            "workspace": rec["directory"],
        }
        if self.usage_on_collect is not None:
            out["usage"] = dict(self.usage_on_collect)
        elif self.tokens:
            out["usage"] = {"total_tokens": self.tokens}
        return out

    def list_pending_actions(self, *, session_id=None):
        del session_id
        return 200, []

    def cancel(self, run_id):
        self.cancelled.append(str(run_id))
        return 200, {"ok": True, "run_id": run_id}


def _goal(**overrides) -> dict:
    body = {
        "idempotency_key": "prog-1",
        "client_id": "test-suite",
        "title": "progress budget",
        "goal": "Write partial.txt inside the assigned workspace.",
        "boundaries": {
            "must": ["Stay inside the assigned workspace"],
            "must_not": ["Do not use network or system tools"],
        },
        "acceptance": {"artifacts": ["partial.txt"], "text": "partial.txt exists"},
        "budget": {"wall_sec": 10**9, "max_reworks": 1},
    }
    body.update(overrides)
    return body


def _task(task_id: str, title: str, *, depends_on: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "title": title,
        "status": "queued",
        "depends_on": list(depends_on or []),
        "inputs": {"instruction": f"do {title}"},
        "expected_artifacts": ["partial.txt"],
        "done_when": {"artifacts": ["partial.txt"]},
    }


def _statuses(app: CollabApplication, goal_id: str) -> dict[str, str]:
    return {row["title"]: row["status"] for row in app.status(goal_id)["tasks"]}


def _by_title(app: CollabApplication, goal_id: str, title: str) -> dict:
    return next(row for row in app.status(goal_id)["tasks"] if row["title"] == title)


def _retries(app: CollabApplication, goal_id: str) -> int:
    snap = app.layer.get_goal(goal_id)["goal"]
    return sum(1 for row in (snap.get("history") or []) if isinstance(row, dict) and row.get("op") == "retry_task")


class DeriveProgressTests(unittest.TestCase):
    def test_boundaries_focus_and_unknown(self) -> None:
        now = 1_700_000_000
        cap = {"available": True, "subagent_observability": False}
        stale_hb = utc_iso(now - 121)
        fresh = utc_iso(now)
        exact = utc_iso(now - 120)
        snap = {
            "state": "running",
            "tasks": [
                {
                    "task_id": "stuck",
                    "status": "running",
                    "progress": {
                        "phase": "executing",
                        "source": "runner",
                        "last_heartbeat_at": stale_hb,
                        "events": ["stuck"],
                        "artifacts_checkpoint": [{"name": "a.txt", "size": 1, "mtime": stale_hb}],
                    },
                },
                {
                    "task_id": "healthy",
                    "status": "running",
                    "progress": {
                        "phase": "executing",
                        "source": "runner",
                        "last_heartbeat_at": fresh,
                        "events": ["healthy"],
                    },
                },
            ],
        }
        view = derive_goal_progress(snap, scheduler={}, capability=cap, stale_after_sec=120, now=now)
        self.assertEqual(view["state"], "stale")
        self.assertEqual(view["recent_events"], ["stuck"])
        self.assertNotIn("percent", view)
        self.assertIs(view["subagent_observability"], False)

        snap["tasks"][0]["progress"]["last_heartbeat_at"] = exact
        exact_view = derive_goal_progress(snap, scheduler={}, capability=cap, stale_after_sec=120, now=now)
        self.assertEqual(exact_view["state"], "executing")
        self.assertNotEqual(exact_view["state"], "stale")

        delivering = {
            "state": "running",
            "tasks": [
                {
                    "task_id": "fin",
                    "status": "running",
                    "progress": {
                        "phase": "finalizing",
                        "source": "runner",
                        "last_heartbeat_at": fresh,
                        "events": ["packing"],
                    },
                },
                {
                    "task_id": "sib",
                    "status": "running",
                    "progress": {
                        "phase": "executing",
                        "source": "runner",
                        "last_heartbeat_at": fresh,
                        "events": ["later"],
                    },
                },
            ],
        }
        packed = derive_goal_progress(delivering, scheduler={}, capability=cap, stale_after_sec=120, now=now)
        self.assertEqual(packed["state"], "delivering")
        self.assertEqual(packed["phase"], "finalizing")
        self.assertEqual(packed["recent_events"], ["packing"])

        unknown = {
            "state": "running",
            "tasks": [
                {
                    "task_id": "u",
                    "status": "running",
                    "progress": {
                        "phase": "unknown",
                        "source": "none",
                        "last_heartbeat_at": stale_hb,
                        "events": ["nope"],
                    },
                }
            ],
        }
        unk = derive_goal_progress(unknown, scheduler={}, capability=cap, stale_after_sec=120, now=now)
        self.assertEqual(unk["state"], "unknown")
        self.assertEqual(unk["phase"], "unknown")

        missing = {"state": "running", "tasks": [{"task_id": "m", "status": "running"}]}
        miss = derive_goal_progress(missing, scheduler={}, capability=cap, stale_after_sec=120, now=now)
        self.assertEqual(miss["state"], "unknown")

        blind = derive_goal_progress(
            snap,
            scheduler={},
            capability={"available": False, "subagent_observability": 0},
            stale_after_sec=120,
            now=now,
        )
        self.assertIs(blind["available"], False)
        self.assertEqual(blind["state"], "unknown")
        self.assertEqual(blind["phase"], "unknown")
        self.assertEqual(blind["subagent_observability"], "unknown")
        self.assertNotIn("percent", blind)

        waiting = derive_goal_progress(
            {"state": "running", "tasks": [], "pending_decisions": [{"decision_id": "d", "kind": "checkpoint"}]},
            scheduler={},
            capability=cap,
            stale_after_sec=120,
            now=now,
        )
        self.assertEqual(waiting["state"], "waiting_decision")
        self.assertEqual(waiting["phase"], "executing")

        capacity = derive_goal_progress(
            {"state": "running", "tasks": [{"task_id": "q", "status": "queued"}]},
            scheduler={"waiting_reason": "capacity", "queued_ready": 1},
            capability=cap,
            stale_after_sec=120,
            now=now,
        )
        self.assertEqual(capacity["state"], "waiting_capacity")

        done = derive_goal_progress(
            {"state": "completed", "tasks": snap["tasks"]},
            scheduler={},
            capability=cap,
            stale_after_sec=120,
            now=now,
        )
        self.assertEqual(done["state"], "done")


class BackendSignalTests(unittest.TestCase):
    def test_inprocess_heartbeat_is_stable_and_hides_instruction(self) -> None:
        backend = InProcessExecutionBackend()
        with tempfile.TemporaryDirectory() as td:
            launched = backend.start_run(
                title="hello",
                directory=td,
                instruction="SECRET_INSTRUCTION",
                artifacts=["hello.txt"],
            )
            self.assertTrue(launched.get("ok"), launched)
            first = backend.observe_run(launched["run_id"])["progress"]
            second = backend.observe_run(launched["run_id"])["progress"]
        self.assertEqual(first["phase"], "done")
        self.assertEqual(first["source"], "runner")
        self.assertEqual(first["last_heartbeat_at"], second["last_heartbeat_at"])
        self.assertNotIn("percent", first)
        blob = json.dumps(first)
        self.assertNotIn("SECRET_INSTRUCTION", blob)
        self.assertTrue(any(item["name"].endswith("hello.txt") for item in first["artifacts_checkpoint"]))

    def test_supervised_progress_does_not_copy_event_payloads(self) -> None:
        unknown = supervised_progress(
            state="not-a-state",
            now=10,
            last_event_at=9,
            event_kinds=["SECRET_KIND_BODY"],
            pending_kinds=["tool"],
            checkpoint=[],
        )
        self.assertEqual(unknown["phase"], "unknown")
        self.assertEqual(unknown["source"], "none")
        self.assertEqual(unknown["events"], [])
        self.assertIsNone(unknown["last_heartbeat_at"])
        pending = supervised_progress(
            state="awaiting_permission",
            now=10,
            last_event_at=9,
            event_kinds=["tool_call"],
            pending_kinds=["permission"],
            checkpoint=[],
        )
        self.assertEqual(pending["phase"], "executing")
        self.assertEqual(pending["source"], "runner")
        self.assertIn("pending permission", pending["events"])
        self.assertNotEqual(pending["last_heartbeat_at"], pending["last_progress_at"])
        text = (SRC / "execution_backend" / "windows_supervised_v1.py").read_text(encoding="utf-8")
        self.assertIn("SELECT time, kind FROM events", text)
        self.assertNotIn("SELECT * FROM events", text)
        self.assertNotIn("SELECT data", text)

    def test_agy_observe_heartbeat_and_artifact_names_while_holding(self) -> None:
        secret = "SECRET_BLOB"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bin").mkdir()
            binary = _write_fake_agy(root / "bin")
            work = root / "work"
            work.mkdir()
            env = os.environ.copy()
            for key in (
                "AGY_AUTO_APPROVE",
                "COLLAB_AGY_AUTO_APPROVE",
                "COLLAB_AGY_ACCOUNT_POOL",
                "AGY_PROFILE",
                "AGY_BIN",
            ):
                env.pop(key, None)
            env["AGY_FAKE_HOLD_SEC"] = "3"
            env["AGY_FAKE_ARTIFACT"] = "note.txt"
            env["AGY_FAKE_ARTIFACT_BODY"] = secret + "\n"
            backend = AntigravityCliExecutionBackend(
                bin_path=str(binary),
                timeout_sec=30,
                environ=env,
            )
            launched = backend.start_run(
                title="hold",
                directory=str(work),
                instruction="SECRET_INSTRUCTION",
                artifacts=["note.txt"],
            )
            self.assertTrue(launched.get("ok"), launched)
            run_id = launched["run_id"]
            progress = {}
            try:
                deadline = time.time() + 2
                while time.time() < deadline:
                    progress = backend.observe_run(run_id)["progress"]
                    names = [row.get("name") for row in progress.get("artifacts_checkpoint") or []]
                    if "note.txt" in names and progress.get("phase") == "executing":
                        break
                    time.sleep(0.05)
                self.assertEqual(progress.get("source"), "runner")
                self.assertEqual(progress.get("phase"), "executing")
                self.assertTrue(progress.get("last_heartbeat_at"))
                names = [row.get("name") for row in progress.get("artifacts_checkpoint") or []]
                self.assertIn("note.txt", names)
                noted = next(row for row in progress["artifacts_checkpoint"] if row["name"] == "note.txt")
                self.assertGreater(noted["size"], 0)
                self.assertNotIn("content", noted)
                blob = json.dumps(progress)
                self.assertNotIn(secret, blob)
                self.assertNotIn("SECRET_INSTRUCTION", blob)
                self.assertNotIn("percent", progress)
            finally:
                backend.cancel(run_id)


class SubmitBudgetTests(unittest.TestCase):
    def test_types_and_capability_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            for bad, piece in (
                ({"wall_sec": True}, "wall_sec"),
                ({"max_reworks": 1.5}, "max_reworks"),
                ({"max_tokens": True}, "max_tokens"),
                ({"no_progress_sec": 0}, "no_progress_sec"),
                ({"on_no_progress": "retry"}, "on_no_progress"),
                ({"budget_mode": "auto"}, "budget_mode"),
            ):
                with self.assertRaises(AppError) as caught:
                    app.submit(_goal(idempotency_key=f"bad-{piece}", budget=bad))
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertIn(piece, str(caught.exception))
            for bad in (None, [], "nope"):
                with self.assertRaises(AppError) as caught:
                    app.submit(_goal(idempotency_key="bad-null", budget=bad))
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("object", str(caught.exception))
            self.assertEqual(app.layer.list_goals().get("goals"), [])

            empty = app.submit(_goal(idempotency_key="empty", budget={}))
            stored = app.layer.get_goal(empty["goal_id"])["goal"]["goal"]["budget"]
            # An empty budget keeps the existing reliable wall-clock default.
            self.assertEqual(stored, {"wall_sec": 360})

            kept = app.submit(
                _goal(
                    idempotency_key="hist",
                    budget={"wall_sec": 30, "max_reworks": 0, "max_attempts": 4, "max_usage": 1.0},
                )
            )
            hist = app.layer.get_goal(kept["goal_id"])["goal"]["goal"]["budget"]
            self.assertEqual(hist["max_attempts"], 4)
            self.assertEqual(hist["max_usage"], 1.0)
            self.assertEqual(hist["wall_sec"], 30)

            bare = app.submit({k: v for k, v in _goal(idempotency_key="bare").items() if k != "budget"})
            self.assertEqual(
                app.layer.get_goal(bare["goal_id"])["goal"]["goal"]["budget"],
                {"wall_sec": 300, "max_reworks": 1},
            )

            with self.assertRaises(AppError) as caught:
                app.submit(_goal(idempotency_key="tok", budget={"max_tokens": 10, "max_tool_calls": 2}))
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.code, "capability_unavailable")
            self.assertEqual(
                caught.exception.extra["missing"],
                ["budget:max_tokens", "budget:max_tool_calls"],
            )
            self.assertIn("capabilities", caught.exception.extra)

            warned = app.submit(
                _goal(
                    idempotency_key="report",
                    budget={"max_tokens": 10, "budget_mode": "report_only"},
                )
            )
            warnings = app.status(warned["goal_id"])["warnings"]
            self.assertIn("budget max_tokens is report-only on this backend", warnings)

            clock = _Clock()
            post = _Fake(_caps(tokens="post_hoc", usage_at_end=True), clock)
            post_app = CollabApplication(str(Path(td) / "post"), backend=post, clock=clock)
            opened = post_app.submit(_goal(idempotency_key="post", budget={"wall_sec": 10**9, "max_tokens": 10}))
            self.assertTrue(
                any("checked after the run; cannot stop mid-run" in item for item in post_app.status(opened["goal_id"])["warnings"])
            )

            refused = _Fake(_caps(no_progress="unsupported"), clock)
            refuse_app = CollabApplication(str(Path(td) / "refuse"), backend=refused, clock=clock)
            with self.assertRaises(AppError) as caught:
                refuse_app.submit(
                    _goal(
                        idempotency_key="all",
                        budget={"max_tokens": 1, "max_tool_calls": 1, "no_progress_sec": 5},
                    )
                )
            self.assertEqual(
                caught.exception.extra["missing"],
                ["budget:max_tokens", "budget:max_tool_calls", "budget:no_progress_sec"],
            )
            self.assertEqual(refuse_app.layer.list_goals().get("goals"), [])


class EnforcementTests(unittest.TestCase):
    def _open(self, td: str, backend: _Fake, **budget) -> tuple[CollabApplication, str]:
        app = CollabApplication(
            td,
            backend=backend,
            clock=backend.clock,
            max_parallel_per_goal=4,
            max_parallel_global=4,
        )
        body = _goal(budget={"wall_sec": 10**9, "max_reworks": 1, **budget})
        opened = app.submit(body)
        self.assertTrue(opened.get("ok", True), opened)
        return app, opened["goal_id"]

    def test_post_hoc_checkpoint_blocks_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(tokens="post_hoc", usage_at_end=True), clock)
            backend.auto_finish = True
            backend.usage_on_collect = {"total_tokens": 50, "input_tokens": 20, "output_tokens": 30}
            app, goal_id = self._open(td, backend, max_tokens=10)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.layer.add_child_task(goal_id, task=_task("D", "D", depends_on=["A"]))
            app.coordinator.process_goal(goal_id)
            status = app.status(goal_id)
            self.assertEqual(backend.started, ["A"])
            self.assertEqual(_statuses(app, goal_id)["A"], "awaiting_decision")
            self.assertEqual(_statuses(app, goal_id)["D"], "queued")
            self.assertEqual(status["state"], "running")
            self.assertTrue(status["awaiting_decision"])
            self.assertEqual(status["progress"]["state"], "waiting_decision")
            self.assertNotIn("percent", status["progress"])
            decision = status["pending_decisions"][0]
            self.assertEqual(decision["kind"], "checkpoint")
            self.assertIn("budget exceeded: continue or stop", decision["title"])
            row = status["budget_status"]["max_tokens"]
            self.assertTrue(row["exceeded"])
            self.assertEqual(row["enforced"], "post_hoc")
            self.assertEqual(row["limit"], 10)
            self.assertEqual(row["used"], 50)
            self.assertEqual(row["source"], "worker_self_reported")
            task = _by_title(app, goal_id, "A")
            self.assertTrue(task["result"]["budget_exceeded"])
            self.assertEqual(task["result"]["usage_report"]["note"], "not a bill")
            self.assertTrue((Path(task["workspace"]) / "partial.txt").is_file())
            self.assertTrue(any("post_hoc" in item and "not a bill" in item for item in status["warnings"]))

    def test_live_usage_cancels_both_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(live=True, tokens="enforced_live"), clock)
            backend.tokens = 6
            app, goal_id = self._open(td, backend, max_tokens=10)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.layer.add_child_task(goal_id, task=_task("B", "B"))
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.started, ["A", "B"])
            self.assertEqual(len(backend.cancelled), 2)
            status = app.status(goal_id)
            self.assertEqual(status["state"], "failed")
            for title in ("A", "B"):
                self.assertEqual(_by_title(app, goal_id, title)["result"]["error"], "budget_exceeded max_tokens")
            self.assertEqual(_retries(app, goal_id), 0)

    def test_live_tool_calls_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(calls="enforced_live"), clock)
            backend.tool_calls = 4
            app, goal_id = self._open(td, backend, max_tool_calls=1)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.coordinator.process_goal(goal_id)
            self.assertEqual(backend.cancelled, [_by_title(app, goal_id, "A")["run_id"]])
            self.assertEqual(_by_title(app, goal_id, "A")["result"]["error"], "budget_exceeded max_tool_calls")

    def test_no_progress_checkpoint_keeps_partial_and_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app, goal_id = self._open(td, backend, no_progress_sec=30, max_reworks=1)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.layer.add_child_task(goal_id, task=_task("B", "B"))
            app.layer.add_child_task(goal_id, task=_task("D", "D", depends_on=["A"]))
            app.coordinator.process_goal(goal_id)
            self.assertEqual(sorted(backend.started), ["A", "B"])
            backend.stuck_titles.add("A")
            clock.now += 30
            app.coordinator.process_goal(goal_id)
            status = app.status(goal_id)
            self.assertEqual(_statuses(app, goal_id)["A"], "awaiting_decision")
            self.assertEqual(_statuses(app, goal_id)["B"], "running")
            self.assertEqual(_statuses(app, goal_id)["D"], "queued")
            self.assertEqual(backend.started, ["A", "B"])
            self.assertEqual(_retries(app, goal_id), 0)
            self.assertEqual(status["progress"]["state"], "waiting_decision")
            decision = status["pending_decisions"][0]
            self.assertEqual(decision["kind"], "checkpoint")
            self.assertIn("checkpoint: no progress for 30s", decision["title"])
            task = _by_title(app, goal_id, "A")
            self.assertTrue((Path(task["workspace"]) / "partial.txt").is_file())
            self.assertEqual(backend.cancelled, [task["run_id"]])
            self.assertNotIn(task["run_id"], [_by_title(app, goal_id, "B")["run_id"]])

    def test_no_progress_fail_and_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app, goal_id = self._open(td, backend, no_progress_sec=30, on_no_progress="fail")
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.coordinator.process_goal(goal_id)
            clock.now += 30
            backend.stuck_titles.add("A")
            app.coordinator.process_goal(goal_id)
            status = app.status(goal_id)
            self.assertEqual(status["state"], "failed")
            self.assertEqual(_by_title(app, goal_id, "A")["result"]["error"], "no_progress_timeout")
            self.assertEqual(status["pending_decisions"], [])
            self.assertEqual(_retries(app, goal_id), 0)

        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app, goal_id = self._open(
                td,
                backend,
                no_progress_sec=30,
                budget_mode="report_only",
            )
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.coordinator.process_goal(goal_id)
            run_id = _by_title(app, goal_id, "A")["run_id"]
            backend.stuck_titles.add("A")
            clock.now += 30
            app.coordinator.process_goal(goal_id)
            self.assertEqual(_statuses(app, goal_id)["A"], "running")
            self.assertEqual(backend.cancelled, [])
            self.assertEqual(app.status(goal_id)["budget_status"]["no_progress_sec"]["enforced"], "report_only")
            self.assertTrue(app.status(goal_id)["budget_status"]["no_progress_sec"]["exceeded"])
            self.assertEqual(_by_title(app, goal_id, "A")["run_id"], run_id)

    def test_continue_counts_one_rework_and_stop_keeps_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app, goal_id = self._open(td, backend, no_progress_sec=30, max_reworks=1)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.coordinator.process_goal(goal_id)
            backend.stuck_titles.add("A")
            clock.now += 30
            app.coordinator.process_goal(goal_id)
            decision_id = app.status(goal_id)["pending_decisions"][0]["decision_id"]
            workspace = Path(_by_title(app, goal_id, "A")["workspace"])
            self.assertTrue((workspace / "partial.txt").is_file())
            backend.stuck_titles.clear()
            app.resolve(goal_id, decision_id, {"verdict": "continue", "reason": "human"})
            self.assertEqual(_retries(app, goal_id), 1)
            self.assertEqual(backend.started, ["A", "A"])
            self.assertEqual(_statuses(app, goal_id)["A"], "running")
            self.assertTrue((workspace / "partial.txt").is_file())
            app.resolve(goal_id, decision_id, {"verdict": "continue", "reason": "again"})
            self.assertEqual(_retries(app, goal_id), 1)
            self.assertEqual(backend.started, ["A", "A"])

        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app, goal_id = self._open(td, backend, no_progress_sec=30, max_reworks=0)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.coordinator.process_goal(goal_id)
            backend.stuck_titles.add("A")
            clock.now += 30
            app.coordinator.process_goal(goal_id)
            decision_id = app.status(goal_id)["pending_decisions"][0]["decision_id"]
            outcome = app.resolve(goal_id, decision_id, {"verdict": "continue", "reason": "human"})
            self.assertEqual(_retries(app, goal_id), 1)
            self.assertEqual(backend.started, ["A"])
            self.assertEqual(outcome.get("tick", {}).get("action"), "budget_exhausted")
            self.assertIn("max_reworks", app.status(goal_id)["failure"]["error"])

        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app, goal_id = self._open(td, backend, no_progress_sec=30, max_reworks=1)
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.coordinator.process_goal(goal_id)
            backend.stuck_titles.add("A")
            clock.now += 30
            app.coordinator.process_goal(goal_id)
            decision_id = app.status(goal_id)["pending_decisions"][0]["decision_id"]
            workspace = Path(_by_title(app, goal_id, "A")["workspace"])
            app.resolve(goal_id, decision_id, {"verdict": "stop", "reason": "human"})
            self.assertEqual(_retries(app, goal_id), 0)
            self.assertEqual(_statuses(app, goal_id)["A"], "failed")
            self.assertTrue((workspace / "partial.txt").is_file())
            self.assertEqual(backend.started, ["A"])

    def test_wall_clock_records_checkpoint_and_worker_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            app = CollabApplication(td, backend=backend, clock=clock)
            opened = app.submit(_goal(budget={"wall_sec": 30, "max_reworks": 0}))
            goal_id = opened["goal_id"]
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.layer.add_child_task(goal_id, task=_task("B", "B"))
            app.coordinator.process_goal(goal_id)
            with app.layer._rmw():
                app.layer.goals[goal_id]["created_at"] = clock.now - 100
                app.layer._persist_unlocked()
            outcome = app.coordinator.process_goal(goal_id)
            self.assertEqual(outcome.get("action"), "budget_exhausted")
            status = app.status(goal_id)
            self.assertEqual(status["primary_failure"]["source"], "worker_timeout")
            self.assertIn("wall_sec", status["failure"]["error"])
            self.assertIsInstance(status["failure"]["elapsed"], float)
            self.assertTrue(status["failure"]["last_progress_at"])
            names = [item["name"] for item in status["failure"]["artifacts_checkpoint"]]
            self.assertIn("partial.txt", names)
            self.assertEqual(len(backend.cancelled), 2)

    def test_status_distinguishes_capacity_and_stale_without_calling_backend_again(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clock = _Clock()
            backend = _Fake(_caps(), clock)
            backend.write_partial = False
            backend.report_progress = False
            app = CollabApplication(
                td,
                backend=backend,
                clock=clock,
                max_parallel_per_goal=1,
                stale_after_sec=120,
            )
            opened = app.submit(_goal())
            goal_id = opened["goal_id"]
            app.layer.add_child_task(goal_id, task=_task("A", "A"))
            app.layer.add_child_task(goal_id, task=_task("B", "B"))
            app.coordinator.process_goal(goal_id)
            calls = len(backend.started)
            status = app.status(goal_id)
            self.assertEqual(status["progress"]["state"], "waiting_capacity")
            self.assertEqual(status["scheduler"]["waiting_reason"], "capacity")
            self.assertEqual(len(backend.started), calls)
            task = _by_title(app, goal_id, "A")
            app.layer.record_task_progress(
                goal_id,
                task["task_id"],
                {
                    "phase": "executing",
                    "source": "runner",
                    "last_heartbeat_at": utc_iso(clock.now - 121),
                    "last_progress_at": utc_iso(clock.now - 121),
                    "events": ["quiet"],
                    "artifacts_checkpoint": [],
                },
            )
            stale = app.status(goal_id)
            self.assertEqual(stale["progress"]["state"], "stale")
            self.assertEqual(stale["progress"]["recent_events"], ["quiet"])
            self.assertEqual(len(backend.started), calls)


class ClientProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_script("hermes_progress_client", ROOT / "bin" / "hermes-collab-request.py")

    def test_summary_and_progress_command_projection(self) -> None:
        mod = self.mod
        absent = mod.summarize_status({"ok": True, "state": "running", "request_id": "g"})
        self.assertEqual(absent["progress"], {"available": False, "phase": "unknown"})
        self.assertNotIn("percent", absent["progress"])
        self.assertNotIn("budget_status", absent)
        phase_only = mod.summarize_status(
            {"ok": True, "state": "running", "request_id": "g", "progress": {"phase": "writing"}}
        )
        self.assertTrue(phase_only["progress"]["available"])
        self.assertEqual(phase_only["progress"]["phase"], "writing")
        self.assertNotIn("percent", phase_only["progress"])
        explicit = mod.summarize_status(
            {
                "ok": True,
                "state": "running",
                "request_id": "g",
                "progress": {"available": False, "phase": "executing", "state": "unknown"},
                "budget_status": {"max_tokens": {"limit": 1, "used": 2, "exceeded": True, "enforced": "post_hoc"}},
            }
        )
        self.assertIs(explicit["progress"]["available"], False)
        self.assertEqual(explicit["progress"]["phase"], "executing")
        self.assertEqual(explicit["progress"]["state"], "unknown")
        self.assertNotIn("percent", explicit["progress"])
        self.assertTrue(explicit["budget_status"]["max_tokens"]["exceeded"])
        full = mod.summarize_status(
            {
                "ok": True,
                "state": "running",
                "request_id": "g",
                "progress": {
                    "available": True,
                    "phase": "executing",
                    "state": "executing",
                    "source": "runner",
                    "recent_events": ["phase executing"],
                    "subagent_observability": False,
                },
            }
        )
        self.assertEqual(full["progress"]["state"], "executing")
        self.assertEqual(full["progress"]["recent_events"], ["phase executing"])
        self.assertIs(full["progress"]["subagent_observability"], False)
        self.assertNotIn("percent", full["progress"])

        projected = mod.project_progress(
            {
                "ok": True,
                "request_id": "g1",
                "progress": {
                    "state": "stale",
                    "phase": "executing",
                    "last_heartbeat_at": "2023-11-14T22:13:20Z",
                    "last_progress_at": "2023-11-14T22:13:20Z",
                    "recent_events": ["quiet"],
                },
            },
            now=1_700_000_000 + 100,
        )
        # 2023-11-14T22:13:20Z is 1_700_000_000.
        self.assertEqual(projected["request_id"], "g1")
        self.assertEqual(projected["state"], "stale")
        self.assertEqual(projected["phase"], "executing")
        self.assertEqual(projected["heartbeat_age_sec"], 100)
        self.assertEqual(projected["progress_age_sec"], 100)
        self.assertEqual(projected["recent_events"], ["quiet"])
        self.assertNotIn("percent", projected)
        empty = mod.project_progress({"ok": True, "goal_id": "g2"}, now=1_700_000_100)
        self.assertEqual(empty["state"], "unknown")
        self.assertEqual(empty["phase"], "unknown")
        self.assertIsNone(empty["heartbeat_age_sec"])
        self.assertEqual(empty["recent_events"], [])

        parser = mod.build_parser()
        args = parser.parse_args(
            [
                "open",
                "--goal",
                "write a file",
                "--max-tokens",
                "12",
                "--max-tool-calls",
                "3",
                "--no-progress-sec",
                "30",
                "--on-no-progress",
                "fail",
                "--budget-report-only",
            ]
        )
        self.assertEqual(args.max_tokens, 12)
        self.assertEqual(args.max_tool_calls, 3)
        self.assertEqual(args.no_progress_sec, 30)
        self.assertEqual(args.on_no_progress, "fail")
        self.assertTrue(args.budget_report_only)
        self.assertEqual(args.wall_sec, 300)
        progress_args = parser.parse_args(["progress", "goal-1"])
        self.assertEqual(progress_args.cmd, "progress")
        self.assertEqual(progress_args.func, mod.cmd_progress)

        service = _load_script("collab_service_stale", ROOT / "bin" / "collab-service.py")
        defaults = service.build_parser().parse_args([])
        self.assertEqual(defaults.stale_after, 120.0)




class TokenTotalTests(unittest.TestCase):
    def test_cache_fields_are_not_added_to_the_total(self) -> None:
        from framework.progress_budget import token_total

        # Shape from issue #10: cache_read is reported on another basis.
        self.assertEqual(
            token_total({"input_tokens": 2854452, "output_tokens": 87759, "cache_read_tokens": 71085784}),
            2854452 + 87759,
        )
        self.assertEqual(token_total({"total_tokens": 2942211, "cache_read_tokens": 71085784}), 2942211)
        self.assertIsNone(token_total({"cache_read_tokens": 5}))


if __name__ == "__main__":
    unittest.main()
