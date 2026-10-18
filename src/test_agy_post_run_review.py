#!/usr/bin/env python3
"""Post-run lead review on the agy path: scope check, lead verdict, bounded rework."""
from __future__ import annotations
from test_synthetic_caller_fixture import fixture_backend, fixture_start

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402
from framework import agy_review  # noqa: E402
from framework.app_service import (  # noqa: E402
    CollabApplication,
    DeterministicPlanner,
    LeadAdapterPlanner,
)

A_BODY = "alpha from task A\n"

# One invocation per dispatch. AGY_FAKE_STEPS is a JSON list; invocation n runs
# step min(n, last): {"write": {relative name: body}}. The counter lives outside
# the workspace so the worker under test cannot see it.
_FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = Path(os.environ["AGY_FAKE_STATE"])
n = int(state.read_text()) if state.exists() else 0
state.write_text(str(n + 1))
steps = json.loads(os.environ["AGY_FAKE_STEPS"])
step = steps[min(n, len(steps) - 1)]
for name, body in (step.get("write") or {}).items():
    p = Path.cwd() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
sys.stdout.write(json.dumps({"conversation_id": "conv_%d" % n, "status": "ok",
                             "response": "step %d done" % n,
                             "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n")
'''


def _write_fake(dirpath: Path) -> Path:
    if os.name == "nt":
        script = dirpath / "fake-agy.py"
        script.write_text(_FAKE_AGY, encoding="utf-8")
        cmd = dirpath / "fake-agy.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return cmd
    path = dirpath / "fake-agy"
    path.write_text(_FAKE_AGY, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class _ScopeLead:
    """Plans A → B; fails a review exactly when the coordinator saw out-of-scope changes."""

    def __init__(self) -> None:
        self.reviews: list[dict] = []

    def decide(self, request, *, schema, cwd, timeout_sec=180):
        base = {"application_id": request["application_id"], "context_summary": request["context_summary"]}
        if request.get("kind") == "review":
            payload = (request.get("extra") or {}).get("worker_request") or {}
            self.reviews.append(payload)
            bad = payload.get("out_of_scope_changes") or []
            out = {
                **base,
                "verdict": "fail" if bad else "pass",
                "reason": "undo changes to " + ", ".join(r["path"] for r in bad) if bad else "only own artifacts changed",
            }
            return json.dumps(out), out
        out = {
            **base,
            "summary": "A then B",
            "tasks": [
                {"task_key": "a", "title": "Write A", "instruction": "write a.txt",
                 "depends_on": [], "artifacts": ["a.txt"]},
                {"task_key": "b", "title": "Write B", "instruction": "write b.txt",
                 "depends_on": ["a"], "artifacts": ["b.txt"]},
            ],
        }
        return json.dumps(out), out


def _body(max_reworks: int) -> dict:
    return {
        "idempotency_key": f"review-{max_reworks}-{time.time_ns()}",
        "client_id": "test-suite",
        "title": "scope review",
        "goal": "write a.txt then b.txt",
        "acceptance": {"artifacts": ["a.txt", "b.txt"]},
        "budget": {"wall_sec": 30, "max_reworks": max_reworks},
    }


def _drive(app: CollabApplication, goal_id: str, *, seconds: float = 20.0) -> dict:
    deadline = time.time() + seconds
    status = app.status(goal_id)
    while time.time() < deadline and status["state"] not in {"completed", "failed", "cancelled"}:
        app.coordinator.process_goal(goal_id)
        status = app.status(goal_id)
        time.sleep(0.05)
    return status


def _task(status: dict, title: str) -> dict:
    return next(t for t in status["tasks"] if t["title"] == title)


class PostRunReviewFlowTests(unittest.TestCase):
    def _app(self, root: Path, steps: list[dict], planner) -> CollabApplication:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = os.environ.copy()
        for key in ("AGY_AUTO_APPROVE", "COLLAB_AGY_AUTO_APPROVE", "COLLAB_AGY_ACCOUNT_POOL"):
            env.pop(key, None)
        env["AGY_FAKE_STATE"] = str(root / "fake-state.txt")
        env["AGY_FAKE_STEPS"] = json.dumps(steps)
        backend = fixture_backend(AntigravityCliExecutionBackend(
            bin_path=str(_write_fake(bin_dir)), environ=env, timeout_sec=15, poll_sec=0.05
        ))
        return CollabApplication(root / "app", planner=planner, backend=backend)

    def test_clean_run_passes_lead_review(self):
        lead = _ScopeLead()
        with tempfile.TemporaryDirectory() as td:
            app = self._app(Path(td), [{"write": {"a.txt": A_BODY}}, {"write": {"b.txt": "beta\n"}}],
                            LeadAdapterPlanner(lead, cwd=td))
            status = _drive(app, app.submit(_body(1))["goal_id"])
        self.assertEqual(status["state"], "completed", status)
        self.assertEqual(status["acceptance_status"]["technical_review"], "passed_by_lead")
        b = _task(status, "Write B")
        self.assertEqual(b["result"]["review"]["status"], "passed")
        self.assertEqual(b["result"]["scope_report"]["out_of_scope_changes"], [])
        self.assertEqual(len(lead.reviews), 2)
        self.assertNotIn("acceptance text was not independently verified", b["result"].get("warnings") or [])

    def test_overreach_is_sent_back_restored_and_then_passes(self):
        lead = _ScopeLead()
        steps = [
            {"write": {"a.txt": A_BODY}},
            {"write": {"b.txt": "beta\n", "a.txt": "rewritten by B\n"}},  # overreach
            {"write": {"b.txt": "beta v2\n"}},  # rework stays in scope
        ]
        with tempfile.TemporaryDirectory() as td:
            app = self._app(Path(td), steps, LeadAdapterPlanner(lead, cwd=td))
            goal_id = app.submit(_body(1))["goal_id"]
            status = _drive(app, goal_id)
            b = _task(status, "Write B")
            a_in_b = (Path(b["workspace"]) / "a.txt").read_text(encoding="utf-8")
        self.assertEqual(status["state"], "completed", status)
        self.assertEqual(b["rework_count"], 1)
        self.assertEqual([r["status"] for r in b["result"]["review_history"]], ["failed", "passed"])
        first = lead.reviews[1]
        self.assertEqual([(r["path"], r["status"]) for r in first["out_of_scope_changes"]], [("a.txt", "modified")])
        self.assertIn("rewritten by B", first["diff"])
        self.assertEqual(b["rework_feedback"][0]["restored"], ["a.txt"])
        self.assertEqual(a_in_b, A_BODY)  # collab put A's file back before the rework

    def test_overreach_without_rework_budget_fails(self):
        lead = _ScopeLead()
        steps = [{"write": {"a.txt": A_BODY}}, {"write": {"b.txt": "beta\n", "a.txt": "rewritten\n"}}]
        with tempfile.TemporaryDirectory() as td:
            app = self._app(Path(td), steps, LeadAdapterPlanner(lead, cwd=td))
            status = _drive(app, app.submit(_body(0))["goal_id"])
        self.assertEqual(status["state"], "failed", status)
        b = _task(status, "Write B")
        self.assertEqual(b["status"], "failed")
        self.assertIn("lead review failed after 0 rework(s)", b["result"]["error"])
        self.assertEqual(status["acceptance_status"]["technical_review"], "failed_by_lead")

    def test_overreach_on_manifest_input_is_restored_and_rework_restages(self):
        class _OneTaskLead(_ScopeLead):
            def decide(self, request, *, schema, cwd, timeout_sec=180):
                if request.get("kind") == "review":
                    return super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)
                out = {
                    "application_id": request["application_id"],
                    "context_summary": request["context_summary"],
                    "summary": "one task",
                    "tasks": [{"task_key": "s", "title": "Summarize", "instruction": "write summary.txt",
                               "depends_on": [], "artifacts": ["summary.txt"]}],
                }
                return json.dumps(out), out

        lead = _OneTaskLead()
        steps = [
            {"write": {"summary.txt": "sum\n", "inputs/manifest/notes.txt": "tidied\n"}},  # overreach
            {"write": {"summary.txt": "sum v2\n"}},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            (src / "notes.txt").write_bytes(b"raw notes\n")
            app = self._app(root, steps, LeadAdapterPlanner(lead, cwd=td))
            body = _body(1)
            body["acceptance"] = {"artifacts": ["summary.txt"]}
            body["input_manifest"] = {
                "root": str(src),
                "entries": [{"relative": "notes.txt", "sha256": __import__("hashlib").sha256(b"raw notes\n").hexdigest(),
                             "size": 10}],
                "max_files": 4,
                "max_total_bytes": 1024,
            }
            status = _drive(app, app.submit(body)["goal_id"])
            task = status["tasks"][0]
            notes = (Path(task["workspace"]) / "inputs" / "manifest" / "notes.txt").read_bytes()
        self.assertEqual(status["state"], "completed", status)
        self.assertEqual(task["rework_count"], 1)
        self.assertEqual(task["rework_feedback"][0]["restored"], ["inputs/manifest/notes.txt"])
        self.assertEqual(notes, b"raw notes\n")
        self.assertEqual([r["status"] for r in task["result"]["review_history"]], ["failed", "passed"])

    def test_lead_protocol_slip_is_asked_again(self):
        class _SlipOnceLead(_ScopeLead):
            slipped = False

            def decide(self, request, *, schema, cwd, timeout_sec=180):
                raw, out = super().decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)
                if request.get("kind") == "review" and not self.slipped:
                    self.slipped = True
                    out = {k: v for k, v in out.items() if k != "application_id"}  # live Grok did this
                    return json.dumps(out), out
                return raw, out

        lead = _SlipOnceLead()
        with tempfile.TemporaryDirectory() as td:
            app = self._app(Path(td), [{"write": {"a.txt": A_BODY}}, {"write": {"b.txt": "beta\n"}}],
                            LeadAdapterPlanner(lead, cwd=td))
            status = _drive(app, app.submit(_body(1))["goal_id"])
        self.assertEqual(status["state"], "completed", status)
        self.assertTrue(lead.slipped)
        self.assertEqual(len(lead.reviews), 3)  # A: slip + retry, B: once

    def test_without_lead_scope_is_reported_not_judged(self):
        steps = [{"write": {"a.txt": "x\n", "notes.txt": "extra\n"}}]
        with tempfile.TemporaryDirectory() as td:
            app = self._app(Path(td), steps, DeterministicPlanner())
            body = _body(1)
            body["acceptance"] = {"artifacts": ["a.txt"]}
            status = _drive(app, app.submit(body)["goal_id"])
        self.assertEqual(status["state"], "completed", status)
        task = status["tasks"][0]
        self.assertEqual(
            [(r["path"], r["status"]) for r in task["result"]["scope_report"]["out_of_scope_changes"]],
            [("notes.txt", "added")],
        )
        self.assertTrue(any("notes.txt" in w for w in task["result"]["warnings"]))


class ChangeSetTests(unittest.TestCase):
    def test_change_set_scope_and_ignores(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "in.txt").write_text("input\n", encoding="utf-8")
            (root / "keep.txt").write_text("same\n", encoding="utf-8")
            before = agy_review.scan_workspace(root)
            (root / "in.txt").write_text("changed\n", encoding="utf-8")
            (root / "out.txt").write_text("new\n", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00\x01")
            changes = agy_review.change_set(before, agy_review.scan_workspace(root), ["out.txt"])
        rows = {(r["path"], r["status"], r["in_scope"], r["preexisting"]) for r in changes["changed_files"]}
        self.assertEqual(rows, {("in.txt", "modified", False, True), ("out.txt", "added", True, False)})
        self.assertEqual([r["path"] for r in changes["out_of_scope_changes"]], ["in.txt"])
        self.assertIn("-input", changes["diff"])
        self.assertIn("+changed", changes["diff"])

    def test_baseline_is_written_once(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            ws.mkdir()
            (ws / "f.txt").write_bytes(b"v1\n")
            self.assertTrue(agy_review.record_baseline(Path(td) / "root", "g", "t", ws))
            (ws / "f.txt").write_bytes(b"v2\n")
            self.assertFalse(agy_review.record_baseline(Path(td) / "root", "g", "t", ws))
            self.assertEqual(agy_review.load_baseline(Path(td) / "root", "g", "t")["f.txt"]["text"], "v1\n")

    def test_feedback_reaches_instruction(self):
        text = agy_review.instruction_with_feedback(
            "write b.txt",
            [agy_review.rework_feedback_entry(
                round_no=1, reason="undo a.txt", out_of_scope=[{"path": "a.txt", "status": "modified"}],
                restored=["a.txt"])],
        )
        self.assertTrue(text.startswith("write b.txt"))
        self.assertIn("undo a.txt", text)
        self.assertIn("a.txt (modified)", text)
        self.assertEqual(agy_review.instruction_with_feedback("plain", None), "plain")


class DeliveryReportTests(unittest.TestCase):
    def test_report_after_rework_is_in_goal_language(self):
        lead = _ScopeLead()
        steps = [
            {"write": {"a.txt": A_BODY}},
            {"write": {"b.txt": "beta\n", "a.txt": "rewritten by B\n"}},
            {"write": {"b.txt": "beta v2\n"}},
        ]
        with tempfile.TemporaryDirectory() as td:
            app = PostRunReviewFlowTests._app(self, Path(td), steps, LeadAdapterPlanner(lead, cwd=td))
            goal_id = app.submit(_body(1))["goal_id"]
            _drive(app, goal_id)
            report = app.report(goal_id)
        md = report["delivery_report_md"]
        for heading in ("## 目标完成度", "## 是否越界", "## 不确定的地方和剩余风险",
                        "## 需要上层做的取舍", "## 怎么亲手验证", "## 成本数据", "## 是否建议晋升记忆"):
            self.assertIn(heading, md)
        self.assertIn("**已完成**", md)
        self.assertIn("a.txt (modified)，collab 已恢复原样并让工人返工", md)
        self.assertIn("返工次数：1", md)
        self.assertIn("共 3 次工人运行", md)
        self.assertNotIn("backend runs with skip-permissions", md)


if __name__ == "__main__":
    unittest.main()
