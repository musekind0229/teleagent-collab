"""Task/Run state vocabulary and tiny transition checks (no TeleAgent HTTP)."""
from __future__ import annotations

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
