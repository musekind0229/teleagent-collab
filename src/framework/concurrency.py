"""Bounded dispatch for independent tasks of one Goal.

Pure helpers. No durable store and no coordinator imports.
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from framework.artifact_handoff import scheduler_task_workspace

# Statuses that occupy a concurrency slot, including a task paused on a decision.
IN_FLIGHT = frozenset({"running", "awaiting_decision", "review", "cancel_requested"})

_GLOBAL_KINDS = frozenset({"plan_review", "return_to_upper"})


def as_limit(value: Any) -> int | None:
    """Positive int, or None when the value is not a usable run cap."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def budget_block_reason(snap: Mapping[str, Any], *, now: float | None = None) -> str:
    """Wall clock or rework cap, or "" when the goal may still run.

    ``max_reworks`` 0 with zero retries does not trip. A missing or non-numeric
    ``wall_sec`` is not a budget. Capacity is not a budget.
    """
    goal = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
    budget = goal.get("budget") if isinstance(goal.get("budget"), Mapping) else {}
    wall = budget.get("wall_sec")
    if isinstance(wall, (int, float)) and not isinstance(wall, bool) and float(wall) > 0:
        created = snap.get("created_at")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            clock = time.time() if now is None else float(now)
            if clock - float(created) >= float(wall):
                return "budget_exceeded wall_sec"
    max_reworks = budget.get("max_reworks")
    if isinstance(max_reworks, int) and not isinstance(max_reworks, bool) and max_reworks >= 0:
        spent = 0
        for row in snap.get("history") or []:
            if isinstance(row, Mapping) and row.get("op") == "retry_task":
                spent += 1
        if spent > max_reworks:
            return "budget_exceeded max_reworks"
    return ""


def task_status(task: Mapping[str, Any]) -> str:
    return str(task.get("status") or "")


def _deps(task: Mapping[str, Any]) -> list[str]:
    raw = task.get("depends_on") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _status_map(tasks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for task in tasks:
        if isinstance(task, Mapping):
            out[str(task.get("task_id") or "")] = task_status(task)
    return out


def task_is_ready(task: Mapping[str, Any], status_by_id: Mapping[str, str]) -> bool:
    """Queued, and every dependency has succeeded."""
    if task_status(task) != "queued":
        return False
    return all(status_by_id.get(dep) == "succeeded" for dep in _deps(task))


def task_waiting_on_live_dep(task: Mapping[str, Any], status_by_id: Mapping[str, str]) -> bool:
    """Queued behind a dep that is still queued or in flight, not failed or cancelled."""
    if task_status(task) != "queued":
        return False
    deps = _deps(task)
    if not deps:
        return False
    if any(status_by_id.get(dep) in {"failed", "cancelled"} for dep in deps):
        return False
    if all(status_by_id.get(dep) == "succeeded" for dep in deps):
        return False
    return True


def goal_has_progress(tasks: Sequence[Any]) -> bool:
    """True when some task is in flight, ready, or blocked only on a live dependency."""
    rows = [task for task in tasks if isinstance(task, Mapping)]
    status_by_id = _status_map(rows)
    for task in rows:
        if task_status(task) in IN_FLIGHT:
            return True
        if task_is_ready(task, status_by_id):
            return True
        if task_waiting_on_live_dep(task, status_by_id):
            return True
    return False


def occupies_run_slot(task: Mapping[str, Any]) -> bool:
    """In flight AND holding a live worker run.

    A budget/no-progress checkpoint parks the task in awaiting_decision after
    its run was collected or cancelled. Waiting for that human verdict must not
    eat a backend slot, or one parked checkpoint blocks every other Goal (#10/#13).
    The marker only counts while the task is still blocked on that same decision.
    """
    status = task_status(task)
    if status not in IN_FLIGHT:
        return False
    if status == "awaiting_decision" and task.get("run_parked") is True:
        parked = str(task.get("parked_decision") or "")
        if parked and parked == str(task.get("blocked_on_decision") or ""):
            return False
    return True


def count_inflight(tasks: Sequence[Any]) -> int:
    return sum(1 for task in tasks if isinstance(task, Mapping) and occupies_run_slot(task))


def decision_is_global(decision: Mapping[str, Any]) -> bool:
    """Approvals that gate every dispatch. A task-scoped question does not."""
    kind = str(decision.get("kind") or "")
    task_id = str(decision.get("task_id") or "").strip()
    if decision.get("return_to_upper") is True:
        return True
    if kind in _GLOBAL_KINDS or kind.startswith("escalate_"):
        return True
    if not task_id:
        return True
    return False


def global_decision_blocks(pending: Sequence[Any] | None) -> bool:
    return any(isinstance(row, Mapping) and decision_is_global(row) for row in (pending or []))


def dispatch_directory(
    task: Mapping[str, Any],
    *,
    workspaces_root: str | Path,
    goal_id: str,
) -> str:
    """Directory passed to ``start_run``.

    Prefer a workspace already bound at dispatch. Otherwise an explicit
    ``inputs.workdir`` / ``inputs.directory``. Otherwise the scheduler path,
    as the same un-resolved string historical binds stored.
    """
    bound = task.get("workspace")
    if isinstance(bound, str) and bound.strip():
        return bound.strip()
    inputs = task.get("inputs") if isinstance(task.get("inputs"), Mapping) else {}
    for key in ("workdir", "directory"):
        raw = inputs.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return str(
        scheduler_task_workspace(
            workspaces_root,
            str(task.get("goal_id") or goal_id),
            str(task.get("task_id") or ""),
        )
    )


def directory_key(path: str) -> str:
    """Comparable directory identity. Resolution failures fall back to the text."""
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(Path(text).resolve()))
    except OSError:
        return os.path.normcase(text)


def remaining_slots(
    *,
    per_goal: int,
    running_here: int,
    global_limit: int,
    running_total: int,
    backend_limit: int | None,
) -> int:
    slots = int(per_goal) - int(running_here)
    slots = min(slots, int(global_limit) - int(running_total))
    if backend_limit is not None:
        slots = min(slots, int(backend_limit) - int(running_total))
    return max(0, slots)


def public_concurrency(
    *,
    max_parallel_per_goal: int,
    max_parallel_global: int,
    backend_caps: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Service caps overlaid on a backend ``concurrency`` declaration.

    ``effective`` is the minimum of the known numeric caps. ``"unknown"`` does
    not reduce it. ``limited_by`` names every cap whose value equals ``effective``.
    Backend reason labels are used when the backend declared them.
    """
    per = as_limit(max_parallel_per_goal) or 2
    glob = as_limit(max_parallel_global) or 4
    declared: Mapping[str, Any] = {}
    if isinstance(backend_caps, Mapping):
        raw = backend_caps.get("concurrency")
        if isinstance(raw, Mapping):
            declared = raw
    backend_raw = declared.get("max_runs", "unknown")
    backend_num = as_limit(backend_raw)
    labels: list[str] = []
    raw_labels = declared.get("limited_by")
    if isinstance(raw_labels, list):
        for item in raw_labels:
            if isinstance(item, str) and item.strip() and item.strip() not in labels:
                labels.append(item.strip())
    effective = per if per < glob else glob
    if backend_num is not None and backend_num < effective:
        effective = backend_num
    reasons: list[str] = []
    if per == effective:
        reasons.append("max_parallel_per_goal")
    if glob == effective:
        reasons.append("max_parallel_global")
    if backend_num is not None and backend_num == effective:
        if labels:
            for name in labels:
                if name not in reasons:
                    reasons.append(name)
        elif "backend_max_runs" not in reasons:
            reasons.append("backend_max_runs")
    return {
        "max_parallel_per_goal": per,
        "max_parallel_global": glob,
        "backend_max_runs": backend_num if backend_num is not None else "unknown",
        "effective": effective,
        "limited_by": reasons,
    }


def scheduler_view(
    *,
    tasks: Sequence[Any],
    pending: Sequence[Any] | None,
    per_goal: int,
    global_limit: int,
    backend_limit: int | None,
    running_elsewhere: int,
    held_keys: set[str],
    workspaces_root: str | Path,
    goal_id: str,
) -> dict[str, Any]:
    """Derived scheduler snapshot. Not persisted. Capacity shortage is not a failure."""
    rows = [task for task in tasks if isinstance(task, Mapping)]
    running = count_inflight(rows)
    status_by_id = _status_map(rows)
    ready = [task for task in rows if task_is_ready(task, status_by_id)]
    parts = [int(per_goal), max(0, int(global_limit) - int(running_elsewhere))]
    if backend_limit is not None:
        parts.append(max(0, int(backend_limit) - int(running_elsewhere)))
    capacity = min(parts) if parts else 0
    slots = remaining_slots(
        per_goal=per_goal,
        running_here=running,
        global_limit=global_limit,
        running_total=running + int(running_elsewhere),
        backend_limit=backend_limit,
    )
    reason = ""
    if ready:
        if global_decision_blocks(pending):
            reason = "global_approval"
        elif slots <= 0 or running >= capacity:
            reason = "capacity"
        elif all(
            directory_key(dispatch_directory(task, workspaces_root=workspaces_root, goal_id=goal_id)) in held_keys
            for task in ready
        ):
            reason = "workdir_claim"
    return {
        "running": running,
        "queued_ready": len(ready),
        "capacity": capacity,
        "waiting_reason": reason,
    }


def all_tasks_succeeded(tasks: Sequence[Any]) -> bool:
    rows = [task for task in tasks if isinstance(task, Mapping)]
    return bool(rows) and all(task_status(task) == "succeeded" for task in rows)


def any_task_failed(tasks: Sequence[Any]) -> bool:
    return any(isinstance(task, Mapping) and task_status(task) == "failed" for task in tasks)
