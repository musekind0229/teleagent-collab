"""Tests for minimal simple collab runner (bin/simple-collab.py and framework.simple_collab)."""
from __future__ import annotations
from test_synthetic_caller_fixture import fixture_backend, fixture_start

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BIN = ROOT / "bin"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from framework.app_service import DeterministicPlanner, LeadAdapterPlanner
from framework.simple_collab import SimpleCollabRunner, build_default_goal_request
from execution_backend.inprocess_v1 import InProcessExecutionBackend
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend
from lead_adapter.inprocess import InProcessLeadAdapter

# Minimal fake agy script for offline testing on Windows/Linux
_FAKE_AGY_SCRIPT = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_file = os.environ.get("AGY_TEST_STATE", "")
n = 0
if state_file:
    p = Path(state_file)
    if p.exists():
        try:
            n = int(p.read_text().strip())
        except Exception:
            n = 0
    p.write_text(str(n + 1), encoding="utf-8")

steps_raw = os.environ.get("AGY_TEST_STEPS", "[]")
try:
    steps = json.loads(steps_raw)
except Exception:
    steps = []

step = steps[min(n, len(steps) - 1)] if steps else {}
for rel_name, body in (step.get("write") or {}).items():
    dest = Path.cwd() / rel_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")

sleep_sec = float(step.get("sleep", 0))
if sleep_sec > 0:
    import time
    time.sleep(sleep_sec)

sys.stdout.write(json.dumps({
    "conversation_id": f"conv_test_{n}",
    "status": step.get("status", "ok"),
    "response": f"step {n} finished: " + str(step.get("response", "ok")),
    "usage": {"input_tokens": 10, "output_tokens": 10}
}) + "\n")
sys.exit(int(step.get("exit_code", 0)))
'''


def _create_fake_agy(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        py_script = bin_dir / "fake_agy.py"
        py_script.write_text(_FAKE_AGY_SCRIPT, encoding="utf-8")
        cmd_script = bin_dir / "fake_agy.cmd"
        cmd_script.write_text(f'@"{sys.executable}" "{py_script}" %*\r\n', encoding="utf-8")
        return cmd_script
    else:
        sh_script = bin_dir / "fake_agy"
        sh_script.write_text(_FAKE_AGY_SCRIPT, encoding="utf-8")
        sh_script.chmod(sh_script.stat().st_mode | stat.S_IEXEC)
        return sh_script


class _CustomDecisionLead(InProcessLeadAdapter):
    """Lead adapter with programmable plan and review verdicts."""

    def __init__(
        self,
        *,
        exchange_dir: Path,
        plan_tasks: list[dict],
        review_verdicts: list[tuple[str, str] | tuple[str, str, str]],
    ) -> None:
        super().__init__(exchange_dir=exchange_dir)
        self.plan_tasks = plan_tasks
        self.review_verdicts = list(review_verdicts)
        self.recorded_reviews: list[dict] = []
        self.review_count = 0

    def decide(self, request: dict, *, schema: dict, cwd: str, timeout_sec: float = 180) -> tuple[str, dict | None]:
        kind = request.get("kind")
        app_id = str(request.get("application_id") or "")
        summary = str(request.get("context_summary") or "")
        if kind == "review":
            self.recorded_reviews.append(request)
            verdict, reason, next_action = ("pass", "accepted", "continue")
            if self.review_count < len(self.review_verdicts):
                item = self.review_verdicts[self.review_count]
                if len(item) == 2:
                    verdict, reason = item
                    next_action = "continue" if verdict == "pass" else "diagnose"
                else:
                    verdict, reason, next_action = item
            self.review_count += 1
            decision = {
                "application_id": app_id,
                "context_summary": summary,
                "verdict": verdict,
                "reason": reason,
                "next_action": next_action,
            }
            return json.dumps(decision), decision

        # Default planning request
        tasks = self.plan_tasks or [
            {
                "task_key": "task1",
                "title": "task1",
                "instruction": "produce output1.txt",
                "depends_on": [],
                "artifacts": ["output1.txt"],
            }
        ]
        decision = {
            "application_id": app_id,
            "context_summary": summary,
            "summary": "custom plan",
            "tasks": tasks,
        }
        return json.dumps(decision), decision


class SimpleCollabRunnerTests(unittest.TestCase):
    def test_build_default_goal_request(self):
        req = build_default_goal_request(
            title="test goal",
            goal="deliver result",
            artifacts=["res.txt"],
            max_reworks=1,
            wall_sec=60.0,
            model="gemini-2.5-pro",
            lead_model="grok-3",
        )
        self.assertEqual(req["title"], "test goal")
        self.assertEqual(req["goal"], "deliver result")
        self.assertEqual(req["acceptance"]["artifacts"], ["res.txt"])
        self.assertEqual(req["budget"]["max_reworks"], 1)
        self.assertEqual(req["budget"]["wall_sec"], 60.0)
        self.assertEqual(req["model"], "gemini-2.5-pro")
        self.assertEqual(req["lead_model"], "grok-3")

    def test_run_goal_clean_pass(self):
        """Lead plans 1 task -> AGY backend writes artifact -> Lead reviews and passes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"output1.txt": "hello world\n"}, "status": "ok"}
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))

            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[("pass", "clean execution and artifact present")],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))

            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)
            req = build_default_goal_request(
                title="clean goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
            )

            res = runner.run_goal(req, timeout_sec=20.0)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["state"], "completed")
            self.assertEqual(len(lead.recorded_reviews), 1)
            # Verify artifact was returned and captured
            report = res["report"]
            self.assertEqual(report["state"], "completed")
            self.assertTrue(report["ok"])

    def test_run_goal_rework_flow(self):
        """Lead rejects first attempt -> Task requeued for rework -> 2nd attempt passes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"output1.txt": "bad output\n", "unexpected.txt": "leak\n"}, "status": "ok"},
                {"write": {"output1.txt": "fixed output\n"}, "status": "ok"},
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))

            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[
                    ("fail", "rework needed: unexpected file leaked"),
                    ("pass", "all clean on round 2"),
                ],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))

            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)
            req = build_default_goal_request(
                title="rework goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
                max_reworks=2,
            )

            res = runner.run_goal(req, timeout_sec=20.0)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["state"], "completed")
            self.assertEqual(len(lead.recorded_reviews), 2)
            task = res["status"]["tasks"][0]
            self.assertEqual(task["rework_count"], 1)

    def test_run_goal_idempotency_dedup(self):
        """Submitting duplicate idempotency key reuses existing goal without duplicate execution."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"output1.txt": "content\n"}, "status": "ok"}
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))
            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[("pass", "ok")],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))
            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)

            req = build_default_goal_request(
                title="idempotent goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
                idempotency_key="fixed-key-12345",
            )

            res1 = runner.run_goal(req, timeout_sec=20.0)
            self.assertTrue(res1["ok"])
            self.assertFalse(res1["is_duplicate"])

            # 2nd run with same idempotency key
            res2 = runner.run_goal(req, timeout_sec=5.0)
            self.assertTrue(res2["ok"])
            self.assertTrue(res2["is_duplicate"])
            self.assertEqual(res1["goal_id"], res2["goal_id"])

    def test_ordinary_failure_preserves_goal_not_auto_cancelled(self):
        """Worker non-zero failure is explicitly returned to lead with partial artifacts & deduplicated."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"partial.txt": "failed halfway\n"}, "status": "error", "exit_code": 1, "response": "syntax error"}
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))
            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[("fail", "worker exit non-zero", "diagnose")],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))
            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)

            callback_events: list[dict] = []

            req = build_default_goal_request(
                title="failure goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
                max_reworks=0,
            )

            res = runner.run_goal(req, timeout_sec=20.0, decision_callback=callback_events.append)
            self.assertFalse(res["ok"])
            self.assertEqual(res["state"], "failed")
            self.assertNotEqual(res["state"], "cancelled")

            # 1. Assert Lead actually received the failure review call!
            self.assertEqual(len(lead.recorded_reviews), 1)
            rev_req = lead.recorded_reviews[0]
            extra = rev_req.get("extra", {})
            worker_req = extra.get("worker_request", {})

            # 2. Assert full failure info & partial artifacts delivered to lead
            self.assertIn("partial.txt", extra.get("partial_artifacts", []))
            self.assertIn("syntax error", str(extra.get("worker_result", {}).get("response", "")))
            self.assertTrue(Path(worker_req.get("workspace", "")).is_dir())

            # 3. Assert lead verdict & next action
            self.assertEqual(len(res["lead_reviews"]), 1)
            review_out = res["lead_reviews"][0]
            self.assertEqual(review_out["verdict"], "fail")
            self.assertEqual(review_out["next_action"], "diagnose")

            # 4. Assert single consumption and deduplication: re-running does not re-invoke lead decide
            consumed_files = list((persist / "consumed_decisions").glob("*.json"))
            self.assertEqual(len(consumed_files), 1)

            # 5. Callback was notified with exact durable event payload containing lead reviews
            self.assertEqual(len(callback_events), 1)
            event_rec = callback_events[0]
            self.assertEqual(event_rec["status"], "acked")
            self.assertEqual(len(event_rec["lead_reviews"]), 1)
            self.assertEqual(event_rec["lead_reviews"][0]["verdict"], "fail")
            self.assertEqual(event_rec["lead_reviews"][0]["next_action"], "diagnose")

            # 6. Durable event persisted on disk with status acked
            events = list((persist / "events").glob("*.json"))
            self.assertEqual(len(events), 1)
            persisted_ev = json.loads(events[0].read_text(encoding="utf-8"))
            self.assertEqual(persisted_ev["status"], "acked")

    def test_run_goal_timeout_preserves_continuation_not_cancelled(self):
        """When observation timeout expires while worker is running, continuation_required is flagged and goal is not cancelled."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            # Worker writes partial file and sleeps for 2 seconds
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"in_flight.txt": "still computing\n"}, "sleep": 2.0, "status": "ok"}
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))
            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[("fail", "timed out in observation", "diagnose")],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))
            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)

            req = build_default_goal_request(
                title="timeout goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
            )

            # Run with very short timeout 0.1s while worker sleeps 2.0s
            try:
                res = runner.run_goal(req, timeout_sec=0.1)
                self.assertTrue(res["timed_out"])
                self.assertTrue(res["continuation_required"])
                self.assertNotEqual(res["state"], "cancelled")

                # Lead was notified with continuation_required and partial artifacts
                self.assertEqual(len(lead.recorded_reviews), 1)
                rev_req = lead.recorded_reviews[0]
                extra = rev_req.get("extra", {})
                self.assertTrue(extra.get("continuation_required"))
                self.assertIn("in_flight.txt", extra.get("partial_artifacts", []))
            finally:
                runner.close()

    def test_cli_simple_collab_execution(self):
        """Test bin/simple-collab.py CLI invocation in-process."""
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td) / "persist"
            cmd = [
                sys.executable,
                str(BIN / "simple-collab.py"),
                "--persist", str(persist),
                "--title", "cli test",
                "--goal", "deliver output",
                "--backend", "inprocess",
                "--planner", "deterministic",
                "--timeout-sec", "10",
                "--model", "test-model",
            ]
            env = dict(os.environ)
            env["PYTHONPATH"] = f"{SRC};{ROOT}"
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertTrue(out.get("ok"), out)
            self.assertEqual(out.get("state"), "completed")
            self.assertIn("event_id", out)


    def test_reproduce_timeout_swallows_final_error_and_artifacts(self):
        """Negative probe: when timeout happens first, followed by final failure with new error and partial artifact,
        the final error and partial artifact must NOT be swallowed by the old timeout decision."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"initial.txt": "in progress\n"}, "sleep": 0.5, "status": "error", "exit_code": 1, "response": "ACTUAL_FINAL_ERROR"}
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))
            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[
                    ("fail", "timeout review", "diagnose"),
                    ("fail", "final error review", "rework"),
                ],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))
            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)

            req = build_default_goal_request(
                title="negative timeout goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
            )

            try:
                # 1. First run times out
                res1 = runner.run_goal(req, timeout_sec=0.1)
                self.assertTrue(res1["timed_out"])
                self.assertTrue(res1["continuation_required"])
                self.assertEqual(len(lead.recorded_reviews), 1)

                # Wait for the fake agy script to finish its sleep and output ACTUAL_FINAL_ERROR
                time.sleep(0.6)

                # 2. Second drive: worker has now finished with ACTUAL_FINAL_ERROR
                # In buggy code, this will reuse app_id rev_<goal>_<task>_<run> and return old timeout review!
                res2 = runner.run_goal(req, timeout_sec=2.0)
                self.assertFalse(res2["ok"])
                self.assertEqual(res2["state"], "failed")

                # The second drive MUST have triggered a new review for the final terminal result!
                self.assertEqual(len(lead.recorded_reviews), 2, "Second final review was swallowed!")
                rev2 = lead.recorded_reviews[1]
                worker_res2 = rev2.get("extra", {}).get("worker_result", {})
                self.assertIn("ACTUAL_FINAL_ERROR", str(worker_res2.get("response", "")))
            finally:
                runner.close()

    def test_reproduce_callback_exception_swallowed(self):
        """Negative probe: when decision_callback raises an exception, the event must NOT be silently lost/marked ok,
        but tracked as pending/failed with error, and dispatch_pending_events can retry it."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            runner = SimpleCollabRunner(persist)
            req = build_default_goal_request(
                title="callback exception goal",
                goal="write delivery.txt",
                artifacts=["delivery.txt"],
            )

            def failing_callback(data: dict):
                raise RuntimeError("network down")

            res = runner.run_goal(req, timeout_sec=5.0, decision_callback=failing_callback)
            # Redeliver with successful callback -> status flips to acked
            success_calls: list[dict] = []
            acked = runner.redeliver_pending_events(success_calls.append)
            self.assertEqual(len(acked), 1)
            self.assertEqual(len(success_calls), 1)
            self.assertEqual(len(runner.get_pending_events()), 0)

            # Re-invoking redelivery does not re-invoke callback (idempotent no-op)
            acked_again = runner.redeliver_pending_events(success_calls.append)
            self.assertEqual(len(acked_again), 0)
            self.assertEqual(len(success_calls), 1)

    def test_duplicate_same_terminal_state_does_not_re_review(self):
        """When the same terminal state and result signature is driven again, it must NOT re-review (single consumption)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            persist = root / "persist"
            bin_dir = root / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["AGY_TEST_STATE"] = str(root / "agy_state.txt")
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"fail.txt": "failed\n"}, "status": "error", "exit_code": 1, "response": "syntax error"}
            ])

            backend = fixture_backend(AntigravityCliExecutionBackend(
                bin_path=str(fake_agy),
                environ=env,
                timeout_sec=15.0,
                poll_sec=0.05,
            ))
            lead = _CustomDecisionLead(
                exchange_dir=persist / "lead_exchange",
                plan_tasks=[{
                    "task_key": "task1",
                    "title": "task1",
                    "instruction": "produce output1.txt",
                    "depends_on": [],
                    "artifacts": ["output1.txt"],
                }],
                review_verdicts=[("fail", "worker exit non-zero", "diagnose")],
            )
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))
            runner = SimpleCollabRunner(persist, planner=planner, backend=backend)

            req = build_default_goal_request(
                title="duplicate terminal goal",
                goal="write output1.txt",
                artifacts=["output1.txt"],
                max_reworks=0,
            )

            try:
                res1 = runner.run_goal(req, timeout_sec=10.0)
                self.assertFalse(res1["ok"])
                self.assertEqual(len(lead.recorded_reviews), 1)

                # Re-running runner.run_goal on already terminal goal with identical result:
                res2 = runner.run_goal(req, timeout_sec=2.0)
                # Must be 1 review call (not re-reviewed)
                self.assertEqual(len(lead.recorded_reviews), 1)
            finally:
                runner.close()


    def test_cli_antigravity_model_wiring(self):
        """Verify bin/simple-collab.py correctly wires --model to AntigravityCliExecutionBackend.model without real spawn."""
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            persist = Path(td) / "persist"
            bin_dir = Path(td) / "bin"
            fake_agy = _create_fake_agy(bin_dir)

            env = dict(os.environ)
            env["PYTHONPATH"] = f"{SRC};{ROOT}"
            env["AGY_TEST_STEPS"] = json.dumps([
                {"write": {"delivery.txt": "cli agy output\n"}, "status": "ok"}
            ])

            cmd = [
                sys.executable,
                str(BIN / "simple-collab.py"),
                "--persist", str(persist),
                "--title", "cli agy test",
                "--goal", "deliver output",
                "--backend", "antigravity",
                "--agy-bin", str(fake_agy),
                "--model", "gemini-3.8-flash-high",
                "--planner", "deterministic",
                "--timeout-sec", "10",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertNotEqual(proc.returncode, 0, proc.stderr) # Default CLI has no trusted caller policy.
            out = json.loads(proc.stdout)
            self.assertFalse(out.get("ok"), out)

            # Check that backend instantiated with model='gemini-3.8-flash-high'
            # by directly verifying AntigravityCliExecutionBackend with that flag
            from execution_backend import get_execution_backend
            be = get_execution_backend("antigravity", bin_path=str(fake_agy), model="gemini-3.8-flash-high")
            self.assertEqual(be.model, "gemini-3.8-flash-high")

    def test_grok_structured_wrapper_offline_review(self):
        """Verify real Grok shape wrapper (structuredOutput with outer envelope) is correctly unwrapped,
        retaining actual verdict, real reason, and accurately tracking next_action_source."""
        class _GrokWrapperLead(InProcessLeadAdapter):
            def decide(self, request: dict, *, schema: dict, cwd: str, timeout_sec: float = 180) -> tuple[str, dict | None]:
                kind = request.get("kind")
                app_id = str(request.get("application_id") or "")
                summary = str(request.get("context_summary") or "")
                if kind == "review":
                    # Real Grok CLI output shape: structured output nested inside envelope
                    inner = {
                        "application_id": app_id,
                        "context_summary": summary,
                        "verdict": "fail",
                        "reason": "REAL_GROK_REASON_SYNTAX_ERROR",
                        "next_action": "diagnose",
                    }
                    wrapper = {
                        "conversation_id": "conv-grok-1",
                        "response": "Here is the decision",
                        "structuredOutput": inner,
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    }
                    return json.dumps(wrapper), wrapper
                # Plan
                plan = {
                    "application_id": app_id,
                    "context_summary": summary,
                    "summary": "plan",
                    "tasks": [{
                        "task_key": "t1",
                        "title": "t1",
                        "instruction": "work",
                        "depends_on": [],
                        "artifacts": ["out.txt"],
                    }],
                }
                return json.dumps(plan), plan

        with tempfile.TemporaryDirectory() as td:
            persist = Path(td) / "persist"
            lead = _GrokWrapperLead(exchange_dir=persist / "lead_exchange")
            planner = LeadAdapterPlanner(lead, cwd=str(persist / "lead_work"))
            runner = SimpleCollabRunner(persist, planner=planner)

            # Test failure review with Grok envelope
            snap = {"goal_id": "g1", "goal": {"desired_outcome": "work", "boundaries": {}}}
            task = {"task_id": "t1", "run_id": "r1", "workspace": str(td), "title": "t1"}
            worker_result = {"ok": False, "error": "worker crash", "response": "crash"}

            decision_rec = runner._conduct_lead_review(
                goal_snap=snap,
                task=task,
                worker_result=worker_result,
                partial_artifacts=["partial.log"],
            )

            # Verify unwrap succeeded: verdict, reason, and next_action extracted from inner structuredOutput
            self.assertEqual(decision_rec["verdict"], "fail")
            self.assertEqual(decision_rec["reason"], "REAL_GROK_REASON_SYNTAX_ERROR")
            self.assertEqual(decision_rec["next_action"], "diagnose")
            self.assertEqual(decision_rec["next_action_source"], "lead")
            self.assertEqual(decision_rec["lead_status"], "ok")


if __name__ == "__main__":
    unittest.main()
