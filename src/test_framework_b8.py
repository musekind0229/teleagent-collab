#!/usr/bin/env python3
"""Path-B knife8: inprocess stage-2 closed loop (artifact_review + rework as new Run)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import load_charter  # noqa: E402
from execution_backend import (  # noqa: E402
    artifact_review,
    make_decision_channel_fail,
    make_fail_once_then_pass,
    run_file_job_via_public_api,
    run_inprocess_charter,
    run_inprocess_closed_loop,
)
from framework.lifecycle import DECISION_CHANNEL_LEAD_CODES, map_error_class  # noqa: E402
from lead_adapter import build_lead_request  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"
CLOSED = REPO / "jobs/examples/hello-inprocess-closed-loop.charter.yaml"


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_knife8", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestErrorClassMap(unittest.TestCase):
    def test_acceptance_vs_channel(self):
        self.assertEqual(map_error_class(kind="acceptance_failed"), "acceptance_failed")
        self.assertEqual(map_error_class(kind="review_fail"), "acceptance_failed")
        self.assertEqual(
            map_error_class(kind="decision_channel", lead_code="application_id_mismatch"),
            "decision_channel_failed",
        )
        for code in DECISION_CHANNEL_LEAD_CODES:
            self.assertEqual(
                map_error_class(kind="lead_protocol", lead_code=code),
                "decision_channel_failed",
                code,
            )


class TestClosedLoopHappy(unittest.TestCase):
    def test_hello_without_review_stays_one_run(self):
        charter = load_charter(HELLO)
        with tempfile.TemporaryDirectory() as td:
            before = set(sys.modules)
            result = run_inprocess_charter(charter=charter, workdir=td, name="hello")
            newly = set(sys.modules) - before
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["used_public_api_only"])
            self.assertEqual(result["attempt"], 1)
            self.assertFalse(result["rework_used_new_run"])
            self.assertTrue(result["wall_deadline_unchanged"])
            self.assertFalse(result["force_lead_review"])
            self.assertIsNone(result.get("error_class"))
            self.assertTrue((Path(td) / "hello-from-worker.txt").is_file())
            banned = [
                n
                for n in newly
                if n == "glue" or n.startswith("glue.") or n.startswith("teleagent_adapter")
            ]
            self.assertEqual(banned, [], f"inprocess closed loop imported TA/glue: {banned}")

    def test_force_review_local_rules_pass(self):
        charter = load_charter(CLOSED)
        self.assertTrue(charter.get("force_lead_review"))
        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_closed_loop(charter=charter, workdir=td, name="hello-cl")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["attempt"], 1)
            self.assertFalse(result["rework_used_new_run"])
            self.assertEqual(result.get("grok_review_decision"), "pass")
            self.assertTrue(result["used_public_api_only"])
            self.assertEqual(len(result["run_ids"]), 1)
            self.assertTrue(result["task_id"].startswith("task_"))


class TestAcceptFailThenRework(unittest.TestCase):
    def test_fail_once_new_run_same_task_wall_unchanged(self):
        charter = load_charter(CLOSED)
        stub = make_fail_once_then_pass()
        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_closed_loop(
                charter=charter,
                workdir=td,
                name="hello-cl",
                decision_fn=stub,
                timeout_sec=30,
                max_reworks=1,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["attempt"], 2, result["notes"])
            self.assertTrue(result["rework_used_new_run"])
            self.assertTrue(result["wall_deadline_unchanged"])
            self.assertEqual(result["wall_deadline"], result["wall_deadline_at_start"])
            self.assertEqual(result["rework_budget"]["reworks_used"], 1)
            self.assertEqual(result["rework_budget"]["wall_deadline"], result["wall_deadline_at_start"])
            ids = result["run_ids"]
            self.assertEqual(len(ids), 2)
            self.assertNotEqual(ids[0], ids[1])
            be_ids = result["backend_run_ids"]
            self.assertEqual(len(set(be_ids)), 2)
            attempts = result["closed_loop"]["attempts"]
            self.assertEqual(attempts[0]["run"]["task_id"], attempts[1]["run"]["task_id"])
            self.assertEqual(attempts[0]["run"]["attempt"], 1)
            self.assertEqual(attempts[1]["run"]["attempt"], 2)
            self.assertEqual(attempts[0]["review"]["error_class"], "acceptance_failed")
            self.assertEqual(attempts[0]["review"]["verdict"], "fail")
            self.assertEqual(attempts[1]["review"]["verdict"], "pass")
            self.assertEqual(stub.calls["n"], 2)
            self.assertTrue((Path(td) / "hello-from-worker.txt").is_file())
            self.assertIsNone(result.get("error_class"))

    def test_channel_fail_does_not_consume_rework(self):
        charter = load_charter(CLOSED)
        stub = make_decision_channel_fail()
        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_closed_loop(
                charter=charter,
                workdir=td,
                name="hello-cl",
                decision_fn=stub,
                max_reworks=2,
            )
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error_class"], "decision_channel_failed")
            self.assertEqual(result["attempt"], 1)
            self.assertFalse(result["rework_used_new_run"])
            self.assertEqual(result["rework_budget"]["reworks_used"], 0)
            self.assertTrue(result["wall_deadline_unchanged"])
            self.assertIn("decision_channel_failed", result["error"])
            self.assertEqual(len(result["run_ids"]), 1)

    def test_channel_timeout_not_business_rework(self):
        charter = load_charter(CLOSED)
        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_closed_loop(
                charter=charter,
                workdir=td,
                decision_fn=make_decision_channel_fail(code="timeout"),
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_class"], "decision_channel_failed")
            self.assertEqual(result["rework_budget"]["reworks_used"], 0)
            self.assertFalse(result["rework_used_new_run"])

    def test_acceptance_exhausted_still_new_run_then_stop(self):
        charter = load_charter(CLOSED)

        def always_fail(request, schema=None):
            return {
                "application_id": request["application_id"],
                "context_summary": request["context_summary"],
                "verdict": "fail",
                "reason": "always fail",
            }

        with tempfile.TemporaryDirectory() as td:
            result = run_inprocess_closed_loop(
                charter=charter,
                workdir=td,
                decision_fn=always_fail,
                max_reworks=1,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_class"], "budget_exhausted")
            self.assertEqual(result["attempt"], 2)
            self.assertTrue(result["rework_used_new_run"])
            self.assertEqual(result["rework_budget"]["reworks_used"], 1)
            self.assertTrue(result["wall_deadline_unchanged"])


class TestPublicArtifactReview(unittest.TestCase):
    def test_gate_uses_binding(self):
        charter = load_charter(CLOSED)
        with tempfile.TemporaryDirectory() as td:
            collect = run_file_job_via_public_api(workdir=td, charter=charter)
            self.assertTrue(collect["ok"], collect)
            out = artifact_review(
                charter=charter,
                workdir=td,
                collect=collect,
                decision_fn=local_pass,
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["kind"], "artifact_review")
            self.assertEqual(out["verdict"], "pass")
            self.assertEqual(out["binding"]["application_id"], out["request_id"])

    def test_unbound_pass_is_channel_fail(self):
        charter = load_charter(CLOSED)
        with tempfile.TemporaryDirectory() as td:
            collect = run_file_job_via_public_api(workdir=td, charter=charter)
            out = artifact_review(
                charter=charter,
                workdir=td,
                collect=collect,
                decision_fn=make_decision_channel_fail(),
            )
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_class"], "decision_channel_failed")
            self.assertEqual(out["lead_error_code"], "application_id_mismatch")


def local_pass(request, schema=None):
    return {
        "application_id": request["application_id"],
        "context_summary": request["context_summary"],
        "verdict": "pass",
        "reason": "test pass",
    }


class TestRunJobCliKnife8(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_run_job()

    def _run(self, argv, env_extra=None):
        env = os.environ.copy()
        env.pop("COLLAB_EXECUTION_BACKEND", None)
        env.pop("COLLAB_INPROCESS_REVIEW_STUB", None)
        if env_extra:
            env.update(env_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = self.mod.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_hello_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["state"], "dry_run")
            self.assertNotIn("backend", summary)

    def test_inprocess_closed_loop_local_rules(self):
        with tempfile.TemporaryDirectory() as td:
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                [
                    "--backend",
                    "inprocess",
                    "--workspace",
                    ws,
                    "--runs-dir",
                    runs,
                    str(CLOSED),
                ]
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"], summary)
            status = json.loads(Path(summary["status"]).read_text(encoding="utf-8"))
            self.assertEqual(status.get("path"), "inprocess.local_v1")
            self.assertTrue(status.get("used_public_api_only"))
            self.assertEqual(status.get("attempt"), 1)
            self.assertTrue((Path(ws) / "hello-from-worker.txt").is_file())

    def test_fail_once_env_stub_rewrites_new_run(self):
        with tempfile.TemporaryDirectory() as td:
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                [
                    "--backend",
                    "inprocess",
                    "--workspace",
                    ws,
                    "--runs-dir",
                    runs,
                    str(CLOSED),
                ],
                env_extra={"COLLAB_INPROCESS_REVIEW_STUB": "fail_once"},
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"], summary)
            status = json.loads(Path(summary["status"]).read_text(encoding="utf-8"))
            self.assertTrue(status.get("rework_used_new_run"), status)
            self.assertEqual(status.get("attempt"), 2)
            self.assertTrue(status.get("wall_deadline_unchanged"))
            report = json.loads(Path(summary["report_json"]).read_text(encoding="utf-8"))
            result = report["result"]
            self.assertEqual(len(result.get("run_ids") or []), 2)
            self.assertNotEqual(result["run_ids"][0], result["run_ids"][1])

    def test_default_entry_not_inprocess(self):
        """Without --backend inprocess, dry-run still does not select inprocess."""
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._run(["--dry-run", "--runs-dir", td, str(HELLO)])
            self.assertEqual(rc, 0, err)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertNotEqual(summary.get("backend"), "inprocess")
            self.assertEqual(summary["state"], "dry_run")


class TestLeadRequestKind(unittest.TestCase):
    def test_review_request_kind(self):
        req = build_lead_request(
            kind="review",
            goal="g",
            authorized_scope=["ws"],
            prohibitions=["secrets"],
            acceptance_criteria={"artifacts": ["a.txt"]},
            current_application={"ok": True},
        )
        self.assertEqual(req["kind"], "review")
        self.assertTrue(req["application_id"])
        self.assertTrue(req["context_summary"])


if __name__ == "__main__":
    unittest.main()
