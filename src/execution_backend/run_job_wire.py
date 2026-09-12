"""Wire old bin/run-job.py to a selectable ExecutionBackend.

Default remains TeleAgent (glue). inprocess.local_v1 uses only the public
ExecutionBackend surface: start_run / observe_run / collect_result plus
empty list_pending_actions. reply_permission stays unsupported — never
invent once/approve. Hermes is not offered.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from execution_backend.base import BackendError, BackendStatus
from execution_backend.inprocess_v1 import run_file_job_via_public_api

ENV_NAME = "COLLAB_EXECUTION_BACKEND"
KIND_TELEAGENT = "teleagent"
KIND_INPROCESS = "inprocess"

_TELEAGENT_ALIASES = frozenset(
    {
        "teleagent",
        "teleagent.linux.local_v1",
        "ta",
        "glue",
    }
)
_INPROCESS_ALIASES = frozenset(
    {
        "inprocess",
        "inprocess.local_v1",
        "in_process",
    }
)


def resolve_run_job_backend(
    cli_value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return KIND_TELEAGENT or KIND_INPROCESS.

    Priority: CLI ``--backend`` > env COLLAB_EXECUTION_BACKEND > teleagent.
    Unknown names (including hermes) raise BackendError UNSUPPORTED.
    """
    raw = (cli_value or "").strip()
    if not raw:
        env = environ if environ is not None else os.environ
        raw = str(env.get(ENV_NAME) or "").strip()
    if not raw:
        return KIND_TELEAGENT
    key = raw.lower()
    if key in _TELEAGENT_ALIASES:
        return KIND_TELEAGENT
    if key in _INPROCESS_ALIASES:
        return KIND_INPROCESS
    raise BackendError(
        BackendStatus.UNSUPPORTED,
        f"unsupported execution backend {raw!r}; "
        "supported: teleagent, inprocess (inprocess.local_v1). "
        "Hermes is not a collab execution backend.",
        capability="select_backend",
    )


def run_inprocess_charter(
    *,
    charter: dict,
    workdir: str | Path,
    instruction: str = "",
    name: str = "",
) -> dict[str, Any]:
    """Run a charter through inprocess.local_v1 public API only (no glue, no TA HTTP)."""
    raw = run_file_job_via_public_api(workdir=workdir, charter=charter)
    notes: list[str] = [
        "backend=inprocess.local_v1 public API only (start_run/observe_run/collect_result)",
        "list_pending_actions empty; reply_permission unsupported (not called as approve)",
        f"used_public_api_only={bool(raw.get('used_public_api_only'))}",
        f"pending_count={raw.get('pending_count', 0)}",
        f"native_handle={raw.get('native_handle') or ''}",
    ]
    if instruction:
        notes.append(f"instruction_chars={len(instruction)}")
    if raw.get("error"):
        notes.append(f"backend_error={raw.get('error')}")
    report: dict[str, Any] = {
        "name": name or str(charter.get("name") or "job"),
        "session_id": raw.get("run_id") or "",
        "pending_seen": False,
        "pending_summaries": [],
        "grok_permission_decision": "",
        "grok_review_decision": "",
        "api_replies": [],
        "hard_rule_rejects": [],
        "artifacts": list(raw.get("artifacts") or []),
        "state": raw.get("state") or ("ok" if raw.get("ok") else "fail"),
        "ok": bool(raw.get("ok")),
        "error": raw.get("error") or "",
        "path": "inprocess.local_v1",
        "notes": notes,
        "dry_run": False,
        "backend": raw.get("backend") or "inprocess.local_v1",
        "used_public_api_only": bool(raw.get("used_public_api_only")),
        "native_handle": raw.get("native_handle") or "",
        "run_observation": raw.get("run_observation") or {},
        "pending_count": int(raw.get("pending_count") or 0),
    }
    # Read-only Goal/Task projection — must not flip ok/state
    try:
        from framework.project_report import attach_framework_projection

        attach_framework_projection(report, charter)
    except Exception as e:  # noqa: BLE001
        report.setdefault("notes", []).append(f"framework_projection skipped: {e}")
    return report
