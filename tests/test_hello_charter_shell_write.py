"""Hello charter must shell-redirect the artifact and forbid TeleAgent write.

Exact-byte review stays in place. TeleAgent write/AIGC post-process injects
ZWSP/ZWJ and the AI mark even when the tool arguments are clean, so the
windows hello template requires a shell redirect and bans the write tool.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from win_collab.core import validate_charter

REPO = Path(__file__).resolve().parents[1]
HELLO = REPO / "windows" / "examples" / "hello.json"

SHELL_STYLES = ("set-content", "echo", "printf", "redirect")
BANNED_PHRASES = ("file-writing tool", "do not run a shell")
REASON_MARKERS = ("aigc", "zwsp", "watermark", "ai生成")


def _blob(charter: dict) -> str:
    parts = [charter["goal"], *charter["must"], charter["acceptance"]]
    return "\n".join(parts)


class HelloCharterShellWriteTests(unittest.TestCase):
    def setUp(self):
        self.charter = json.loads(HELLO.read_text(encoding="utf-8"))

    def test_write_tool_is_forbidden_case_sensitive(self):
        forbidden = self.charter["forbidden_tools"]
        self.assertIsInstance(forbidden, list)
        self.assertIn("write", forbidden)
        write_names = [name for name in forbidden if name.lower() == "write"]
        self.assertEqual(write_names, ["write"])
        lowered = {name.lower() for name in forbidden}
        self.assertNotIn("powershell", lowered)
        self.assertNotIn("shell", lowered)
        self.assertNotIn("bash", lowered)

    def test_charter_requires_shell_redirect_not_file_tool(self):
        blob = _blob(self.charter).lower()
        self.assertTrue(
            any(style in blob for style in SHELL_STYLES),
            "goal/must/acceptance must require a shell redirect, Set-Content, echo, or printf",
        )
        self.assertTrue("shell" in blob or "powershell" in blob)
        for phrase in BANNED_PHRASES:
            self.assertNotIn(phrase, blob)

    def test_charter_documents_aigc_watermark_reason(self):
        text = json.dumps(self.charter, ensure_ascii=False).lower()
        self.assertTrue(
            any(marker in text for marker in REASON_MARKERS),
            "charter must explain the AIGC / ZWSP / watermark / 「AI生成」 reason",
        )
        self.assertIn("zwsp", text)
        self.assertIn("ai生成", text)

    def test_exact_line_acceptance_unchanged(self):
        acceptance = self.charter["acceptance"]
        lowered = acceptance.lower()
        self.assertIn("independently reads", lowered)
        self.assertIn("exact line", lowered)
        self.assertIn("merely existing is insufficient", lowered)
        self.assertNotIn("contains-tip", lowered)
        self.assertNotIn("strip", lowered)
        self.assertEqual(self.charter["artifacts"], ["hello.txt"])
        self.assertIn("Hello from TeleAgent on Windows!", self.charter["goal"])
        self.assertIn("Hello from TeleAgent on Windows!", acceptance)
        validate_charter(self.charter)
