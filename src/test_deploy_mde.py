"""Deployment findings on mde (Linux, root-owned checkout, service as TeleAgent user)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from framework.app_service import read_git_head


@unittest.skipUnless(shutil.which("git"), "git required")
class GitHeadWithForeignOwner(unittest.TestCase):
    """RED on 61e410c: a checkout owned by another user made git refuse
    ("dubious ownership"), HEAD read as "" and every dispatch failed with
    running_tip_mismatch."""

    def test_head_readable_when_checkout_owner_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"],
                cwd=repo, check=True,
            )
            want = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
            # Git's own test hook: behave as if the checkout belonged to someone else.
            with mock.patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
                self.assertEqual(read_git_head(repo), want)


if __name__ == "__main__":
    unittest.main()
