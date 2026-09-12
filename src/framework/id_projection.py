"""Stable public goal_id/task_id projection from job + charter (read-only; no judgment)."""
from __future__ import annotations

import hashlib
from typing import Any


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "goal"))[:40] or "goal"


def stable_goal_task_ids(job_id: str, charter: dict | None = None) -> tuple[str, str]:
    """Deterministic ids for a job so persist/restore stays aligned.

    Does not call map_charter_to_goal_task (that mints fresh UUIDs each time).
    """
    jid = (job_id or "").strip() or "job"
    name = ""
    if isinstance(charter, dict):
        name = str(charter.get("name") or "")
    digest = hashlib.sha256(f"{jid}|{name}".encode("utf-8")).hexdigest()
    goal_id = f"goal_{_slug(name or jid)}_{digest[:8]}"
    task_id = f"task_{digest[8:20]}"
    return goal_id, task_id


def ids_dict(job_id: str, charter: dict | None = None) -> dict[str, Any]:
    gid, tid = stable_goal_task_ids(job_id, charter)
    return {"goal_id": gid, "task_id": tid, "readonly": True}
