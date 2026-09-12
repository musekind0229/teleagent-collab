"""Minimal Goal/Task/Run record helpers aligned with contracts/*.schema.json."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

CONTRACT_VERSION = "contract.v0.1-draft"


def new_goal_id(name: str = "goal") -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "goal"))[:40]
    return f"goal_{slug}_{uuid.uuid4().hex[:8]}"


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def contract_fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def make_run(
    *,
    task_id: str,
    attempt: int,
    backend: str,
    contract_fingerprint_value: str,
    workspace_id: str | None = None,
    dispatch_id: str | None = None,
    native_handle: str | None = None,
    state: str = "starting",
) -> dict[str, Any]:
    """Build a Run dict. native_handle is opaque — TeleAgent session ids live only here."""
    return {
        "contract_version": CONTRACT_VERSION,
        "run_id": new_run_id(),
        "task_id": task_id,
        "attempt": int(attempt),
        "backend": backend,
        "native_handle": native_handle,
        "dispatch_id": dispatch_id or f"dispatch_{uuid.uuid4().hex[:10]}",
        "contract_fingerprint": contract_fingerprint_value,
        "workspace_id": workspace_id or "",
        "state": state,
        "error_class": None,
        "usage": {},
        "artifact_refs": [],
    }
