"""Normalize TeleAgent permission payloads into a public PendingAction view.

Scheduler/glue should prefer these fields. Raw TeleAgent keys stay under
`native` for adapter-local reconfirm only — not Goal/Task state.
"""
from __future__ import annotations

from typing import Any


_PUBLIC_KEYS = (
    "request_id",
    "session_id",
    "permission",
    "patterns",
    "path",
    "tool",
    "summary",
)


def session_id_from_raw(p: dict) -> str:
    for k in ("sessionID", "session_id", "sessionId"):
        v = p.get(k)
        if v:
            return str(v)
    meta = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
    for k in ("sessionID", "session_id", "sessionId"):
        v = meta.get(k)
        if v:
            return str(v)
    return ""


def request_id_from_raw(p: dict) -> str:
    return str(p.get("id") or p.get("requestID") or p.get("request_id") or "")


def to_public_permission(raw: dict) -> dict[str, Any]:
    """Map one TeleAgent permission object → public PendingAction."""
    if not isinstance(raw, dict):
        raise TypeError("permission raw must be dict")
    path = raw.get("path")
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    if not path and meta.get("filepath"):
        path = meta.get("filepath")
    tool = raw.get("tool")
    if isinstance(tool, dict):
        tool_name = tool.get("name") or tool.get("tool") or ""
    else:
        tool_name = tool or ""
    patterns = raw.get("patterns")
    if patterns is None:
        patterns = []
    if not isinstance(patterns, list):
        patterns = [patterns]
    summary_bits = [
        str(raw.get("permission") or ""),
        str(path or ""),
        str(tool_name or ""),
    ]
    public = {
        "request_id": request_id_from_raw(raw),
        "session_id": session_id_from_raw(raw),
        "permission": str(raw.get("permission") or raw.get("type") or ""),
        "patterns": patterns,
        "path": str(path or ""),
        "tool": str(tool_name or ""),
        "summary": " ".join(x for x in summary_bits if x).strip()[:240],
        # Opaque native for adapter reconfirm — keep full TA raw (no auth-field drop).
        # Kernel must not parse TA fields from here; missing public fields stay
        # empty strings, never mapped to allow.
        "native": dict(raw),
    }
    return public


def list_public_permissions(raw_items: list, *, session_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        pub = to_public_permission(item)
        if session_id and pub.get("session_id") and pub["session_id"] != session_id:
            continue
        out.append(pub)
    return out


def to_native_for_rules(p: dict) -> dict:
    """Return a TeleAgent-shaped dict for hard_rules / fingerprints.

    Accepts either a raw TA permission or a public PendingAction with `native`.
    """
    if not isinstance(p, dict):
        raise TypeError("permission must be dict")
    if isinstance(p.get("native"), dict) and (p.get("request_id") or p["native"].get("id")):
        native = dict(p["native"])
        rid = p.get("request_id") or native.get("id")
        if rid:
            native.setdefault("id", rid)
        sid = p.get("session_id") or ""
        if sid:
            native.setdefault("sessionID", sid)
        if p.get("path") and not native.get("path"):
            native["path"] = p["path"]
        if p.get("patterns") and not native.get("patterns"):
            native["patterns"] = p["patterns"]
        if p.get("permission") and not native.get("permission"):
            native["permission"] = p["permission"]
        return native
    return p


def prepare_permission(p: dict) -> tuple[dict, dict]:
    """Return (public PendingAction, native TA-shaped dict for hard_rules).

    Callers that only have raw TA input get both views. Callers that already
    hold a public view get native via to_native_for_rules.
    """
    if not isinstance(p, dict):
        raise TypeError("permission must be dict")
    if isinstance(p.get("native"), dict) and (p.get("request_id") or p.get("session_id")):
        public = p
        native = to_native_for_rules(p)
    else:
        public = to_public_permission(p)
        native = to_native_for_rules(public)
    return public, native
