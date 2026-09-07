"""Canonical path helpers — forbid bare startswith workspace/allowlist checks."""
from __future__ import annotations

import os
from pathlib import Path


def canonicalize(path: str | None, *, base: str | None = None) -> str:
    """Expand user, resolve (realpath-ish), return absolute string.

    Non-existent paths still resolve parents via Path.resolve(strict=False).
    Relative paths are joined to *base* when given.
    """
    if path is None:
        return ""
    s = str(path).strip()
    if not s:
        return ""
    try:
        s = os.path.expanduser(s)
    except Exception:
        pass
    p = Path(s)
    if not p.is_absolute() and base:
        p = Path(os.path.expanduser(base)) / p
    try:
        return str(p.resolve(strict=False))
    except Exception:
        try:
            return str(p.absolute())
        except Exception:
            return s.replace("\\", "/")


def is_path_within(child: str | None, parent: str | None) -> bool:
    """True iff canonical child is parent or a descendant (boundary-safe).

    Unlike ``child.startswith(parent)``, this rejects ``/ws/job-evil`` when
    parent is ``/ws/job``.
    """
    if not child or not parent:
        return False
    c = canonicalize(child)
    p = canonicalize(parent)
    if not c or not p:
        return False
    try:
        Path(c).relative_to(p)
        return True
    except ValueError:
        return False
    except Exception:
        return False


def permission_fingerprint(p: dict) -> str:
    """Stable fingerprint of permission content for reconfirm (exclude volatile ids only kept as id)."""
    import hashlib
    import json

    if not isinstance(p, dict):
        return ""
    keep_keys = (
        "id",
        "requestID",
        "sessionID",
        "session_id",
        "sessionId",
        "path",
        "paths",
        "patterns",
        "pattern",
        "tool",
        "permission",
        "permissions",
        "command",
        "message",
        "title",
        "description",
        "ops",
        "operations",
        "type",
        "metadata",
    )
    slim = {k: p.get(k) for k in keep_keys if k in p}
    blob = json.dumps(slim, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


__all__ = ["canonicalize", "is_path_within", "permission_fingerprint"]
