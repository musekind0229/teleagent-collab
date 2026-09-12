#!/usr/bin/env python3
"""Path-B knife5: public Run observation (status/message/finish behind adapter)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from teleagent_adapter.run_observe import build_run_observation, fetch_run_observation  # noqa: E402


class TestBuildRunObservation(unittest.TestCase):
    def test_idle_successful_finish(self):
        msgs = [
            {"info": {"role": "user", "id": "u1"}},
            {"info": {"role": "assistant", "finish": "stop", "id": "a1", "parentID": "u1"}},
        ]
        obs = build_run_observation(
            session_id="ses_1",
            status_http=200,
            status_body={},  # valid empty → idle
            message_http=200,
            messages=msgs,
            dispatch_user_message_id="u1",
        )
        self.assertEqual(obs["activity"], "idle")
        self.assertTrue(obs["finish_successful"])
        self.assertEqual(obs["finish"], "stop")
        self.assertFalse(obs["busy"])

    def test_bad_status_not_idle(self):
        obs = build_run_observation(
            session_id="ses_1",
            status_http=500,
            status_body={"error": "boom"},
            message_http=200,
            messages=[],
        )
        self.assertEqual(obs["activity"], "unknown")
        self.assertTrue(obs["busy"])
        self.assertFalse(obs["finish_successful"])

    def test_no_this_round_no_success(self):
        obs = build_run_observation(
            session_id="ses_1",
            status_http=200,
            status_body={},
            message_http=200,
            messages=[{"info": {"role": "user", "id": "u1"}}],
            dispatch_user_message_id="u1",
        )
        self.assertFalse(obs["finish_successful"])
        self.assertFalse(obs["this_round_found"])

    def test_fetch_via_call(self):
        calls = []

        def call(method, path, **kw):
            calls.append((method, path))
            if path == "/session/status":
                return 200, {}
            if path.endswith("/message"):
                return 200, [
                    {"info": {"role": "user", "id": "u9"}},
                    {"info": {"role": "assistant", "finish": "complete", "id": "a9", "parentID": "u9"}},
                ]
            return 404, None

        obs = fetch_run_observation(call, "ses_x", dispatch_user_message_id="u9")
        self.assertTrue(obs["finish_successful"])
        self.assertIn(("GET", "/session/status"), calls)
        self.assertIn(("GET", "/session/ses_x/message"), calls)


if __name__ == "__main__":
    unittest.main()
