"""Task/Run state vocabulary and tiny transition checks (no TeleAgent HTTP)."""
from __future__ import annotations

from typing import Any

TASK_STATES = frozenset(
    {
        "queued",
        "running",
        "awaiting_decision",
        "review",
        "blocked",
        "unknown",
        "succeeded",
        "failed",
        "cancel_requested",
        "cancelled",
    }
)

RUN_STATES = frozenset(
    {
        "starting",
        "running",
        "awaiting_decision",
        "observing",
        "collecting",
        "succeeded",
        "failed",
        "cancel_requested",
        "cancelled",
        "unknown",
    }
)

ERROR_CLASSES = frozenset(
    {
        "implementation_failed",
        "acceptance_failed",
        "decision_channel_failed",
        "backend_unavailable",
        "budget_exhausted",
        "awaiting_user",
        "unknown",
    }
)

# Conservative edges for the skeleton — expand later without rewriting glue.
_TASK_EDGES = {
    "queued": {"running", "cancelled", "blocked"},
    "running": {"awaiting_decision", "review", "failed", "cancel_requested", "unknown", "blocked"},
    "awaiting_decision": {"running", "blocked", "failed", "cancel_requested", "unknown"},
    "review": {"succeeded", "failed", "running", "awaiting_decision"},
    "blocked": {"queued", "running", "cancelled", "failed"},
    "cancel_requested": {"cancelled", "unknown", "failed"},
    "unknown": {"running", "failed", "cancelled", "review"},
    "succeeded": set(),
    "failed": {"queued"},  # explicit reopen only
    "cancelled": set(),
}


class LifecycleError(ValueError):
    pass


def assert_transition(kind: str, src: str, dst: str) -> None:
    if kind != "task":
        raise LifecycleError(f"unsupported kind={kind!r} (only task skeleton today)")
    if src not in TASK_STATES or dst not in TASK_STATES:
        raise LifecycleError(f"unknown state {src!r} -> {dst!r}")
    allowed = _TASK_EDGES.get(src, set())
    if dst not in allowed:
        raise LifecycleError(f"illegal task transition {src} -> {dst}")


# Scheduler JobState.value → public Task/Run vocabulary (read-only projection).
_JOB_TO_TASK_RUN: dict[str, tuple[str, str]] = {
    "queued": ("queued", "starting"),
    "starting": ("running", "starting"),
    "running": ("running", "running"),
    "pending_approval": ("awaiting_decision", "awaiting_decision"),
    "cancel_requested": ("cancel_requested", "cancel_requested"),
    "cancelled": ("cancelled", "cancelled"),
    "done": ("succeeded", "succeeded"),
    "fail": ("failed", "failed"),
    "timeout": ("failed", "failed"),
}


def project_scheduler_state(
    job_state: str,
    *,
    busy: bool = False,
    force_lead_review: bool = False,
    goal_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Map scheduler JobState → Task/Run words. Never mutates scheduler state.

    Missing/unknown job_state → task/run "unknown" (does not invent success).
    """
    key = (job_state or "").strip()
    task_st, run_st = _JOB_TO_TASK_RUN.get(key, ("unknown", "unknown"))
    # Soft hint only: busy while running → observing; force review terminal path stays review only when done-ish
    if key == "running" and busy:
        run_st = "observing"
    if key == "running" and force_lead_review and not busy:
        # still running until completion gate; do not claim review yet
        pass
    if key == "done" and force_lead_review:
        task_st = "succeeded"  # completion already happened; review was a gate not a lingering state
    err = None
    if key == "timeout":
        err = "budget_exhausted"
    elif key == "fail":
        err = "implementation_failed"
    out: dict[str, Any] = {
        "job_state": key,
        "task_state": task_st,
        "run_state": run_st,
        "error_class": err,
        "goal_id": goal_id or "",
        "task_id": task_id or "",
        "readonly": True,
    }
    if task_st not in TASK_STATES:
        out["task_state"] = "unknown"
    if run_st not in RUN_STATES:
        out["run_state"] = "unknown"
    return out
