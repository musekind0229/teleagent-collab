"""codex_cli lead: model + reasoning effort pinning only (no live codex)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from lead_adapter.codex_cli import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    CodexCliLeadAdapter,
    build_command,
)


def _argv(**kw):
    return build_command("/x/codex", cwd="/w", schema_path="/t/s.json", last_message_path="/t/l.json", **kw)


class CodexModelEffortTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_CODEX_MODEL, "gpt-6.1-sol")
        self.assertEqual(DEFAULT_CODEX_EFFORT, "medium")

    def test_flags_before_stdin_dash_and_fixed_positions_unchanged(self):
        a = _argv(model="gpt-6.1-sol", effort="medium")
        self.assertEqual(a[-1], "-")
        self.assertEqual(a[-5:-1], ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=medium"])
        self.assertEqual(a[2:6], ["--sandbox", "read-only", "-c", "approval_policy=never"])
        self.assertEqual(a[14], "-o")

    def test_no_model_no_effort_keeps_old_argv(self):
        self.assertNotIn("-m", _argv())
        self.assertFalse(any(str(x).startswith("model_reasoning_effort") for x in _argv()))

    def test_resolution_kwarg_env_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLAB_CODEX_LEAD_MODEL", None)
            os.environ.pop("COLLAB_CODEX_LEAD_EFFORT", None)
            ad = CodexCliLeadAdapter()
            self.assertEqual((ad.model_name(), ad.effort_name()), ("gpt-6.1-sol", "medium"))
            os.environ["COLLAB_CODEX_LEAD_MODEL"] = "gpt-x"
            os.environ["COLLAB_CODEX_LEAD_EFFORT"] = "high"
            self.assertEqual((ad.model_name(), ad.effort_name()), ("gpt-x", "high"))
            ad2 = CodexCliLeadAdapter(model="gpt-y", effort="low")
            self.assertEqual((ad2.model_name(), ad2.effort_name()), ("gpt-y", "low"))

    def test_unsafe_model_or_effort_fails_closed_without_spawn(self):
        for kw in ({"model": "gpt&calc"}, {"model": "a b"}, {"effort": "medium;x"}, {"effort": "x=y"}):
            with self.subTest(**kw):
                ad = CodexCliLeadAdapter(bin_path=__file__, **kw)
                with mock.patch("lead_adapter.codex_cli.subprocess.Popen", side_effect=AssertionError("spawned")):
                    raw, parsed = ad.decide({"application_id": "a1", "kind": "permission"}, schema={"type": "object"}, cwd="/tmp")
                self.assertEqual(parsed.get("_lead_status"), "call_failed")
                self.assertIn("refused", parsed["error"])

    def test_doctor_hint_shows_model_and_effort(self):
        h = CodexCliLeadAdapter(model="gpt-6.1-sol", effort="medium").doctor_hint()
        self.assertEqual((h["model"], h["reasoning_effort"]), ("gpt-6.1-sol", "medium"))


if __name__ == "__main__":
    unittest.main()
