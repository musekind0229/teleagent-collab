#!/usr/bin/env python3
"""Path-B knife2: report projection + public permission view."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from framework.project_report import attach_framework_projection  # noqa: E402
from teleagent_adapter.permission_view import (  # noqa: E402
    to_native_for_rules,
    to_public_permission,
)


class TestProjection(unittest.TestCase):
    def test_attach_does_not_flip_ok(self):
        report = {"ok": False, "state": "fail", "error": "x", "notes": []}
        charter = {
            "goal": "g",
            "must": ["m"],
            "must_not": ["n"],
            "allow_secret_globs": [],
            "allow_paths": [],
            "allow_keys": [],
            "name": "t",
        }
        attach_framework_projection(report, charter)
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "fail")
        self.assertEqual(report["error"], "x")
        self.assertIn("framework_projection", report)
        self.assertTrue(report["framework_projection"]["readonly"])
        self.assertEqual(report["framework_projection"]["goal"]["boundaries"]["allow_secret_globs"], [])

    def test_missing_allow_not_invented(self):
        report = {"ok": True, "state": "dry_run", "error": "", "notes": []}
        attach_framework_projection(
            report, {"goal": "g", "must": [], "must_not": [], "name": "n"}
        )
        self.assertTrue(report["ok"])
        b = report["framework_projection"]["goal"]["boundaries"]
        self.assertNotIn("allow_secret_globs", b)


class TestPermissionView(unittest.TestCase):
    def test_roundtrip_public(self):
        raw = {
            "id": "per_1",
            "sessionID": "ses_1",
            "permission": "external_directory",
            "patterns": ["/etc/*"],
            "metadata": {"filepath": "/etc/hostname"},
            "tool": {"name": "Read"},
        }
        pub = to_public_permission(raw)
        self.assertEqual(pub["request_id"], "per_1")
        self.assertEqual(pub["session_id"], "ses_1")
        self.assertEqual(pub["path"], "/etc/hostname")
        self.assertNotIn("sessionID", pub)
        native = to_native_for_rules(pub)
        self.assertEqual(native["id"], "per_1")
        self.assertEqual(native["sessionID"], "ses_1")


if __name__ == "__main__":
    unittest.main()
