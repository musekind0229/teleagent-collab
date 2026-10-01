"""Task failures show up on Goal status, with the dispatch reason kept."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from framework.app_service import CollabApplication

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "hermes-collab-request.py"

_TIMEOUT_NEXT = (
    "raise budget.wall_sec or split the task; open a NEW request citing this request_id"
)
_SPAWN_NEXT = "check collab-service --ready"
_STDOUT = "STDOUT_SECRET_BLOB"
_GOAL = "GOAL_CONTRACT_SECRET"


def _load_client():
    spec = importlib.util.spec_from_file_location("hermes_collab_request_failure_projection", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HCR = _load_client()


class _FakeResp:
    def __init__(self, body: dict) -> None:
        self._raw = json.dumps(body).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _RaiseBackend:
    backend_id = "fake.raise"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        raise self.exc

    def observe_run(self, run_id, **kwargs):
        return {"busy": False}

    def collect_result(self, run_id):
        return {"ok": False, "error": "not reached", "stdout": _STDOUT}

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id}


class _TimeoutBackend:
    backend_id = "fake.timeout"

    def start_run(self, *, title, directory, instruction="", artifacts=None, charter=None):
        return {"ok": True, "backend": self.backend_id, "run_id": "run-timeout", "native_handle": "run-timeout"}

    def observe_run(self, run_id, **kwargs):
        return {"busy": False, "finish_successful": False}

    def collect_result(self, run_id):
        return {
            "ok": False,
            "run_id": run_id,
            "error": "timeout",
            "missing": ["delivery.txt", "notes.md"],
            "stdout": _STDOUT,
            "response": "WORKER_RESPONSE_SECRET",
        }

    def list_pending_actions(self, *, session_id=None):
        return 200, []

    def cancel(self, run_id):
        return 200, {"ok": True, "run_id": run_id}


def _request(key: str) -> dict:
    return {
        "idempotency_key": key,
        "title": "Ship the file",
        "goal": _GOAL,
        "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }


def _projection_blob(status: dict) -> str:
    brief = {
        "failure_reason": status.get("failure_reason"),
        "primary_failure": status.get("primary_failure"),
        "failures": status.get("failures"),
    }
    return json.dumps(brief, ensure_ascii=False)


class FailureProjectionTests(unittest.TestCase):
    def test_worker_timeout_projects_reason_source_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_TimeoutBackend())
            opened = app.submit(_request("timeout-1"))
            tick = app.coordinator.process_goal(opened["goal_id"])
            self.assertEqual(tick.get("action"), "task_finished", tick)
            status = app.status(opened["goal_id"])
        self.assertEqual(status["state"], "failed")
        self.assertIsNone(status["failure"])
        self.assertTrue(str(status.get("failure_reason") or "").strip())
        self.assertNotIn("\n", status["failure_reason"])
        primary = status["primary_failure"]
        self.assertEqual(primary["source"], "worker_timeout")
        self.assertEqual(primary["error"], "timeout")
        self.assertEqual(primary["missing_artifacts"], ["delivery.txt", "notes.md"])
        self.assertEqual(primary["run_id"], "run-timeout")
        self.assertEqual(primary["title"], "Ship the file")
        self.assertIs(primary["retryable"], False)
        self.assertEqual(primary["next_step"], _TIMEOUT_NEXT)
        self.assertEqual(status["failure_reason"], "timeout")
        self.assertEqual(len(status["failures"]), 1)
        self.assertEqual(status["failures"][0]["task_id"], primary["task_id"])
        blob = _projection_blob(status)
        self.assertNotIn(_STDOUT, blob)
        self.assertNotIn("WORKER_RESPONSE_SECRET", blob)
        self.assertNotIn(_GOAL, blob)
        self.assertNotIn("desired_outcome", blob)

    def test_spawn_runtimeerror_keeps_the_message(self) -> None:
        message = "no TeleAgent process (verified=0)"
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_RaiseBackend(RuntimeError(message)))
            opened = app.submit(_request("spawn-1"))
            app.coordinator.process_goal(opened["goal_id"])
            status = app.status(opened["goal_id"])
        self.assertEqual(status["state"], "failed")
        self.assertIsNone(status["failure"])
        result = status["tasks"][0]["result"]
        self.assertEqual(result["error_source"], "spawn")
        self.assertEqual(
            result["error"],
            f"backend dispatch failed: RuntimeError: {message}",
        )
        self.assertIn(message, result["error"])
        primary = status["primary_failure"]
        self.assertEqual(primary["source"], "spawn")
        self.assertIn(message, primary["error"])
        self.assertIn(message, status["failure_reason"])
        self.assertEqual(primary["run_id"], "")
        self.assertEqual(primary["missing_artifacts"], [])
        self.assertIs(primary["retryable"], False)
        self.assertEqual(primary["next_step"], _SPAWN_NEXT)
        self.assertNotIn(_GOAL, _projection_blob(status))

    def test_spawn_empty_message_keeps_the_type_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_RaiseBackend(RuntimeError()))
            opened = app.submit(_request("spawn-empty"))
            app.coordinator.process_goal(opened["goal_id"])
            status = app.status(opened["goal_id"])
        result = status["tasks"][0]["result"]
        self.assertEqual(result["error"], "backend dispatch failed: RuntimeError")
        self.assertEqual(result["error_source"], "spawn")
        self.assertEqual(status["primary_failure"]["source"], "spawn")
        self.assertEqual(status["failure_reason"], "backend dispatch failed: RuntimeError")

    def test_spawn_redacts_secret_looking_exception_text(self) -> None:
        secret = "token=sk-ABCDEFGHIJKLMNOP1234"
        message = f"no TeleAgent process (verified=0) {secret}"
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td, backend=_RaiseBackend(RuntimeError(message)))
            opened = app.submit(_request("spawn-secret"))
            app.coordinator.process_goal(opened["goal_id"])
            status = app.status(opened["goal_id"])
        dumped = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(secret, dumped)
        self.assertNotIn("sk-ABCDEFGHIJKLMNOP1234", dumped)
        primary = status["primary_failure"]
        self.assertEqual(primary["source"], "spawn")
        self.assertIn("[redacted]", primary["error"])
        self.assertIn("no TeleAgent process (verified=0)", primary["error"])
        self.assertIn("[redacted]", status["tasks"][0]["result"]["error"])
        self.assertNotIn(secret, status["failure_reason"])

    def test_two_failed_tasks_stay_two_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            sub = app.layer.submit_goal(
                submit_key="two-fail",
                title="two",
                desired_outcome="both fail",
                tasks=[
                    {"task_id": "task_a", "title": "Alpha"},
                    {"task_id": "task_b", "title": "Beta"},
                ],
            )
            self.assertTrue(sub.get("ok"), sub)
            gid = sub["goal_id"]
            self.assertTrue(app.layer.start_task(gid, "task_a").get("ok"))
            self.assertTrue(app.layer.start_task(gid, "task_b").get("ok"))
            # Finish the later task first. Plan order, not finish time, picks primary.
            self.assertTrue(
                app.layer.finish_task(
                    gid,
                    "task_b",
                    succeeded=False,
                    result={"ok": False, "error": "second broke", "run_id": "run-b", "stdout": _STDOUT},
                ).get("ok")
            )
            self.assertTrue(
                app.layer.finish_task(
                    gid,
                    "task_a",
                    succeeded=False,
                    result={"ok": False, "error": "first broke", "run_id": "run-a"},
                ).get("ok")
            )
            status = app.status(gid)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(len(status["failures"]), 2)
        self.assertEqual([row["task_id"] for row in status["failures"]], ["task_a", "task_b"])
        self.assertEqual([row["error"] for row in status["failures"]], ["first broke", "second broke"])
        self.assertEqual(status["primary_failure"]["task_id"], "task_a")
        self.assertEqual(status["failure_reason"], "first broke")
        self.assertNotIn("second broke", status["failure_reason"])
        self.assertNotIn(_STDOUT, _projection_blob(status))
        self.assertFalse(status["need_human"])

    def test_awaiting_decision_has_no_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            sub = app.layer.submit_goal(
                submit_key="ask-1",
                title="ask",
                desired_outcome="need a person",
                tasks=[{"task_id": "task_q", "title": "Ask"}],
            )
            self.assertTrue(sub.get("ok"), sub)
            gid = sub["goal_id"]
            self.assertTrue(app.layer.start_task(gid, "task_q").get("ok"))
            opened = app.layer.open_decision(
                gid,
                kind="question",
                task_id="task_q",
                title="Need a choice",
                reason="which file",
            )
            self.assertTrue(opened.get("ok"), opened)
            status = app.status(gid)
        self.assertTrue(status["awaiting_decision"])
        self.assertNotEqual(status["state"], "failed")
        self.assertNotIn("primary_failure", status)
        self.assertNotIn("failures", status)
        self.assertEqual(status["failure_reason"], "")
        self.assertFalse(status["need_human"])

    def test_source_labels_and_need_human_failure_stays(self) -> None:
        cases = (
            ({"ok": False, "error": "contract_render_error: boom"}, "contract_render", False),
            (
                {"ok": False, "error": "acceptance_failed: body mismatch", "acceptance_failed": True},
                "acceptance",
                False,
            ),
            ({"ok": False, "error": "artifact_contaminated: CONTAMINATED delivery.txt: AI生成x1"}, "contamination", False),
            ({"ok": False, "error": "cancelled", "state": "cancelled"}, "cancelled", False),
            ({"ok": False, "error": "worker crashed"}, "task_failed", False),
            (
                {"ok": False, "error": "need_human: budget_exceeded wall wall_s=12 max=10"},
                "worker_timeout",
                True,
            ),
            (
                {"ok": False, "error": "need_human: budget_exceeded steps steps=6 max=5"},
                "task_failed",
                True,
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(td)
            for index, (result, source, need_human) in enumerate(cases):
                key = f"label-{index}"
                sub = app.layer.submit_goal(
                    submit_key=key,
                    title=key,
                    desired_outcome=key,
                    tasks=[{"task_id": f"t-{index}", "title": key}],
                )
                self.assertTrue(sub.get("ok"), sub)
                gid = sub["goal_id"]
                tid = f"t-{index}"
                self.assertTrue(app.layer.start_task(gid, tid).get("ok"), key)
                finished = app.layer.finish_task(gid, tid, succeeded=False, result=result)
                self.assertTrue(finished.get("ok"), finished)
                status = app.status(gid)
                self.assertEqual(status["primary_failure"]["source"], source, result)
                self.assertTrue(str(status.get("failure_reason") or "").strip(), result)
                self.assertIs(status["need_human"], need_human, result)
                if need_human:
                    self.assertTrue((status.get("failure") or {}).get("need_human"), result)
                    self.assertIn("budget_exceeded", status["failure_reason"])
                else:
                    self.assertIsNone(status.get("failure"), result)

    def test_client_summary_and_wait_task_timeout(self) -> None:
        primary = {
            "task_id": "t1",
            "run_id": "r1",
            "title": "work",
            "error": "timeout",
            "source": "worker_timeout",
            "missing_artifacts": ["delivery.txt"],
            "retryable": False,
            "next_step": _TIMEOUT_NEXT,
            "stdout": _STDOUT,
            "desired_outcome": _GOAL,
        }
        second = {
            "task_id": "t2",
            "run_id": "r2",
            "title": "other",
            "error": "worker crashed",
            "source": "task_failed",
            "missing_artifacts": [],
            "retryable": False,
            "next_step": "inspect the task error and open a NEW request citing this request_id",
        }
        summary = HCR.summarize_status(
            {
                "ok": True,
                "state": "failed",
                "request_id": "g1",
                "goal": {"desired_outcome": _GOAL},
                "primary_failure": primary,
                "failures": [primary, second],
                "tasks": [
                    {
                        "task_id": "t1",
                        "title": "work",
                        "status": "failed",
                        "result": {"error": "timeout", "stdout": _STDOUT, "response": "WORKER_RESPONSE_SECRET"},
                    }
                ],
            }
        )
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(_STDOUT, text)
        self.assertNotIn(_GOAL, text)
        self.assertNotIn("WORKER_RESPONSE_SECRET", text)
        self.assertNotIn("desired_outcome", summary["primary_failure"])
        self.assertNotIn("stdout", summary["primary_failure"])
        self.assertEqual(summary["failure_reason"], "timeout")
        self.assertEqual(summary["failure_count"], 2)
        self.assertEqual(summary["primary_failure"]["source"], "worker_timeout")
        self.assertEqual(summary["primary_failure"]["missing_artifacts"], ["delivery.txt"])
        self.assertEqual(summary["primary_failure"]["next_step"], _TIMEOUT_NEXT)

        kept = HCR.summarize_status(
            {
                "ok": True,
                "state": "failed",
                "request_id": "g1",
                "failure_reason": "need a human",
                "primary_failure": primary,
                "failures": [primary],
            }
        )
        self.assertEqual(kept["failure_reason"], "need a human")
        self.assertEqual(kept["failure_count"], 1)

        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "failed",
                    "request_id": "g1",
                    "goal": {"desired_outcome": _GOAL},
                    "primary_failure": primary,
                    "failures": [primary],
                    "tasks": [
                        {
                            "task_id": "t1",
                            "status": "failed",
                            "result": {"error": "timeout", "stdout": _STDOUT},
                        }
                    ],
                }
            )

        code, printed = _run_client(["wait", "g1", "--timeout", "5", "--interval", "0.01"], fake_urlopen)
        self.assertEqual(code, 2)
        self.assertNotIn(_STDOUT, printed)
        self.assertNotIn(_GOAL, printed)
        body = json.loads(printed)
        self.assertEqual(body["wait"]["kind"], "task_failed")
        self.assertIs(body["wait"]["task_timeout"], True)
        self.assertEqual(body["failure_reason"], "timeout")
        self.assertEqual(body["failure_count"], 1)
        self.assertEqual(body["primary_failure"]["source"], "worker_timeout")

        def crashed(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "failed",
                    "request_id": "g1",
                    "failure_reason": "worker crashed",
                    "primary_failure": {**second, "task_id": "t1"},
                    "failures": [second],
                }
            )

        code, printed = _run_client(["wait", "g1", "--timeout", "5", "--interval", "0.01"], crashed)
        self.assertEqual(code, 2)
        crashed_wait = json.loads(printed)["wait"]
        self.assertEqual(crashed_wait["kind"], "task_failed")
        self.assertNotIn("task_timeout", crashed_wait)


def _run_client(argv: list[str], urlopen) -> tuple[int, str]:
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        missing = str(Path(td) / "missing.env")
        env = {
            "COLLAB_API_BASE": "http://127.0.0.1:8765",
            "COLLAB_API_TOKEN": "",
            "COLLAB_ENV_FILE": missing,
            "COLLAB_JSON_UNICODE": "",
            "COLLAB_OUTPUT_FULL": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                with mock.patch("sys.stdout", buf):
                    code = HCR.main(argv)
    return code, buf.getvalue()


if __name__ == "__main__":
    unittest.main()
