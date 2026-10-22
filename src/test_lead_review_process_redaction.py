"""Tests for process row redaction by sensitive key name reusing agy_review.redact_tree."""
from __future__ import annotations

import json
import unittest
from typing import Any

from execution_backend import agy_review
try:
    from execution_backend.agy_review import redact_tree
except ImportError:  # pre-fix tree: keep the other tests runnable
    redact_tree = None
from test_lead_review_event_cap import _MemoryLayer, _runner, _stopped_decision


class LeadReviewProcessRedactionTests(unittest.TestCase):
    def test_order_example_row_redacted_via_patch_processes(self) -> None:
        """asserts {"pid": 1, "nested": {"password": "SYNTH_P"}, "access_token": "SYNTH_T"} via _patch_processes redacts SYNTH_P and SYNTH_T, keeps pid, and contains [redacted]."""
        layer = _MemoryLayer(_stopped_decision())
        runner = _runner(layer)
        row = {"pid": 1, "nested": {"password": "SYNTH_P"}, "access_token": "SYNTH_T"}
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            [row],
        )
        events = layer.decision["details"]["lead_review"]["events"]
        blob = json.dumps(events)
        self.assertNotIn("SYNTH_P", blob)
        self.assertNotIn("SYNTH_T", blob)
        self.assertIn("[redacted]", blob)
        proc = events[-1]["lead_processes"][0]
        self.assertEqual(proc.get("pid"), 1)
        self.assertEqual(proc.get("nested"), {"password": "[redacted]"})
        self.assertEqual(proc.get("access_token"), "[redacted]")

    def test_sensitive_keys_at_depth_redacted(self) -> None:
        """asserts sensitive keys at depth (dict in list in dict: password, access_token, client_secret, api-key, Authorization, cookie) are all redacted."""
        layer = _MemoryLayer(_stopped_decision())
        runner = _runner(layer)
        sensitive_dict = {
            "password": "SYNTH_PASSWORD_VAL",
            "access_token": "SYNTH_ACCESS_TOKEN_VAL",
            "client_secret": "SYNTH_CLIENT_SECRET_VAL",
            "api-key": "SYNTH_API_KEY_VAL",
            "Authorization": "SYNTH_AUTH_HEADER_VAL",
            "cookie": "SYNTH_COOKIE_VAL",
            "normal_field": "SYNTH_SAFE_VAL",
        }
        row = {
            "pid": 42,
            "container": {
                "items": [
                    sensitive_dict,
                ],
            },
        }
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            [row],
        )
        events = layer.decision["details"]["lead_review"]["events"]
        blob = json.dumps(events)
        for secret in (
            "SYNTH_PASSWORD_VAL",
            "SYNTH_ACCESS_TOKEN_VAL",
            "SYNTH_CLIENT_SECRET_VAL",
            "SYNTH_API_KEY_VAL",
            "SYNTH_AUTH_HEADER_VAL",
            "SYNTH_COOKIE_VAL",
        ):
            self.assertNotIn(secret, blob)
        inner = events[-1]["lead_processes"][0]["container"]["items"][0]
        self.assertEqual(inner["password"], "[redacted]")
        self.assertEqual(inner["access_token"], "[redacted]")
        self.assertEqual(inner["client_secret"], "[redacted]")
        self.assertEqual(inner["api-key"], "[redacted]")
        self.assertEqual(inner["Authorization"], "[redacted]")
        self.assertEqual(inner["cookie"], "[redacted]")
        self.assertEqual(inner["normal_field"], "SYNTH_SAFE_VAL")

    def test_non_sensitive_keys_and_list_shape_kept(self) -> None:
        """asserts non-sensitive keys/values (pid, method, result, exit_code) are unchanged and list shape is kept."""
        layer = _MemoryLayer(_stopped_decision())
        runner = _runner(layer)
        procs = [
            {"pid": 101, "method": "sigterm", "result": "terminated", "exit_code": 0},
            {"pid": 102, "method": "sigkill", "result": "killed", "exit_code": -9},
        ]
        runner._patch_processes(
            {"goal_id": "goal-1", "decision_id": "dec-stop", "job_id": "job-stop"},
            procs,
        )
        events = layer.decision["details"]["lead_review"]["events"]
        lead_procs = events[-1].get("lead_processes")
        self.assertIsInstance(lead_procs, list)
        self.assertEqual(len(lead_procs), 2)
        self.assertEqual(
            lead_procs[0],
            {"pid": 101, "method": "sigterm", "result": "terminated", "exit_code": 0},
        )
        self.assertEqual(
            lead_procs[1],
            {"pid": 102, "method": "sigkill", "result": "killed", "exit_code": -9},
        )

    def test_redact_tree_matches_cap_args(self) -> None:
        """asserts redact_tree gives the same output as agy_review._cap_args for a dict input."""
        self.assertIsNotNone(redact_tree, "redact_tree must be exported by agy_review")
        sample_dict = {
            "api_key": "SYNTH_KEY_123",
            "pid": 500,
            "flag": True,
            "nested": {
                "password": "SYNTH_SECRET_PW",
                "client_secret": "SYNTH_CLIENT_SEC",
                "command": "python3 script.py",
                "list": [
                    {"token": "SYNTH_TOK_VAL", "exit_code": 0},
                    "keep-scalar",
                    123,
                ],
            },
        }
        tree_res = redact_tree(sample_dict, 300)
        cap_res = agy_review._cap_args(sample_dict, 300)
        self.assertEqual(tree_res, cap_res)
        for cap in (0, 10, 50, 300):
            self.assertEqual(redact_tree(sample_dict, cap), agy_review._cap_args(sample_dict, cap))


if __name__ == "__main__":
    unittest.main()
