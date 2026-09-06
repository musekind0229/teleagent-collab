#!/usr/bin/env python3
"""Assertions for hard_rules (run: python3 -m test_hard_rules  or  python3 test_hard_rules.py)."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python3 -m test_hard_rules` from src/ and direct script run.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hard_rules import (
    hard_rule_decision,
    is_eternal_reject_path,
    is_secret_path,
    path_allowlisted,
)


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
    # Obvious .env read → reject, no lead (no whitelist)
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


def test_no_whitelist_rejects() -> None:
    """无白名单：明显凭据路径仍 reject。"""
    perm = {
        "id": "nw1",
        "tool": "read_file",
        "path": "/workspace/teleagent-collab/.env.local",
        "permission": "read",
    }
    d = hard_rule_decision(perm, charter=None)
    assert_true(d is not None and d["reply"] == "reject", f"no charter reject got {d}")
    d = hard_rule_decision(perm, charter={"goal": "x", "must_not": ["secrets"]})
    assert_true(d is not None and d["reply"] == "reject", f"empty allowlist reject got {d}")
    assert_true(not perm.get("_hard_rule_allowlisted"), "flag must not be set on reject")


def test_whitelist_does_not_reject() -> None:
    """有 allow_secret_globs / allow_paths → 硬规则不 reject，打 allowlisted 标记。"""
    # via allow_secret_globs
    perm = {
        "id": "wl1",
        "tool": "read_file",
        "path": "/workspace/teleagent-collab/.env",
        "patterns": ["**/.env*"],
        "permission": "read",
    }
    charter = {
        "goal": "load app config from repo .env",
        "allow_secret_globs": ["**/.env*"],
    }
    d = hard_rule_decision(perm, charter=charter)
    assert_true(d is None, f"allow_secret_globs should not reject, got {d}")
    assert_true(perm.get("_hard_rule_allowlisted") is True, "allowlisted flag missing")

    # via allow_paths (+ optional allow_keys)
    perm2 = {
        "id": "wl2",
        "tool": "read_file",
        "path": "/workspace/teleagent-collab/.env.local",
        "permission": "read",
    }
    charter2 = {
        "goal": "read DATABASE_URL from repo env",
        "allow_paths": ["/workspace/teleagent-collab/.env.local"],
        "allow_keys": ["DATABASE_URL"],
    }
    d2 = hard_rule_decision(perm2, charter=charter2)
    assert_true(d2 is None, f"allow_paths should not reject, got {d2}")
    assert_true(perm2.get("_hard_rule_allowlisted") is True, "allowlisted flag missing on paths")

    assert_true(
        path_allowlisted(
            "/workspace/teleagent-collab/.env",
            charter={"allow_secret_globs": ["**/.env*"]},
        ),
        "path_allowlisted glob",
    )


def test_ssh_eternal_reject() -> None:
    """~/.ssh 永拒，即使白名单 / always。"""
    assert_true(is_eternal_reject_path("~/.ssh/id_ed25519"), "ssh key eternal")
    assert_true(is_eternal_reject_path(patterns=["~/.ssh/**"]), "ssh glob eternal")

    charter = {
        "allow_secret_globs": ["**/*", "~/.ssh/**"],
        "allow_paths": ["~/.ssh/id_ed25519", "/home/u/.ssh"],
    }
    perm = {"path": "~/.ssh/id_ed25519", "tool": "read"}
    d = hard_rule_decision(perm, charter=charter)
    assert_true(d is not None and d["reply"] == "reject", f"ssh must reject got {d}")
    assert_true("eternal" in d["reason"].lower() or "ssh" in d["reason"].lower() or "hard_rule" in d["reason"], d["reason"])
    assert_true(not perm.get("_hard_rule_allowlisted"), "ssh must not be allowlisted")

    # browser cookie / gh hosts / .netrc also eternal
    for path in (
        "~/.config/gh/hosts.yml",
        "/home/u/.netrc",
        "/home/u/.config/chromium/Default/Cookies",
        "browser/profile/Default",
    ):
        d = hard_rule_decision({"path": path}, charter={"allow_secret_globs": ["**/*"]})
        assert_true(d is not None and d["reply"] == "reject", f"eternal {path} got {d}")


def test_always_plus_secret_rejects_even_with_whitelist() -> None:
    """always + 秘密类：即使白名单仍拒。"""
    perm = {
        "path": "/workspace/teleagent-collab/.env",
        "requested_reply": "always",
        "message": "always allow reading .env",
    }
    charter = {"allow_secret_globs": ["**/.env*"]}
    d = hard_rule_decision(perm, charter=charter)
    assert_true(d is not None and d["reply"] == "reject", f"always+secret got {d}")
    assert_true("always" in d["reason"].lower(), d["reason"])
    assert_true(not perm.get("_hard_rule_allowlisted"), "always must not allowlist")


def main() -> int:
    test_is_secret_path()
    test_hard_rule_decision()
    test_no_whitelist_rejects()
    test_whitelist_does_not_reject()
    test_ssh_eternal_reject()
    test_always_plus_secret_rejects_even_with_whitelist()
    print("test_hard_rules: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
