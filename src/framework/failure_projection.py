"""Project failed tasks onto Goal status.

need_human stays a separate flag. This module only reads task results and
returns a short, sanitized brief. It does not copy stdout or the goal contract.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from framework.need_human import sanitize_reason

# Public source vocabulary. task_failed is the conservative default.
FAILURE_SOURCES = (
    "worker_timeout",
    "spawn",
    "contract_render",
    "acceptance",
    "contamination",
    "task_failed",
    "cancelled",
)
_EXPLICIT_SOURCES = frozenset(FAILURE_SOURCES) - {"task_failed"}

# Goal-level (no task result) failure sources; kept apart from task sources.
GOAL_FAILURE_SOURCES = ("planner", "coordination")

# Same wall-budget phrases the Hermes client already treats as a goal timeout.
# A step budget (budget_exceeded steps) is intentionally not in this list.
_WALL_MARKERS = (
    "budget_exceeded wall",
    "budget_exceeded: wall",
    "budget_exhausted",
    "timed_out",
    "wall clock",
    "wall_clock",
    "wall budget",
    "wall_budget",
    "wall_sec",
    "wall_s=",
    "deadline exhausted",
)

_NEXT_STEP = {
    "worker_timeout": (
        "raise budget.wall_sec or split the task; open a NEW request citing this request_id"
    ),
    "spawn": "check collab-service --ready",
    "contract_render": (
        "fix the worker contract and open a NEW request citing this request_id"
    ),
    "acceptance": (
        "fix the artifact or acceptance criteria and open a NEW request citing this request_id"
    ),
    "contamination": (
        "remove AIGC or invisible marks, or set acceptance.allow_aigc_marks; "
        "open a NEW request citing this request_id"
    ),
    "cancelled": "open a NEW request citing this request_id if the work is still needed",
    "task_failed": "inspect the task error and open a NEW request citing this request_id",
}

_GOAL_NEXT_STEP = {
    "planner": (
        "no worker was started; check the lead (lead_status) and open a NEW request "
        "citing this request_id"
    ),
    "coordination": "inspect the goal failure and open a NEW request citing this request_id",
}

_MISSING_CAP = 32


def _one_line(value: Any, *, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[:limit]
    return text


def _result_of(task: Mapping[str, Any]) -> Mapping[str, Any]:
    result = task.get("result")
    if isinstance(result, Mapping):
        return result
    return {}


def _error_text(task: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    for value in (
        result.get("error"),
        task.get("error"),
        result.get("failure_reason"),
        task.get("failure_reason"),
    ):
        if isinstance(value, str) and value.strip():
            return sanitize_reason(value)
    return ""


def _collapsed(text: str) -> str:
    return " ".join(text.lower().split())


def _is_worker_timeout(error_lower: str, result: Mapping[str, Any]) -> bool:
    """True for error ``timeout`` or a wall-budget marker. Step budgets are not."""
    if error_lower == "timeout":
        return True
    step_only = (
        "budget_exceeded" in error_lower
        and "steps" in error_lower
        and "wall" not in error_lower
        and "timeout" not in error_lower
        and "timed_out" not in error_lower
    )
    if step_only:
        return False
    if any(marker in error_lower for marker in _WALL_MARKERS):
        return True
    if "budget_exceeded" in error_lower and "timeout" in error_lower:
        return True
    if not error_lower:
        for key in ("state", "controller_state"):
            state = _collapsed(str(result.get(key) or ""))
            if state in {"timeout", "timed_out"}:
                return True
    return False


def _is_cancelled(task: Mapping[str, Any], result: Mapping[str, Any], error_lower: str) -> bool:
    if str(task.get("status") or "") == "cancelled":
        return True
    if error_lower in {"cancelled", "canceled", "cancel"}:
        return True
    if error_lower.startswith("cancelled:") or error_lower.startswith("canceled:"):
        return True
    for key in ("state", "controller_state", "finish"):
        state = _collapsed(str(result.get(key) or ""))
        if state in {"cancelled", "canceled", "cancel"}:
            return True
    return False


def classify_failure_source(task: Mapping[str, Any], result: Mapping[str, Any], error: str) -> str:
    """Map one failed task onto the public source enum. Default is task_failed."""
    explicit = str(result.get("error_source") or "").strip()
    if explicit in _EXPLICIT_SOURCES:
        return explicit
    lowered = _collapsed(error)
    if _is_worker_timeout(lowered, result):
        return "worker_timeout"
    if lowered.startswith("backend dispatch failed"):
        return "spawn"
    if "contract_render_error" in lowered or lowered.startswith("contract_render"):
        return "contract_render"
    if result.get("acceptance_failed") is True or "acceptance_failed" in lowered:
        return "acceptance"
    if (
        "artifact_contaminated" in lowered
        or "artifact contaminated" in lowered
        or isinstance(result.get("artifact_contamination"), Mapping)
    ):
        return "contamination"
    if _is_cancelled(task, result, lowered):
        return "cancelled"
    return "task_failed"


def _missing_artifacts(result: Mapping[str, Any]) -> list[str]:
    raw: Any = None
    for key in ("missing_artifacts", "missing"):
        if key in result:
            raw = result.get(key)
            break
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (bytes, bytearray)) or not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    found: list[str] = []
    for item in raw:
        text = ""
        if isinstance(item, str):
            text = sanitize_reason(item)
        elif isinstance(item, Mapping):
            label = item.get("path") or item.get("name") or item.get("relative") or ""
            if isinstance(label, str) and label.strip():
                text = sanitize_reason(label)
        if text and text not in found:
            found.append(text)
        if len(found) >= _MISSING_CAP:
            break
    return found


def _run_id(task: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    for source in (result, task):
        for key in ("run_id", "native_handle"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return _one_line(value.strip())
    return ""


def failure_brief(task: Mapping[str, Any]) -> dict[str, Any]:
    """One sanitized failed-task record. retryable is conservative (always false).

    Same-goal POST /retry is only for need_human. A blind retry of a timeout,
    spawn, contract, acceptance, or contamination failure would repeat it.
    """
    result = _result_of(task)
    error = _error_text(task, result)
    source = classify_failure_source(task, result, error)
    if not error and source == "worker_timeout":
        error = "timeout"
    return {
        "task_id": _one_line(task.get("task_id") or ""),
        "run_id": _run_id(task, result),
        "title": sanitize_reason(str(task.get("title") or "")),
        "error": error,
        "source": source,
        "missing_artifacts": _missing_artifacts(result),
        "retryable": False,
        "next_step": _NEXT_STEP[source],
    }


def goal_level_failure_brief(failure: Any) -> dict[str, Any] | None:
    """Brief for a Goal that failed before any task result (planning, coordination).

    Same shape as failure_brief plus ``stage`` and optional ``code``/``lead_status``.
    No task_id/run_id is invented.
    """
    if not isinstance(failure, Mapping):
        return None
    error = sanitize_reason(str(failure.get("error") or ""))
    stage = _one_line(failure.get("phase") or "coordination", limit=40)
    source = str(failure.get("source") or "").strip()
    if source not in GOAL_FAILURE_SOURCES:
        source = "planner" if stage == "planning" else "coordination"
    brief: dict[str, Any] = {
        "task_id": "",
        "run_id": "",
        "stage": stage,
        "error": error or "goal failed",
        "source": source,
        "missing_artifacts": [],
        "retryable": False,
        "next_step": _GOAL_NEXT_STEP[source],
    }
    for key in ("code", "lead_status"):
        value = failure.get(key)
        if isinstance(value, str) and value.strip():
            brief[key] = _one_line(value, limit=60)
    return brief


def project_failed_tasks(tasks: Sequence[Any]) -> dict[str, Any] | None:
    """Briefs for every failed task, in plan order. None when nothing failed.

    failure_reason is the primary (first) task only. Later failures stay in
    the list so callers do not collapse several causes into one sentence.
    """
    briefs: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        if str(task.get("status") or "") != "failed":
            continue
        briefs.append(failure_brief(task))
    if not briefs:
        return None
    primary = briefs[0]
    reason = primary["error"] or "task failed"
    return {
        "failure_reason": reason,
        "primary_failure": primary,
        "failures": briefs,
    }
