#!/usr/bin/env python3
"""Client summary tests for lead review. Red until hermes summarize_status learns the fields.

Loads bin/hermes-collab-request.py the same way src/test_hermes_collab_request.py does.
Does not start a service and does not call a model.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "hermes-collab-request.py"

_LEAD_REVIEW = {
    "outcome": "unavailable",
    "code": "timeout",
    "next_step": "check the lead CLI, then open a NEW request",
    "human_action_required": True,
}
_STARTED = "2026-10-08T00:00:00Z"
_BRIEF = {
    "status": "running",
    "round": 2,
    "attempt": 1,
    "max_attempts": 2,
    "started_at_iso": _STARTED,
    "error_code": "timeout",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_collab_request", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HCR = _load_module()


def _payload() -> dict:
    return {
        "ok": True,
        "state": "failed",
        "request_id": "goal_t15",
        "tasks": [
            {
                "task_id": "task_1",
                "title": "implementation",
                "status": "failed",
                "result": {
                    "review": {"status": "passed", "source": "lead_review"},
                    "rework": {
                        "used": 1,
                        "max": 1,
                        "history": [{"verdict": "fail"}, {"verdict": "pass"}],
                    },
                    "lead_review": dict(_LEAD_REVIEW),
                },
            }
        ],
        "pending_decisions": [
            {
                "decision_id": "dec_t15",
                "kind": "artifact_review",
                "title": "TeleAgent review",
                "task_id": "task_1",
                "status": "pending",
                "reason": "review",
                "awaiting": "lead",
                "details": {
                    "lead_review": {
                        "status": "running",
                        "round": 2,
                        "attempt": 1,
                        "max_attempts": 2,
                        "started_at_iso": _STARTED,
                        "last_error": {"code": "timeout"},
                    }
                },
            }
        ],
    }


class ClientLeadReviewSummaryTests(unittest.TestCase):
    def test_t15_summarize_status_rework_and_lead_review(self) -> None:
        """T15: asserts summarize_status task view has rework used/max/last_verdict and lead_review, and the pending brief has lead_review status, round, attempt, max_attempts, started_at_iso, and error_code.
        red on 49fa3a36: summarize_status omits rework and lead_review.
        """
        summary = HCR.summarize_status(_payload())
        view = summary["tasks"][0]
        self.assertIn("rework", view, view)
        self.assertEqual(view["rework"], {"used": 1, "max": 1, "last_verdict": "pass"})
        self.assertIn("lead_review", view, view)
        self.assertEqual(view["lead_review"], _LEAD_REVIEW)
        brief = summary["pending_decisions"][0]
        self.assertIn("lead_review", brief, brief)
        self.assertEqual(brief["lead_review"], _BRIEF)

    def test_t15_decision_brief_lead_review(self) -> None:
        """T15: asserts _decision_brief copies details.lead_review into status, round, attempt, max_attempts, started_at_iso, and error_code.
        red on 49fa3a36: _decision_brief omits lead_review.
        """
        row = _payload()["pending_decisions"][0]
        brief = HCR._decision_brief(row)
        self.assertIn("lead_review", brief, brief)
        self.assertEqual(brief["lead_review"], _BRIEF)


if __name__ == "__main__":
    unittest.main()
