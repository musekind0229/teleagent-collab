#!/usr/bin/env python3
"""Assertions for hard_rules (run: python3 -m test_hard_rules  or  python3 test_hard_rules.py)."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python3 -m test_hard_rules` from src/ and direct script run.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hard_rules import hard_rule_decision, is_secret_path


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_is_secret_path() -> None:
    positives = [
        ".env",
        ".env.local",
        "/home/user/workspace/.env.local",
        "~/.env",
        "auth.json",
        "/tmp/foo/auth.json",
        "token",
        "token.json",
        "GITHUB_TOKEN.txt",
        "my_token_store",
        "~/.ssh/id_ed25519",
        "/home/u/.ssh/config",
        "~/.ssh",
        "~/.netrc",
        "/home/u/.netrc",
        "~/.config/gh/hosts.yml",
        "hosts.yml",
        "credentials.json",
        "credentials",
        "**/.env*",
        "**/token*",
        "/home/u/.config/chromium/Default/Cookies",
        "cookies.sqlite",
        "browser/profile/Default",
    ]
    for p in positives:
        assert_true(is_secret_path(p), f"expected secret: {p!r}")

    negatives = [
        "/workspace/teleagent-collab/src/glue.py",
        "README.md",
        "src/test_hard_rules.py",
        "/tmp/hello.txt",
        "package.json",
        "hosts.txt",  # not hosts.yml
    ]
    for p in negatives:
        assert_true(not is_secret_path(p), f"expected non-secret: {p!r}")

    # patterns kwarg
    assert_true(is_secret_path(patterns=["**/.env*"]), "patterns .env")
    assert_true(is_secret_path(path="/safe/a.py", patterns=["**/token*"]), "mixed patterns")
    assert_true(not is_secret_path(path="/safe/a.py", patterns=["**/*.py"]), "safe patterns")


def test_hard_rule_decision() -> None:
    # Obvious .env read → reject, no lead
    d = hard_rule_decision(
        {
            "id": "p1",
            "tool": "read_file",
            "path": "/home/user/workspace/.env",
            "permission": "read",
        }
    )
    assert_true(d is not None and d["reply"] == "reject", f".env reject got {d}")
    assert_true("hard_rule" in d["reason"], d["reason"])

    # gh hosts
    d = hard_rule_decision({"path": "~/.config/gh/hosts.yml", "tool": "read"})
    assert_true(d is not None and d["reply"] == "reject", f"gh hosts got {d}")

    # ssh
    d = hard_rule_decision({"patterns": ["~/.ssh/**"], "tool": "bash"})
    assert_true(d is not None and d["reply"] == "reject", f"ssh got {d}")

    # always + secret → reject
    d = hard_rule_decision(
        {
            "path": "/home/u/.env.local",
            "requested_reply": "always",
            "message": "always allow reading credentials",
        }
    )
    assert_true(d is not None and d["reply"] == "reject", f"always+secret got {d}")
    assert_true("always" in d["reason"].lower() or "secret" in d["reason"].lower(), d["reason"])

    # Ordinary workspace write → None (lead / once path)
    d = hard_rule_decision(
        {
            "id": "p2",
            "tool": "edit",
            "path": "/workspace/teleagent/probe-sandbox/collab/hello-from-worker.txt",
            "permission": "edit",
        }
    )
    assert_true(d is None, f"ordinary edit should pass-through, got {d}")

    # nested metadata path
    d = hard_rule_decision({"metadata": {"filepath": "/x/.netrc"}, "tool": "read"})
    assert_true(d is not None and d["reply"] == "reject", f"nested netrc got {d}")


def main() -> int:
    test_is_secret_path()
    test_hard_rule_decision()
    print("test_hard_rules: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
