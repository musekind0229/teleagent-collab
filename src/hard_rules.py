"""Hard-rule prefilter for permission requests.

Default: obvious credential paths are rejected before any lead ping
(unauthorized secret harvest).

Exception: charter.allow_secret_globs and/or allow_paths (optional
allow_keys) explicitly authorize a path → hard rules do NOT reject;
glue logs and may reply once (small-risk legitimate work).

Eternal reject (even with whitelist / always): ~/.ssh, browser
cookie/profile, gh hosts, .netrc, and always+secret_adjacent.

Grey cases (looks like config but intent is blocked_workaround) are
not hard-killed → decision packet asks Grok.
"""
from __future__ import annotations

import fnmatch
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

# Eternal-reject markers: never allowlisted, even via charter.
_ETERNAL_PATH_MARKERS = (
    "/.ssh/",
    "/.ssh",
    "~/.ssh",
    "/.netrc",
    ".netrc",
    "/.config/gh/hosts",
    "/gh/hosts",
    "gh/hosts.yml",
    "gh/hosts.yaml",
    "/cookies",
    "/cookie",
    "cookies.sqlite",
    "cookies.db",
    "/chrome/profile",
    "/chromium/profile",
    "/browser/profile",
    "/.mozilla/",
    "/login data",
    "/logindata",
)

_ETERNAL_BASENAMES = frozenset(
    {
        ".netrc",
        "netrc",
        "hosts.yml",
        "hosts.yaml",
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


def is_eternal_reject_path(
    path: str | Iterable[str] | None = None,
    patterns: Iterable[str] | None = None,
) -> bool:
    """True for paths that must never be allowlisted: ~/.ssh, browser
    cookie/profile, gh hosts, .netrc (and related basenames/markers).
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
        if _one_is_eternal(raw):
            return True
    return False


def _one_is_eternal(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    norm = _norm(s)
    low = norm.lower()
    low_orig = s.lower().replace("\\", "/")
    base = _basename(s).lower()

    if base in _ETERNAL_BASENAMES:
        return True

    for marker in _ETERNAL_PATH_MARKERS:
        m = marker.lower()
        variants = {m, m.lstrip("/"), m.rstrip("/"), m.strip("/")}
        for v in variants:
            if not v:
                continue
            if v in low or v in low_orig:
                return True
            if low.endswith(v) or low_orig.endswith(v):
                return True

    if "*" in s or "?" in s:
        pl = s.lower().replace("\\", "/")
        if "/.ssh" in pl or "hosts.yml" in pl or "hosts.yaml" in pl or ".netrc" in pl:
            return True
        if "cookie" in pl or "browser/profile" in pl or "chrome/profile" in pl:
            return True
        if "chromium/profile" in pl or ".mozilla" in pl:
            return True

    return False


def _charter_allow_entries(charter: dict | None) -> tuple[list[str], list[str], list[str]]:
    """Return (globs, paths, keys) from charter allowlist fields."""
    if not isinstance(charter, dict):
        return [], [], []
    globs: list[str] = []
    paths: list[str] = []
    keys: list[str] = []

    raw_globs = charter.get("allow_secret_globs") or charter.get("allow_secret_glob") or []
    if isinstance(raw_globs, str):
        raw_globs = [raw_globs]
    for g in raw_globs:
        if isinstance(g, str) and g.strip():
            globs.append(g.strip())

    raw_paths = charter.get("allow_paths") or charter.get("allow_path") or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    for item in raw_paths:
        if isinstance(item, str) and item.strip():
            paths.append(item.strip())
        elif isinstance(item, dict):
            p = item.get("path") or item.get("file") or item.get("filepath")
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
            ks = item.get("keys") or item.get("allow_keys") or []
            if isinstance(ks, str):
                ks = [ks]
            for k in ks:
                if isinstance(k, str) and k.strip():
                    keys.append(k.strip())

    raw_keys = charter.get("allow_keys") or []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    for k in raw_keys:
        if isinstance(k, str) and k.strip():
            keys.append(k.strip())

    return globs, paths, keys


def _path_matches_glob(candidate: str, glob_pat: str) -> bool:
    """Match path or basename against a charter glob (supports ** and *)."""
    c = candidate.strip().replace("\\", "/")
    g = glob_pat.strip().replace("\\", "/")
    if not c or not g:
        return False
    # Direct fnmatch on full path and basename
    if fnmatch.fnmatch(c, g):
        return True
    if fnmatch.fnmatch(_basename(c), g):
        return True
    # Also try expanded form
    try:
        expanded = os.path.expanduser(c).replace("\\", "/")
        if expanded != c and fnmatch.fnmatch(expanded, g):
            return True
    except Exception:
        pass
    # Prefix-style: allow_paths may be exact or directory prefix
    return False


def path_allowlisted(
    path: str | Iterable[str] | None = None,
    patterns: Iterable[str] | None = None,
    charter: dict | None = None,
) -> bool:
    """True if any candidate path/pattern is covered by charter allow_secret_globs
    and/or allow_paths. Optional allow_keys are recorded on the charter side but
    do not themselves authorize a path (path/glob match is required).

    Eternal-reject paths are never considered allowlisted.
    """
    globs, allow_paths, _keys = _charter_allow_entries(charter)
    if not globs and not allow_paths:
        return False

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

    if not candidates:
        return False

    # If ANY candidate is eternal, do not allowlist the request as a whole
    # when that eternal path is among the secret targets. We only allowlist
    # when every secret-looking candidate is covered OR at least one
    # non-eternal secret candidate matches. Policy: allowlist applies to
    # non-eternal paths; eternal paths stay reject elsewhere.
    matched_non_eternal = False
    for raw in candidates:
        if not raw.strip():
            continue
        if _one_is_eternal(raw):
            continue
        c_norm = raw.strip().replace("\\", "/")
        c_exp = _norm(raw)
        for g in globs:
            if _path_matches_glob(c_norm, g) or _path_matches_glob(c_exp, g):
                matched_non_eternal = True
                break
            # Pattern-vs-pattern: request pattern subset of allow glob
            if ("*" in raw or "?" in raw) and (
                raw.strip().replace("\\", "/") == g
                or fnmatch.fnmatch(raw.strip().replace("\\", "/"), g)
            ):
                matched_non_eternal = True
                break
        if matched_non_eternal:
            break
        for ap in allow_paths:
            ap_n = ap.replace("\\", "/")
            if c_norm == ap_n or c_exp == ap_n or c_norm.endswith("/" + ap_n.lstrip("./")):
                matched_non_eternal = True
                break
            if _path_matches_glob(c_norm, ap_n) or _path_matches_glob(c_exp, ap_n):
                matched_non_eternal = True
                break
            # allow_paths entry is a prefix directory
            ap_prefix = ap_n.rstrip("/") + "/"
            if c_norm.startswith(ap_prefix) or c_exp.startswith(ap_prefix):
                matched_non_eternal = True
                break
            # basename equality for simple filenames like ".env"
            if _basename(c_norm) == _basename(ap_n) and (
                ap_n in (".env", ".env.local") or ap_n.endswith("/" + _basename(c_norm))
            ):
                # Prefer exact path match; basename-only for relative allow_paths
                if "/" not in ap_n.rstrip("/") or c_norm.endswith(ap_n) or c_exp.endswith(ap_n):
                    matched_non_eternal = True
                    break
        if matched_non_eternal:
            break

    return matched_non_eternal


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


def hard_rule_decision(permission_dict: dict, charter: dict | None = None) -> Optional[dict]:
    """If hard rules fire, return {"reply":"reject","reason":...}; else None.

    When charter allowlists a non-eternal secret path, sets
    permission_dict['_hard_rule_allowlisted']=True and returns None so glue
    can once + log (legitimate small-risk work). Eternal paths and
    always+secret still reject even with a whitelist.
    """
    if not isinstance(permission_dict, dict):
        return None

    # Clear prior flag so callers don't see stale state
    permission_dict.pop("_hard_rule_allowlisted", None)

    paths, patterns = _collect_paths(permission_dict)
    secret = is_secret_path(paths, patterns)
    eternal = is_eternal_reject_path(paths, patterns)
    wants_always = _wants_always(permission_dict)

    # always + secret_adjacent → eternal reject (whitelist cannot override)
    if secret and wants_always:
        return {
            "reply": "reject",
            "reason": "hard_rule: always + secret_adjacent (credential path / always-approve denied)",
        }

    # Eternal paths (ssh, cookies/profile, gh hosts, .netrc) → always reject
    if eternal:
        hint_bits = []
        for p in (paths + patterns)[:3]:
            b = _basename(p) or p
            hint_bits.append(b)
        hint = ", ".join(hint_bits) if hint_bits else "eternal credential path"
        return {
            "reply": "reject",
            "reason": f"hard_rule: eternal reject path ({hint})",
        }

    if secret:
        if path_allowlisted(paths, patterns, charter=charter):
            permission_dict["_hard_rule_allowlisted"] = True
            return None
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

    return None


__all__ = [
    "is_secret_path",
    "is_eternal_reject_path",
    "path_allowlisted",
    "hard_rule_decision",
]
