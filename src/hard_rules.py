"""Hard-rule prefilter for permission requests.

Obvious credential paths and "always + secret_adjacent" are rejected
before any lead ping. Grey cases go to the lead with a decision packet.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

# Basename / path fragments that are obviously secret stores.
_SECRET_BASENAMES = frozenset(
    {
        "auth.json",
        ".netrc",
        "netrc",
        "hosts.yml",
        "hosts.yaml",
        "credentials",
        "credentials.json",
        "credential",
        "credential.json",
        "credentials.yml",
        "credentials.yaml",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "known_hosts",
        "cookies",
        "cookies.sqlite",
        "cookies.db",
        "cookie",
    }
)

_SECRET_BASENAME_GLOBS = (
    re.compile(r"^\.env($|\.)", re.I),  # .env, .env.local, .env.*
    re.compile(r"^token", re.I),  # token, token.json, tokens*
    re.compile(r"token", re.I),  # *token*
    re.compile(r"credential", re.I),
    re.compile(r"^cookie", re.I),
)

# Path substrings / directory markers (normalized with forward slashes).
_SECRET_PATH_MARKERS = (
    "/.ssh/",
    "/.ssh",
    "~/.ssh",
    "/.netrc",
    "/.config/gh/hosts",
    "/gh/hosts",
    "gh/hosts.yml",
    "gh/hosts.yaml",
    "/cookies",
    "/cookie",
    "/chrome/profile",
    "/chromium/profile",
    "/browser/profile",
    "/.mozilla/",
    "/login data",
    "/logindata",
    "/keychain",
    "/keyring",
)

_ALWAYS_REPLY = frozenset({"always", "allow_always", "approve_always"})


def _norm(p: str) -> str:
    s = (p or "").strip().replace("\\", "/")
    if s.startswith("~/"):
        s = s  # keep tilde form for marker match
    try:
        # expanduser for marker checks but keep original basename logic
        expanded = os.path.expanduser(s)
        return expanded.replace("\\", "/")
    except Exception:
        return s


def _basename(p: str) -> str:
    s = (p or "").strip().replace("\\", "/")
    if not s:
        return ""
    return Path(s).name


def is_secret_path(path: str | Iterable[str] | None = None, patterns: Iterable[str] | None = None) -> bool:
    """Return True if path or any pattern looks like an obvious credential store.

    Accepts a single path string, an iterable of paths, and/or glob-like patterns
    from a permission request.
    """
    candidates: list[str] = []
    if path is None:
        pass
    elif isinstance(path, str):
        if path:
            candidates.append(path)
    else:
        for item in path:
            if item:
                candidates.append(str(item))
    if patterns is not None:
        for item in patterns:
            if item:
                candidates.append(str(item))

    for raw in candidates:
        if _one_is_secret(raw):
            return True
    return False


def _one_is_secret(raw: str) -> bool:
    s = raw.strip()
    if not s:
        return False
    norm = _norm(s)
    low = norm.lower()
    base = _basename(s)
    base_low = base.lower()

    # Exact basename hits
    if base_low in _SECRET_BASENAMES:
        return True

    # Basename glob-ish
    for rx in _SECRET_BASENAME_GLOBS:
        if rx.search(base):
            return True

    # Path markers (directories / known credential files)
    low_orig = s.lower().replace("\\", "/")
    for marker in _SECRET_PATH_MARKERS:
        m = marker.lower()
        variants = {m, m.lstrip("/"), m.rstrip("/"), m.strip("/")}
        for v in variants:
            if not v:
                continue
            if v in low or v in low_orig:
                return True
            if low.endswith(v) or low_orig.endswith(v):
                return True

    # Pattern strings like **/.env* or **/token*
    if "*" in s or "?" in s:
        pl = s.lower().replace("\\", "/")
        if ".env" in pl or "token" in pl or "credential" in pl:
            return True
        if "/.ssh" in pl or "hosts.yml" in pl or "hosts.yaml" in pl or ".netrc" in pl:
            return True
        if "cookie" in pl or "auth.json" in pl:
            return True

    return False


def _collect_paths(permission_dict: dict) -> tuple[list[str], list[str]]:
    """Extract path-like and pattern-like fields from a permission payload."""
    paths: list[str] = []
    patterns: list[str] = []

    def add_path(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str) and v:
            paths.append(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add_path(x)
        elif isinstance(v, dict):
            for k in ("path", "paths", "file", "filepath", "target", "uri"):
                if k in v:
                    add_path(v[k])

    def add_pat(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str) and v:
            patterns.append(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add_pat(x)

    for k in ("path", "paths", "file", "filepath", "target", "uri", "cwd"):
        if k in permission_dict:
            add_path(permission_dict[k])
    for k in ("patterns", "pattern", "glob", "globs"):
        if k in permission_dict:
            add_pat(permission_dict[k])

    # nested metadata / permission objects
    for nest_key in ("metadata", "permission", "permissions", "input", "args"):
        nest = permission_dict.get(nest_key)
        if isinstance(nest, dict):
            p2, g2 = _collect_paths(nest)
            paths.extend(p2)
            patterns.extend(g2)
        elif isinstance(nest, list):
            for item in nest:
                if isinstance(item, dict):
                    p2, g2 = _collect_paths(item)
                    paths.extend(p2)
                    patterns.extend(g2)
                elif isinstance(item, str):
                    paths.append(item)

    # message / title / description may embed paths — light scan for known markers
    for text_key in ("message", "title", "description", "command"):
        t = permission_dict.get(text_key)
        if isinstance(t, str) and t:
            # pull path-looking tokens
            for m in re.finditer(r"(~/[^\s\"']+|/[^\s\"']*\.(?:env|json|yml|yaml|netrc)[^\s\"']*|/[^\s\"']*/\.(?:ssh|netrc)[^\s\"']*)", t):
                paths.append(m.group(1))

    return paths, patterns


def _wants_always(permission_dict: dict) -> bool:
    """Detect requests that ask for durable/always approval on a secret path."""
    for k in ("reply", "requested_reply", "decision", "approval", "want", "mode"):
        v = permission_dict.get(k)
        if isinstance(v, str) and v.strip().lower() in _ALWAYS_REPLY:
            return True
    meta = permission_dict.get("metadata")
    if isinstance(meta, dict):
        for k in ("reply", "requested_reply", "decision", "always"):
            v = meta.get(k)
            if v is True:
                return True
            if isinstance(v, str) and v.strip().lower() in _ALWAYS_REPLY:
                return True
    # permission type hints
    perm = permission_dict.get("permission") or permission_dict.get("type") or ""
    if isinstance(perm, str) and "always" in perm.lower():
        return True
    msg = " ".join(
        str(permission_dict.get(k) or "")
        for k in ("message", "title", "description")
    ).lower()
    if "always" in msg and any(
        w in msg for w in ("token", "credential", ".env", "secret", "cookie", "password", "auth")
    ):
        return True
    return False


def hard_rule_decision(permission_dict: dict) -> Optional[dict]:
    """If hard rules fire, return {"reply":"reject","reason":...}; else None (ask lead)."""
    if not isinstance(permission_dict, dict):
        return None

    paths, patterns = _collect_paths(permission_dict)
    secret = is_secret_path(paths, patterns)

    if secret and _wants_always(permission_dict):
        return {
            "reply": "reject",
            "reason": "hard_rule: always + secret_adjacent (credential path / always-approve denied)",
        }
    if secret:
        # Show a short pattern/basename hint only — never file contents.
        hint_bits = []
        for p in (paths + patterns)[:3]:
            b = _basename(p) or p
            hint_bits.append(b)
        hint = ", ".join(hint_bits) if hint_bits else "credential-like path"
        return {
            "reply": "reject",
            "reason": f"hard_rule: obvious credential path ({hint})",
        }
    if _wants_always(permission_dict) and secret:
        # unreachable due to order above; kept for clarity
        return {
            "reply": "reject",
            "reason": "hard_rule: always + secret_adjacent",
        }
    return None


__all__ = ["is_secret_path", "hard_rule_decision"]
