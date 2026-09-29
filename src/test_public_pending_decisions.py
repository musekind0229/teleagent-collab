"""public_pending_decisions copies reason and leaves the other public fields alone."""
from __future__ import annotations

import unittest

from framework.app_service import public_pending_decisions


class PublicPendingDecisionsTests(unittest.TestCase):
    def test_reason_copied_other_fields_unchanged(self) -> None:
        row = {
            "decision_id": "dec-1",
            "request_id": "req-1",
            "kind": "escalate_over_budget",
            "title": "escalate_over_budget",
            "task_id": "T",
            "run_id": "run-1",
            "status": "pending",
            "reason": "R",
            "details": {
                "backend_kind": "antigravity",
                "backend_request_id": "br-1",
                "note": "keep",
            },
            "lead_error": {"code": "timeout", "message": "boom"},
            "secret_extra": "not-public",
        }
        out = public_pending_decisions([row, "skip", None])
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0],
            {
                "decision_id": "dec-1",
                "request_id": "req-1",
                "kind": "escalate_over_budget",
                "title": "escalate_over_budget",
                "reason": "R",
                "task_id": "T",
                "run_id": "run-1",
                "status": "pending",
                "backend_kind": "antigravity",
                "backend_request_id": "br-1",
                "details": {
                    "backend_kind": "antigravity",
                    "backend_request_id": "br-1",
                    "note": "keep",
                },
                "lead_error": {"code": "timeout", "message": "boom"},
            },
        )

    def test_reason_defaults_to_empty_string(self) -> None:
        missing = public_pending_decisions(
            [{"decision_id": "d", "kind": "question", "title": "ask"}]
        )[0]
        self.assertEqual(missing["reason"], "")
        self.assertEqual(missing["decision_id"], "d")
        self.assertEqual(missing["request_id"], "")
        self.assertEqual(missing["kind"], "question")
        self.assertEqual(missing["title"], "ask")
        self.assertEqual(missing["task_id"], "")
        self.assertEqual(missing["run_id"], "")
        self.assertEqual(missing["status"], "")
        self.assertEqual(missing["backend_kind"], "")
        self.assertEqual(missing["backend_request_id"], "")
        self.assertEqual(missing["details"], {})
        self.assertNotIn("lead_error", missing)

        self.assertEqual(
            public_pending_decisions([{"decision_id": "d", "reason": ""}])[0]["reason"],
            "",
        )
        self.assertEqual(
            public_pending_decisions([{"decision_id": "d", "reason": None}])[0]["reason"],
            "",
        )
        nested = public_pending_decisions(
            [
                {
                    "decision_id": "d",
                    "details": {
                        "reason": "inside",
                        "backend_kind": "inprocess",
                        "lead_error": {"message": "from-details"},
                    },
                }
            ]
        )[0]
        self.assertEqual(nested["reason"], "")
        self.assertEqual(nested["details"]["reason"], "inside")
        self.assertEqual(nested["backend_kind"], "inprocess")
        self.assertEqual(nested["lead_error"], {"message": "from-details"})

    def test_empty_input(self) -> None:
        self.assertEqual(public_pending_decisions(None), [])
        self.assertEqual(public_pending_decisions([]), [])


if __name__ == "__main__":
    unittest.main()
