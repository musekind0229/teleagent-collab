"""Opaque native handles for TeleAgent — keep HTTP/session fields out of the public kernel."""
from __future__ import annotations

from typing import Any


def wrap_session_handle(session_id: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Adapter-local envelope. Kernel should store only a string ref or this opaque blob."""
    out: dict[str, Any] = {"backend": "teleagent.linux.local_v1", "session_id": session_id}
    if extra:
        # Permission/question payloads stay nested under native — not promoted to Goal state.
        out["native"] = dict(extra)
    return out


def session_id_of(handle: dict[str, Any] | str | None) -> str | None:
    if handle is None:
        return None
    if isinstance(handle, str):
        return handle
    if isinstance(handle, dict):
        return handle.get("session_id") or (handle.get("native") or {}).get("sessionID")
    return None
