"""Local application facade: external request -> lead plan -> worker tasks.

The caller only speaks in Goals. It does not address a lead or worker. The
coordinator owns those implementation details and persists public state in
``DurableLayer``. The first release intentionally defaults to the deterministic
in-process backend; Windows TeleAgent can be selected after its live auth path
passes acceptance.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlparse

from execution_backend.base import ExecutionBackend
from execution_backend.inprocess_v1 import InProcessExecutionBackend
from framework.artifact_handoff import HandoffError, handoff_direct_dependency_artifacts
from framework.need_human import goal_need_human_view, sanitize_reason
from framework.durable_api import DurableLayer
from lead_adapter.schema import context_summary_of, unwrap_structured

API_VERSION = "collab-app.v0.1"
MAX_BODY_BYTES = 1024 * 1024
MAX_PLAN_TASKS = 8


class AppError(ValueError):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request") -> None:
        self.status = int(status)
        self.code = code
        super().__init__(message)


class GoalPlanner(Protocol):
    name: str

    def plan(self, goal_snapshot: Mapping[str, Any]) -> dict[str, Any]: ...


def _strings(value: Any, *, field: str, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise AppError(f"{field} is required")
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise AppError(f"{field} must be an array of non-empty strings")
    return [x.strip() for x in value]


def _safe_artifacts(value: Any) -> list[str]:
    artifacts = _strings(value, field="acceptance.artifacts", required=True)
    out: list[str] = []
    for raw in artifacts:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise AppError(f"artifact must be a relative path inside the task workspace: {raw!r}")
        out.append(path.as_posix())
    return out


def _task_id(goal_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{goal_id}\0{key}".encode("utf-8")).hexdigest()[:16]
    return f"task_{digest}"


GOAL_HTTP_TERMINAL = frozenset({"completed", "failed", "cancelled"})
TASK_HTTP_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MAX_FORBIDDEN_TOOLS = 32


def project_forbidden_tools(
    *,
    payload: Mapping[str, Any] | None = None,
    goal_text: str = "",
    must: list[str] | None = None,
    must_not: list[str] | None = None,
) -> list[str]:
    """Return the explicit Goal.forbidden_tools field after legal validation.

    Goal/must/must_not text is ignored: wording such as "use PowerShell" is
    not a ban. The persisted list is copied into the worker charter for
    post-hoc acceptance detection after a tool has completed. It is not OS
    isolation and cannot prevent TeleAgent from auto-allowing a tool.
    """
    del goal_text, must, must_not
    src = payload if isinstance(payload, Mapping) else {}
    if "forbidden_tools" not in src or src.get("forbidden_tools") is None:
        return []
    found = _strings(src.get("forbidden_tools"), field="forbidden_tools")
    if len(found) > MAX_FORBIDDEN_TOOLS:
        raise AppError(
            f"forbidden_tools must have at most {MAX_FORBIDDEN_TOOLS} entries",
            code="invalid_forbidden_tools",
        )
    if len(found) != len(set(found)):
        raise AppError(
            "forbidden_tools must be a unique list of non-empty tool names",
            code="invalid_forbidden_tools",
        )
    return found


def worker_charter_for_task(
    *,
    goal: Mapping[str, Any] | None,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-task worker contract. Do not send the whole Goal as the worker goal."""
    goal_obj = goal if isinstance(goal, Mapping) else {}
    boundaries = goal_obj.get("boundaries") if isinstance(goal_obj.get("boundaries"), Mapping) else {}
    instruction = str((task.get("inputs") or {}).get("instruction") or task.get("title") or "").strip()
    budget = goal_obj.get("budget") if isinstance(goal_obj.get("budget"), Mapping) else {}
    charter: dict[str, Any] = {
        "goal": instruction,
        "must": [str(x) for x in (boundaries.get("must") or [])],
        "must_not": [str(x) for x in (boundaries.get("must_not") or [])],
        "done_when": task.get("done_when") or {},
        "timeout_sec": budget.get("wall_sec"),
        "max_redos": budget.get("max_reworks"),
    }
    raw_forbidden = goal_obj.get("forbidden_tools")
    if isinstance(raw_forbidden, list):
        forbidden = [str(x).strip() for x in raw_forbidden if str(x).strip()]
        if forbidden:
            charter["forbidden_tools"] = forbidden
    raw_inputs = (task.get("inputs") or {}).get("input_files") if isinstance(task.get("inputs"), Mapping) else None
    if isinstance(raw_inputs, list) and raw_inputs:
        names: list[str] = []
        for item in raw_inputs:
            if isinstance(item, Mapping) and str(item.get("relative") or "").strip():
                names.append(str(item.get("relative")).strip())
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
        if names:
            charter["input_files"] = names
    return charter


MAX_LEAD_ATTEMPTS_PER_DECISION = 2
AUTO_RESOLVE_BACKEND_KINDS = frozenset({"permission", "review"})
# Permission / question / review / system_action surface through Goal decision API.
# system_action maps to system_action_approval and is NEVER in AUTO_RESOLVE_BACKEND_KINDS.
PROJECTABLE_BACKEND_KINDS = frozenset({"permission", "question", "review", "system_action"})
_NONRETRYABLE_LEAD_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "429",
    "401",
    "unauthorized",
    "login",
    "sign in",
    "signin",
    "authentication",
    "not logged",
    "credit",
    "billing",
    "insufficient_quota",
    "payment",
    "spawn failed",
)
TASK_REVIEW_HINT = (
    "This is a Task-level review for the current task only, not Goal completion. "
    "Pass if this task's expected artifacts and this task's tool evidence satisfy "
    "task acceptance_criteria. Do not fail because later tasks or remaining Goal "
    "artifacts are unfinished. Still fail if this task violated prohibitions or "
    "did not produce its own artifacts."
)


def task_acceptance_criteria(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """Acceptance used for a single Task review. Goal remaining work stays on the Goal."""
    row = task if isinstance(task, Mapping) else {}
    done = row.get("done_when") if isinstance(row.get("done_when"), Mapping) else {}
    artifacts = [str(x) for x in (done.get("artifacts") or row.get("expected_artifacts") or []) if str(x).strip()]
    text = str(done.get("text") or "").strip()
    return {"artifacts": artifacts, "text": text}


_TASK_LABEL_RE = re.compile(r"\btask\s+([a-z])\b", re.IGNORECASE)


def _task_letters(siblings: list[Mapping[str, Any]]) -> dict[str, str]:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {
        str(row.get("task_id") or ""): letters[i]
        for i, row in enumerate(siblings)
        if isinstance(row, Mapping) and i < len(letters) and str(row.get("task_id") or "").strip()
    }


def _mentions_task_label(text: str, letter: str) -> bool:
    if not letter or len(letter) != 1 or not letter.isalpha():
        return False
    return bool(re.search(rf"\btask\s+{re.escape(letter)}\b", text, re.IGNORECASE))


def _mentions_task_title(text: str, title: str) -> bool:
    phrase = str(title or "").strip()
    if len(phrase) < 2:
        return False
    return phrase.lower() in text.lower()


def split_task_musts(
    *,
    musts: list[str],
    task: Mapping[str, Any] | None,
    siblings: list[Mapping[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Keep Goal musts; only defer items that name a later Task and not this Task.

    Deferred items remain on the Goal and in extra.goal_must. They must not be
    used as this Task's pass/fail authorized_scope. Unknown Task labels stay
    in scope so the lead still sees them.
    """
    rows = [row for row in (siblings or []) if isinstance(row, Mapping)]
    current = task if isinstance(task, Mapping) else {}
    letters = _task_letters(rows)
    current_id = str(current.get("task_id") or "")
    current_letter = letters.get(current_id, "")
    current_title = str(current.get("title") or "").strip()
    current_idx = next(
        (i for i, row in enumerate(rows) if str(row.get("task_id") or "") == current_id),
        -1,
    )
    known_letters = {letter.upper() for letter in letters.values() if letter}
    later_letters: list[str] = []
    later_titles: list[str] = []
    for i, row in enumerate(rows):
        rid = str(row.get("task_id") or "")
        if rid == current_id or i <= current_idx:
            continue
        letter = letters.get(rid, "")
        if letter:
            later_letters.append(letter)
        title = str(row.get("title") or "").strip()
        if len(title) >= 2:
            later_titles.append(title)
    scoped: list[str] = []
    deferred: list[str] = []
    for raw in musts:
        text = str(raw)
        mentioned = {mark.upper() for mark in _TASK_LABEL_RE.findall(text)}
        unknown = bool(mentioned - known_letters)
        mentions_current = _mentions_task_label(text, current_letter) or _mentions_task_title(
            text, current_title
        )
        mentions_later = any(_mentions_task_label(text, letter) for letter in later_letters) or any(
            _mentions_task_title(text, title) for title in later_titles
        )
        if mentions_later and not mentions_current and not unknown:
            deferred.append(text)
        else:
            scoped.append(text)
    return scoped, deferred


def lead_error_retryable(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "").lower()
    blob = f"{code} {exc} {type(exc).__name__}".lower()
    if any(marker in blob for marker in _NONRETRYABLE_LEAD_MARKERS):
        return False
    if code == "timeout" or "timed out" in blob or "timeout" == code:
        return True
    if code in {
        "illegal_json",
        "application_id_mismatch",
        "context_summary_mismatch",
        "illegal_verdict",
        "missing_reason",
    }:
        return True
    return False


def _lead_error_record(exc: BaseException) -> dict[str, Any]:
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    return {
        "type": type(exc).__name__,
        "code": code[:80],
        "message": str(exc)[:240],
        "retryable": lead_error_retryable(exc),
    }


def planning_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["application_id", "context_summary", "summary", "tasks"],
        "properties": {
            "application_id": {"type": "string"},
            "context_summary": {"type": "string"},
            "summary": {"type": "string"},
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_TASKS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_key", "title", "instruction", "depends_on", "artifacts"],
                    "properties": {
                        "task_key": {"type": "string"},
                        "title": {"type": "string"},
                        "instruction": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "artifacts": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }


def validate_plan(plan: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise AppError("lead did not return a plan object", code="invalid_plan")
    if str(plan.get("application_id") or "") != str(request.get("application_id") or ""):
        raise AppError("lead plan application_id mismatch", code="stale_plan")
    if str(plan.get("context_summary") or "") != str(request.get("context_summary") or ""):
        raise AppError("lead plan context_summary mismatch", code="stale_plan")
    rows = plan.get("tasks")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PLAN_TASKS:
        raise AppError(f"lead plan must contain 1..{MAX_PLAN_TASKS} tasks", code="invalid_plan")
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise AppError("plan task must be an object", code="invalid_plan")
        key = str(row.get("task_key") or "").strip()
        title = str(row.get("title") or "").strip()
        instruction = str(row.get("instruction") or "").strip()
        if not key or key in seen or not title or not instruction:
            raise AppError("plan task keys must be unique and task fields non-empty", code="invalid_plan")
        seen.add(key)
        clean.append(
            {
                "task_key": key,
                "title": title,
                "instruction": instruction,
                "depends_on": _strings(row.get("depends_on") or [], field="task.depends_on"),
                "artifacts": _safe_artifacts(row.get("artifacts")),
            }
        )
    for row in clean:
        if any(dep not in seen or dep == row["task_key"] for dep in row["depends_on"]):
            raise AppError("task dependency references an unknown/self task", code="invalid_plan")
    return {
        "application_id": str(plan["application_id"]),
        "context_summary": str(plan["context_summary"]),
        "summary": str(plan.get("summary") or "").strip(),
        "tasks": clean,
    }


class DeterministicPlanner:
    """Safe bootstrap planner: one bounded implementation task per Goal."""

    name = "deterministic.single_task"

    def plan(self, goal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        goal = goal_snapshot.get("goal") if isinstance(goal_snapshot.get("goal"), Mapping) else {}
        acceptance = goal.get("acceptance") if isinstance(goal.get("acceptance"), Mapping) else {}
        artifacts = _safe_artifacts(acceptance.get("artifacts"))
        request = build_planning_request(goal_snapshot)
        raw = {
            "application_id": request["application_id"],
            "context_summary": request["context_summary"],
            "summary": "Single bounded task generated by the bootstrap planner",
            "tasks": [
                {
                    "task_key": "implement",
                    "title": str(goal.get("title") or "implementation"),
                    "instruction": str(goal.get("desired_outcome") or "Complete the requested outcome"),
                    "depends_on": [],
                    "artifacts": artifacts,
                }
            ],
        }
        return validate_plan(raw, request)


class LeadAdapterPlanner:
    """Use any existing LeadAdapter to turn a Goal into a bounded task graph."""

    name = "lead_adapter.plan_v1"

    def __init__(self, adapter: Any, *, cwd: str | Path, timeout_sec: float = 180) -> None:
        self.adapter = adapter
        self.cwd = str(Path(cwd).resolve())
        self.timeout_sec = float(timeout_sec)

    def plan(self, goal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        request = build_planning_request(goal_snapshot)
        _raw, parsed = self.adapter.decide(
            request,
            schema=planning_response_schema(),
            cwd=self.cwd,
            timeout_sec=self.timeout_sec,
        )
        parsed = unwrap_structured(parsed)
        if isinstance(parsed, Mapping) and parsed.get("_lead_status"):
            raise AppError(
                f"lead planning failed: {parsed.get('error') or parsed.get('_lead_status')}",
                status=503,
                code="lead_unavailable",
            )
        return validate_plan(parsed, request)

    def decide_action(
        self,
        goal_snapshot: Mapping[str, Any],
        task: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> dict[str, str]:
        """Ask the same pluggable lead about routine worker gates."""
        from lead_adapter.schema import (
            build_lead_request,
            lead_permission_response_schema,
            lead_review_response_schema,
            validate_lead_decision,
        )

        backend_kind = str(action.get("kind") or "")
        if backend_kind not in {"permission", "review"}:
            raise AppError("decision requires external authority", code="external_decision_required")
        goal = goal_snapshot.get("goal") if isinstance(goal_snapshot.get("goal"), Mapping) else {}
        boundaries = goal.get("boundaries") if isinstance(goal.get("boundaries"), Mapping) else {}
        instruction = str((task.get("inputs") or {}).get("instruction") or task.get("title") or "").strip()
        siblings = [row for row in (goal_snapshot.get("tasks") or []) if isinstance(row, Mapping)]
        goal_must = [str(x) for x in (boundaries.get("must") or [])]
        scoped_must, deferred_must = split_task_musts(musts=goal_must, task=task, siblings=siblings)
        task_accept = task_acceptance_criteria(task)
        extra = {
            "worker_request": action.get("payload") or {},
            "review_scope": "task",
            "task_acceptance": task_accept,
            "goal_acceptance": dict(goal.get("acceptance") or {}),
            "goal_must": goal_must,
            "deferred_must": deferred_must,
            "current_task": {
                "task_id": task.get("task_id"),
                "title": task.get("title"),
                "expected_artifacts": list(task.get("expected_artifacts") or []),
                "instruction": instruction,
            },
            "sibling_tasks": [
                {
                    "task_id": row.get("task_id"),
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "expected_artifacts": list(row.get("expected_artifacts") or []),
                }
                for row in siblings
            ],
        }
        if backend_kind == "review":
            extra["allow_hint"] = TASK_REVIEW_HINT
        request = build_lead_request(
            kind=backend_kind,
            goal=instruction or str(goal.get("desired_outcome") or ""),
            authorized_scope=scoped_must,
            prohibitions=list(boundaries.get("must_not") or []),
            acceptance_criteria=task_accept if backend_kind == "review" else dict(goal.get("acceptance") or {}),
            current_application={
                "goal_id": goal_snapshot.get("goal_id"),
                "task_id": task.get("task_id"),
                "run_id": task.get("run_id"),
                "review_scope": "task",
            },
            extra=extra,
        )
        schema = lead_permission_response_schema() if backend_kind == "permission" else lead_review_response_schema()
        raw, parsed = self.adapter.decide(
            request,
            schema=schema,
            cwd=self.cwd,
            timeout_sec=self.timeout_sec,
        )
        decision = validate_lead_decision(raw, parsed, request=request, kind=backend_kind)
        verdict = str(decision.get("decision") or decision.get("verdict") or "")
        return {"verdict": verdict, "reason": str(decision.get("reason") or "")}


def build_planning_request(goal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    goal = goal_snapshot.get("goal") if isinstance(goal_snapshot.get("goal"), Mapping) else {}
    boundaries = goal.get("boundaries") if isinstance(goal.get("boundaries"), Mapping) else {}
    body = {
        "protocol": "collab-plan-v1",
        "kind": "plan",
        "application_id": f"plan_{goal_snapshot.get('goal_id') or uuid.uuid4().hex}",
        "task_goal": str(goal.get("desired_outcome") or ""),
        "authorized_scope": list(boundaries.get("must") or []),
        "prohibitions": list(boundaries.get("must_not") or []),
        "acceptance_criteria": dict(goal.get("acceptance") or {}),
        "budget": dict(goal.get("budget") or {}),
        "max_tasks": MAX_PLAN_TASKS,
        "current_application": {"goal_id": goal_snapshot.get("goal_id"), "task_count": 0},
        "issued_at": time.time(),
    }
    body["context_summary"] = context_summary_of(body)
    return body


class AppCoordinator:
    """Plan Goals and dispatch ready Tasks through one ExecutionBackend."""

    def __init__(
        self,
        layer: DurableLayer,
        *,
        planner: GoalPlanner | None = None,
        backend: ExecutionBackend | None = None,
        workspaces_root: str | Path,
    ) -> None:
        self.layer = layer
        self.planner = planner or DeterministicPlanner()
        self.backend = backend or InProcessExecutionBackend()
        self.workspaces_root = Path(workspaces_root)
        # HTTP ticks and the background loop may fire together.  Serialize
        # coordination so a queued Task is dispatched at most once by this
        # service instance.
        self._lock = threading.RLock()

    def process_all(self) -> dict[str, Any]:
        with self._lock:
            rows = self.layer.list_goals().get("goals") or []
            outcomes = [self._process_goal(str(row.get("goal_id") or "")) for row in rows]
            return {"ok": all(x.get("ok") for x in outcomes), "processed": outcomes}

    def process_goal(self, goal_id: str) -> dict[str, Any]:
        with self._lock:
            return self._process_goal(goal_id)

    def _process_goal(self, goal_id: str) -> dict[str, Any]:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            return current
        snap = current["goal"]
        if snap.get("state") == "cancel_requested":
            active = [
                t
                for t in (snap.get("tasks") or [])
                if isinstance(t, Mapping) and str(t.get("run_id") or "").strip()
                and t.get("status") in {"running", "cancel_requested", "awaiting_decision", "review"}
            ]
            if not active:
                effected = self.layer.effect_cancel(goal_id, reason="No worker Run remains active")
                return {**effected, "action": "cancelled"}
            all_stopped = True
            outcomes = []
            for task in active:
                run_id = str(task.get("run_id") or "")
                try:
                    code, body = self.backend.cancel(run_id)
                    stopped = int(code) < 300 and isinstance(body, Mapping) and bool(body.get("ok"))
                except Exception as e:
                    code, body, stopped = 503, {"error": type(e).__name__}, False
                outcomes.append({"run_id": run_id, "http": code, "stopped": stopped})
                all_stopped = all_stopped and stopped
            if all_stopped:
                effected = self.layer.effect_cancel(goal_id, reason="Worker cancellation confirmed")
                return {**effected, "action": "cancelled", "backend_cancellations": outcomes}
            return {
                "ok": True,
                "goal_id": goal_id,
                "state": "cancel_requested",
                "action": "cancellation_pending",
                "backend_cancellations": outcomes,
            }
        if snap.get("state") in {"completed", "failed", "cancelled"}:
            return {"ok": True, "goal_id": goal_id, "state": snap.get("state"), "action": "terminal"}
        tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        if not tasks:
            try:
                plan = self.planner.plan(snap)
            except Exception as e:
                detail = f"planner failed: {type(e).__name__}"
                if isinstance(e, AppError):
                    detail = f"planner failed [{e.code}]: {str(e)[:300]}"
                failed = self.layer.fail_goal(
                    goal_id,
                    phase="planning",
                    error=detail,
                )
                return {**failed, "action": "planning_failed"}
            key_to_id = {row["task_key"]: _task_id(goal_id, row["task_key"]) for row in plan["tasks"]}
            for row in plan["tasks"]:
                added = self.layer.add_child_task(
                    goal_id,
                    task={
                        "task_id": key_to_id[row["task_key"]],
                        "title": row["title"],
                        "status": "queued",
                        "depends_on": [key_to_id[x] for x in row["depends_on"]],
                        "inputs": {
                            "instruction": row["instruction"],
                            "planner": self.planner.name,
                        },
                        "expected_artifacts": row["artifacts"],
                        "done_when": {"artifacts": row["artifacts"]},
                        "assignee_role": "executor",
                        "backend_requirement": getattr(self.backend, "backend_id", ""),
                    },
                )
                if not added.get("ok"):
                    return added
            current = self.layer.get_goal(goal_id)
            snap = current["goal"]
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]

        # A real worker backend is normally asynchronous.  Resume polling a
        # persisted Run before considering new queued work.
        for task in tasks:
            if task.get("status") == "awaiting_decision":
                return {
                    "ok": True,
                    "goal_id": goal_id,
                    "state": "running",
                    "action": "decision_required",
                }
            if task.get("status") != "running":
                continue
            existing_decision = next(
                (
                    d
                    for d in (snap.get("pending_decisions") or [])
                    if isinstance(d, Mapping) and str(d.get("task_id") or "") == str(task.get("task_id") or "")
                ),
                None,
            )
            if existing_decision is not None:
                return self._continue_pending_decision(snap, task, existing_decision)
            run_id = str(task.get("run_id") or "").strip()
            if not run_id:
                return self.layer.finish_task(
                    goal_id,
                    str(task["task_id"]),
                    succeeded=False,
                    result={"ok": False, "error": "running task has no persisted run handle"},
                )
            try:
                pending_code, pending = self.backend.list_pending_actions(session_id=run_id)
            except Exception as e:
                pending_code, pending = 503, []
                pending_error = f"pending action scan failed: {type(e).__name__}"
            else:
                pending_error = ""
            if int(pending_code) >= 300:
                return self.layer.finish_task(
                    goal_id,
                    str(task["task_id"]),
                    succeeded=False,
                    result={"ok": False, "run_id": run_id, "error": pending_error or "pending action scan failed"},
                )
            if pending:
                action: Mapping[str, Any] = {}
                for candidate in pending:
                    if not isinstance(candidate, Mapping):
                        continue
                    kind = str(candidate.get("kind") or "").strip()
                    if kind in PROJECTABLE_BACKEND_KINDS:
                        action = candidate
                        break
                if not action:
                    return {
                        "ok": True,
                        "goal_id": goal_id,
                        "state": "running",
                        "action": "backend_gate_unprojected",
                        "pending_kinds": [
                            str(row.get("kind") or "")
                            for row in pending
                            if isinstance(row, Mapping)
                        ],
                    }
                request_id = str(action.get("request_id") or "").strip()
                if not request_id:
                    return self.layer.finish_task(
                        goal_id,
                        str(task["task_id"]),
                        succeeded=False,
                        result={"ok": False, "run_id": run_id, "error": "backend decision has no request_id"},
                    )
                backend_kind = str(action.get("kind") or "permission")
                public_kind = {
                    "permission": "action_approval",
                    "question": "question",
                    "review": "artifact_review",
                    "system_action": "system_action_approval",
                }[backend_kind]
                decision_id = f"dec_{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:12]}"
                opened = self.layer.open_decision(
                    goal_id,
                    kind=public_kind,
                    task_id=str(task["task_id"]),
                    run_id=run_id,
                    decision_id=decision_id,
                    request_id=request_id,
                    actions=[{"request_id": request_id, "kind": backend_kind}],
                    title=f"TeleAgent {backend_kind}",
                    return_to_upper=False,
                    details={
                        "backend_kind": backend_kind,
                        "backend_request_id": request_id,
                        "context_hash": action.get("context_hash"),
                        "payload": action.get("payload"),
                    },
                    reason="TeleAgent worker requires a bounded decision",
                )
                if opened.get("ok"):
                    rec = opened.get("decision") if isinstance(opened.get("decision"), Mapping) else {}
                    return self._lead_resolve_opened(
                        snap,
                        task,
                        decision=rec or {"decision_id": decision_id, "request_id": request_id},
                        action=action,
                    )
                return {**opened, "action": "decision_required"}
            try:
                observation = self.backend.observe_run(run_id)
                if observation.get("busy"):
                    return {"ok": True, "goal_id": goal_id, "state": "running", "action": "worker_running"}
                result = self.backend.collect_result(run_id)
            except Exception as e:
                result = {
                    "ok": False,
                    "run_id": run_id,
                    "error": f"backend resume failed: {type(e).__name__}",
                }
            finished = self.layer.finish_task(
                goal_id,
                str(task["task_id"]),
                succeeded=bool(result.get("ok")),
                result=result,
            )
            return {**finished, "action": "task_finished"}

        status_by_id = {str(t.get("task_id")): str(t.get("status")) for t in tasks}
        for task in tasks:
            if task.get("status") != "queued":
                continue
            deps = [str(x) for x in (task.get("depends_on") or [])]
            if any(status_by_id.get(dep) == "failed" for dep in deps):
                continue
            if any(status_by_id.get(dep) != "succeeded" for dep in deps):
                continue
            started = self.layer.start_task(goal_id, str(task["task_id"]))
            if not started.get("ok"):
                return started
            root = self.workspaces_root / goal_id / str(task["task_id"])
            root.mkdir(parents=True, exist_ok=True)
            try:
                staged = handoff_direct_dependency_artifacts(
                    task=task,
                    siblings=tasks,
                    dest_root=root,
                )
            except HandoffError as e:
                return self.layer.finish_task(
                    goal_id,
                    str(task["task_id"]),
                    succeeded=False,
                    result={"ok": False, "error": f"dependency handoff failed: {e}"},
                )
            if staged:
                inputs = dict(task.get("inputs") or {})
                inputs["input_files"] = [row["relative"] for row in staged]
                task["inputs"] = inputs
            try:
                launched = self.backend.start_run(
                    title=str(task.get("title") or task["task_id"]),
                    directory=str(root),
                    instruction=str((task.get("inputs") or {}).get("instruction") or ""),
                    artifacts=[str(x) for x in (task.get("expected_artifacts") or [])],
                    charter=worker_charter_for_task(
                        goal=snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {},
                        task=task,
                    ),
                )
            except Exception as e:
                launched = {"ok": False, "error": f"backend dispatch failed: {type(e).__name__}"}
            run_id = str(launched.get("run_id") or launched.get("native_handle") or "")
            if not launched.get("ok") or not run_id:
                return self.layer.finish_task(goal_id, str(task["task_id"]), succeeded=False, result=launched)
            bound = self.layer.bind_task_run(
                goal_id,
                str(task["task_id"]),
                run_id=run_id,
                native_handle=str(launched.get("native_handle") or ""),
                backend=str(launched.get("backend") or getattr(self.backend, "backend_id", "")),
            )
            if not bound.get("ok"):
                # The Run may already exist, so fail closed and never launch a
                # replacement.  The returned binding error remains durable.
                return bound
            try:
                observation = self.backend.observe_run(run_id)
                if observation.get("busy"):
                    return {"ok": True, "goal_id": goal_id, "state": "running", "action": "worker_running"}
                result = self.backend.collect_result(run_id)
            except Exception as e:
                result = {
                    "ok": False,
                    "run_id": run_id,
                    "error": f"backend observation failed: {type(e).__name__}",
                }
            finished = self.layer.finish_task(
                goal_id,
                str(task["task_id"]),
                succeeded=bool(result.get("ok")),
                result=result,
            )
            return {**finished, "action": "task_finished"}
        return {"ok": True, "goal_id": goal_id, "state": snap.get("state"), "action": "waiting"}

    def _continue_pending_decision(
        self,
        snap: Mapping[str, Any],
        task: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        details = decision.get("details") if isinstance(decision.get("details"), Mapping) else {}
        backend_kind = str(details.get("backend_kind") or "")
        lead_decider = getattr(self.planner, "decide_action", None)
        backend_resolver = getattr(self.backend, "resolve_decision", None)
        if not (
            callable(lead_decider)
            and callable(backend_resolver)
            and backend_kind in AUTO_RESOLVE_BACKEND_KINDS
        ):
            return {
                "ok": True,
                "goal_id": snap.get("goal_id"),
                "state": "running",
                "action": "decision_required",
                "decision_id": decision.get("decision_id"),
            }
        err = details.get("lead_error") if isinstance(details.get("lead_error"), Mapping) else {}
        if not err and isinstance(decision.get("lead_error"), Mapping):
            err = dict(decision.get("lead_error") or {})
        attempts = int(details.get("lead_attempts") or 0)
        if attempts >= MAX_LEAD_ATTEMPTS_PER_DECISION or (attempts >= 1 and not err.get("retryable", False)):
            return {
                "ok": True,
                "goal_id": snap.get("goal_id"),
                "state": "running",
                "action": "decision_required",
                "decision_id": decision.get("decision_id"),
                "lead_error": err or {"code": "lead_exhausted", "retryable": False},
            }
        action = {
            "kind": backend_kind,
            "request_id": str(details.get("backend_request_id") or decision.get("request_id") or ""),
            "payload": details.get("payload") or {},
            "context_hash": details.get("context_hash"),
        }
        return self._lead_resolve_opened(snap, task, decision=decision, action=action)

    def _lead_resolve_opened(
        self,
        snap: Mapping[str, Any],
        task: Mapping[str, Any],
        *,
        decision: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        lead_decider = getattr(self.planner, "decide_action", None)
        backend_resolver = getattr(self.backend, "resolve_decision", None)
        backend_kind = str(action.get("kind") or "")
        decision_id = str(decision.get("decision_id") or "")
        request_id = str(action.get("request_id") or decision.get("request_id") or "")
        details = decision.get("details") if isinstance(decision.get("details"), Mapping) else {}
        if not (
            callable(lead_decider)
            and callable(backend_resolver)
            and backend_kind in AUTO_RESOLVE_BACKEND_KINDS
        ):
            return {
                "ok": True,
                "goal_id": snap.get("goal_id"),
                "state": "running",
                "action": "decision_required",
                "decision_id": decision_id,
            }
        try:
            lead_choice = lead_decider(snap, task, action)
            lead_verdict = str(lead_choice.get("verdict") or "")
            lead_reason = str(lead_choice.get("reason") or "")
            transport = backend_resolver(
                request_id,
                verdict=lead_verdict,
                reason=lead_reason,
                answers=None,
            )
            if not isinstance(transport, Mapping) or not transport.get("ok"):
                raise AppError("worker decision was not applied", code="worker_decision_failed")
            durable_verdict = (
                "reject"
                if lead_verdict in {"deny_job", "demand_safe_path"}
                else lead_verdict
            )
            resolved = self.layer.resolve_decision(
                str(snap.get("goal_id") or ""),
                decision_id=decision_id,
                verdict=durable_verdict,
                reason=lead_reason,
                extra={
                    "actor_id": str(
                        ((snap.get("ownership") or {}).get("coordinator_id"))
                        if isinstance(snap.get("ownership"), Mapping)
                        else ""
                    )
                    or "app-coordinator"
                },
            )
            if not resolved.get("ok"):
                return resolved
            return {**resolved, "action": "decision_resolved", "lead": self.planner.name}
        except Exception as e:
            rec = _lead_error_record(e)
            attempts = int(details.get("lead_attempts") or 0) + 1
            rec["attempts"] = attempts
            if decision_id:
                self.layer.annotate_decision(
                    str(snap.get("goal_id") or ""),
                    decision_id=decision_id,
                    details={
                        "lead_error": rec,
                        "lead_attempts": attempts,
                        "lead_attempt_at": time.time(),
                    },
                    lead_error=rec,
                )
            return {
                "ok": True,
                "goal_id": snap.get("goal_id"),
                "state": "running",
                "action": "decision_required",
                "decision_id": decision_id,
                "lead_error": rec,
            }



def probe_win_gui_connection(
    *,
    simulated: bool = False,
    simulate_status: str | None = None,
    port_open_fn: Callable[[str, int], bool] | None = None,
    creds_presence_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Fail-closed doctor probe against desktop GUI TeleAgent ports (4399/4397/4398).

    Never defaults to stdin_wrap. Uncertain / non-ok statuses are not success.
    """
    from teleagent_adapter.base import AdapterStatus
    from teleagent_adapter.doctor import doctor

    report = doctor(
        base_url="http://127.0.0.1:4399",
        platform="win32",
        simulated=simulated,
        simulate_status=simulate_status,
        port_open_fn=port_open_fn,
        creds_presence_fn=creds_presence_fn,
    )
    status = str(report.status or "")
    ok = status == AdapterStatus.OK.value
    detail = "; ".join(str(x) for x in (report.details or [])[:3])
    reason = sanitize_reason(detail or status or "connection_not_ready")
    return {
        "ok": ok,
        "status": status,
        "reason": reason if not ok else "",
        "base_url": report.base_url,
        "simulated": bool(report.simulated),
    }



def public_pending_decisions(rows: Any) -> list[dict[str, Any]]:
    """Stable public shape for status/events: kind includes system_action_approval."""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        out.append(
            {
                "decision_id": str(row.get("decision_id") or ""),
                "request_id": str(row.get("request_id") or ""),
                "kind": str(row.get("kind") or ""),
                "title": str(row.get("title") or ""),
                "task_id": str(row.get("task_id") or ""),
                "run_id": str(row.get("run_id") or ""),
                "status": str(row.get("status") or ""),
                "backend_kind": str(details.get("backend_kind") or ""),
                "backend_request_id": str(details.get("backend_request_id") or ""),
                "details": dict(details),
            }
        )
    return out


class CollabApplication:
    def __init__(
        self,
        persist_root: str | Path,
        *,
        planner: GoalPlanner | None = None,
        backend: ExecutionBackend | None = None,
        coordinator_id: str = "app-coordinator",
        connection_probe: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.persist_root = Path(persist_root)
        self.layer = DurableLayer.open(self.persist_root, use_cache=False)
        self.coordinator_id = coordinator_id
        self.coordinator = AppCoordinator(
            self.layer,
            planner=planner,
            backend=backend,
            workspaces_root=self.persist_root / "workspaces",
        )
        self._connection_probe = connection_probe

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AppError("request body must be a JSON object")
        goal_text = str(payload.get("goal") or payload.get("desired_outcome") or "").strip()
        if not goal_text:
            raise AppError("goal is required")
        acceptance = payload.get("acceptance")
        if not isinstance(acceptance, Mapping):
            raise AppError("acceptance object is required")
        acceptance_obj = dict(acceptance)
        acceptance_obj["artifacts"] = _safe_artifacts(acceptance.get("artifacts"))
        boundaries = payload.get("boundaries") if isinstance(payload.get("boundaries"), Mapping) else {}
        must = _strings(boundaries.get("must") or payload.get("must") or [], field="boundaries.must")
        must_not = _strings(boundaries.get("must_not") or payload.get("must_not") or [], field="boundaries.must_not")
        must_not = must_not or [
            "Do not access credentials or account secrets",
            "Do not modify system settings or install software",
            "Do not write outside the assigned task workspace",
        ]
        budget = payload.get("budget") if isinstance(payload.get("budget"), Mapping) else {"wall_sec": 300, "max_reworks": 1}
        submit_key = str(payload.get("idempotency_key") or payload.get("request_id") or "").strip()
        if not submit_key:
            submit_key = f"req_{uuid.uuid4().hex}"
        client_id = str(payload.get("client_id") or "local-api").strip() or "local-api"
        title = str(payload.get("title") or goal_text[:80]).strip()
        forbidden_tools = project_forbidden_tools(payload=payload)
        goal = {
            "title": title,
            "desired_outcome": goal_text,
            "boundaries": {"must": must, "must_not": must_not},
            "acceptance": acceptance_obj,
            "budget": dict(budget),
            "platform_allowlist": [str(x) for x in (payload.get("platform_allowlist") or ["windows"])],
            "role_hints": {"entrypoint": API_VERSION},
        }
        if forbidden_tools:
            goal["forbidden_tools"] = forbidden_tools
        result = self.layer.submit_goal(
            submit_key=submit_key,
            title=title,
            desired_outcome=goal_text,
            goal=goal,
            submitter_id=f"api:{client_id}",
            external_goal_ref=str(payload.get("external_goal_ref") or submit_key),
            autonomy=payload.get("autonomy") or "bounded_autonomy",
            coordinator_id=self.coordinator_id,
        )
        if not result.get("ok"):
            status = 409 if result.get("reason") == "submit_content_conflict" else 400
            raise AppError(str(result.get("error") or result.get("reason")), status=status, code=str(result.get("reason") or "submit_failed"))
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": result["goal_id"],
            "goal_id": result["goal_id"],
            "state": result["goal"].get("state"),
            "created": bool(result.get("created")),
            "duplicate": bool(result.get("duplicate")),
            "status_url": f"/v1/requests/{result['goal_id']}",
        }

    def status(self, goal_id: str) -> dict[str, Any]:
        result = self.layer.get_goal(goal_id)
        if not result.get("ok"):
            raise AppError(str(result.get("error")), status=404, code="not_found")
        snap = result["goal"]
        tasks = snap.get("tasks") or []
        failure = snap.get("failure")
        nh_view = goal_need_human_view(
            failure=failure if isinstance(failure, Mapping) else None,
            tasks=tasks if isinstance(tasks, list) else [],
        )
        pending = public_pending_decisions(snap.get("pending_decisions") or [])
        out = {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "state": snap.get("state"),
            "goal": snap.get("goal"),
            "tasks": tasks,
            "pending_decisions": pending,
            "pending_decision_count": len(pending),
            "awaiting_decision": bool(pending),
            "failure": failure,
            "need_human": bool(nh_view.get("need_human")),
            "failure_reason": nh_view.get("failure_reason") or "",
            "updated_at": snap.get("updated_at_iso"),
        }
        return out

    def list_requests(self) -> dict[str, Any]:
        out = self.layer.list_goals()
        rows: list[dict[str, Any]] = []
        for row in out.get("goals") or []:
            if not isinstance(row, Mapping):
                continue
            pending_count = int(row.get("pending_count") or row.get("pending_decision_count") or 0)
            item = dict(row)
            item["pending_decision_count"] = pending_count
            item["awaiting_decision"] = pending_count > 0
            rows.append(item)
        return {
            "ok": True,
            "api_version": API_VERSION,
            "requests": rows,
            "awaiting_decision_count": sum(1 for r in rows if r.get("awaiting_decision")),
        }


    def list_decisions(self, goal_id: str) -> dict[str, Any]:
        """GET …/decisions: pending only, same public rows as status/events."""
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            raise AppError(str(current.get("error")), status=404, code="not_found")
        snap = current.get("goal") if isinstance(current.get("goal"), Mapping) else {}
        pending = public_pending_decisions(snap.get("pending_decisions") or [])
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "pending_decisions": pending,
            "pending_decision_count": len(pending),
            "awaiting_decision": bool(pending),
        }

    def get_decision(self, goal_id: str, decision_id: str) -> dict[str, Any]:
        """GET …/decisions/{decision_id}: one pending public row."""
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            raise AppError(str(current.get("error")), status=404, code="not_found")
        snap = current.get("goal") if isinstance(current.get("goal"), Mapping) else {}
        pending = public_pending_decisions(snap.get("pending_decisions") or [])
        want = str(decision_id or "")
        row = next((item for item in pending if str(item.get("decision_id") or "") == want), None)
        if row is None:
            raise AppError("decision not found", status=404, code="not_found")
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "decision": row,
            "pending_decision_count": 1,
            "awaiting_decision": True,
        }

    def events(self, goal_id: str) -> dict[str, Any]:
        out = self.layer.list_events(goal_id)
        if not out.get("ok"):
            raise AppError(str(out.get("error")), status=404, code="not_found")
        pending = public_pending_decisions(out.get("pending") or out.get("pending_decisions") or [])
        return {
            **out,
            "pending": pending,
            "pending_decisions": pending,
            "pending_decision_count": len(pending),
            "awaiting_decision": bool(pending),
        }

    def report(self, goal_id: str) -> dict[str, Any]:
        out = self.layer.get_report(goal_id)
        if not out.get("ok") and out.get("reason") == "unknown_goal":
            raise AppError(str(out.get("error")), status=404, code="not_found")
        return out

    def cancel(self, goal_id: str, reason: str = "") -> dict[str, Any]:
        out = self.layer.cancel_goal(goal_id, reason=reason)
        if not out.get("ok"):
            raise AppError(str(out.get("error") or out.get("reason")), status=409, code=str(out.get("reason")))
        # If no task remains in flight, cancellation can be confirmed immediately.
        # For asynchronous backends, confirm every backend cancellation before
        # changing cancel_requested into the terminal cancelled state.
        snap = self.layer.get_goal(goal_id).get("goal") or {}
        active = [
            t
            for t in (snap.get("tasks") or [])
            if isinstance(t, Mapping)
            and t.get("status") in {"running", "cancel_requested", "awaiting_decision", "review"}
        ]
        if not active:
            return self.layer.effect_cancel(goal_id, reason=reason)
        cancelled_runs: list[dict[str, Any]] = []
        all_stopped = True
        for task in active:
            run_id = str(task.get("run_id") or "").strip()
            if not run_id:
                all_stopped = False
                cancelled_runs.append({"ok": False, "task_id": task.get("task_id"), "error": "missing run_id"})
                continue
            try:
                code, body = self.coordinator.backend.cancel(run_id)
                ok = int(code) < 300 and isinstance(body, Mapping) and bool(body.get("ok"))
                cancelled_runs.append({"ok": ok, "task_id": task.get("task_id"), "run_id": run_id, "http": code})
                all_stopped = all_stopped and ok
            except Exception as e:
                all_stopped = False
                cancelled_runs.append(
                    {
                        "ok": False,
                        "task_id": task.get("task_id"),
                        "run_id": run_id,
                        "error": f"backend cancel failed: {type(e).__name__}",
                    }
                )
        if all_stopped:
            effected = self.layer.effect_cancel(goal_id, reason=reason)
            effected["backend_cancellations"] = cancelled_runs
            return effected
        return {**out, "backend_cancellations": cancelled_runs}


    def _probe_connection(self) -> dict[str, Any]:
        if self._connection_probe is not None:
            raw = self._connection_probe()
            if not isinstance(raw, Mapping):
                return {"ok": False, "status": "invalid_probe", "reason": "connection probe returned non-object"}
            ok = bool(raw.get("ok"))
            status = str(raw.get("status") or ("ok" if ok else "not_ready"))
            reason = sanitize_reason(str(raw.get("reason") or status))
            # Fail-closed: only explicit ok=True counts.
            ok_flag = bool(raw.get("ok")) is True
            status_ok = (not status) or status == "ok"
            ready = ok_flag and status_ok
            return {"ok": ready, "status": status or ("ok" if ready else "not_ready"), "reason": reason if not ready else ""}
        backend = self.coordinator.backend
        backend_id = str(getattr(backend, "backend_id", "") or "")
        # Non-TeleAgent backends (in-process fakes) need no GUI doctor.
        if "teleagent" not in backend_id and "windows" not in backend_id:
            return {"ok": True, "status": "ok", "reason": ""}
        # Prefer GUI doctor; never invent stdin_wrap readiness here.
        return probe_win_gui_connection()

    def retry(self, goal_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Controlled retry for terminal failed+need_human after a human fix.

        1) Gate: state=failed and need_human=true
        2) Doctor/connection probe (GUI ports) — 409 if not ready
        3) Prefer resume/observe when the prior run is still recoverable
        4) Else requeue only the failed Task (keep Goal id / budget / history)
        """
        del payload  # reserved; body currently unused
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            raise AppError(str(current.get("error") or "unknown request"), status=404, code="not_found")
        snap = current.get("goal") if isinstance(current.get("goal"), Mapping) else {}
        state = str(snap.get("state") or "")
        tasks = snap.get("tasks") if isinstance(snap.get("tasks"), list) else []
        failure = snap.get("failure") if isinstance(snap.get("failure"), Mapping) else None
        nh_view = goal_need_human_view(failure=failure, tasks=tasks)
        if state != "failed":
            raise AppError(
                f"retry only allowed for terminal failed requests (state={state!r})",
                status=409,
                code="retry_not_allowed",
            )
        if not nh_view.get("need_human"):
            raise AppError(
                "retry only allowed when need_human=true",
                status=409,
                code="not_need_human",
            )

        probe = self._probe_connection()
        if not probe.get("ok"):
            raise AppError(
                f"connection not ready for retry: {probe.get('reason') or probe.get('status') or 'not_ready'}",
                status=409,
                code="connection_not_ready",
            )

        # Locate failed need_human task + prior run_id for possible resume.
        preferred = str((failure or {}).get("task_id") or "")
        target = None
        for t in tasks:
            if not isinstance(t, Mapping):
                continue
            if preferred and str(t.get("task_id") or "") == preferred:
                target = t
                break
        if target is None:
            for t in tasks:
                if not isinstance(t, Mapping) or str(t.get("status") or "") != "failed":
                    continue
                result = t.get("result") if isinstance(t.get("result"), Mapping) else {}
                if result.get("need_human") is True or str(result.get("error") or "").lower().startswith("need_human:"):
                    target = t
                    break
        if target is None:
            for t in tasks:
                if isinstance(t, Mapping) and str(t.get("status") or "") == "failed":
                    target = t
                    break
        prior_run = str((target or {}).get("run_id") or "").strip()

        resume_mode = False
        if prior_run:
            observe = getattr(self.coordinator.backend, "observe_run", None)
            if callable(observe):
                try:
                    observation = observe(prior_run)
                except Exception:
                    # Uncertain observe → do not treat as recoverable; fall through to redispatch.
                    observation = None
                if isinstance(observation, Mapping):
                    busy = bool(observation.get("busy"))
                    errored = bool(observation.get("errored"))
                    still_nh = bool(observation.get("need_human"))
                    # Recoverable: still-alive session (busy) and not a fresh need_human terminal.
                    if busy and not still_nh and not errored:
                        resume_mode = True

        reopened = self.layer.retry_need_human(goal_id, resume=resume_mode)
        if not reopened.get("ok"):
            raise AppError(
                str(reopened.get("error") or reopened.get("reason")),
                status=409,
                code=str(reopened.get("reason") or "retry_failed"),
            )

        mode = str(reopened.get("mode") or "redispatch")
        tid = str(reopened.get("task_id") or "")
        if mode == "resume" and tid and prior_run:
            started = self.layer.start_task(goal_id, tid)
            if not started.get("ok"):
                raise AppError(
                    str(started.get("error") or started.get("reason")),
                    status=409,
                    code=str(started.get("reason") or "resume_start_failed"),
                )
            # Ensure run binding survives start_task.
            bound = self.layer.bind_task_run(
                goal_id,
                tid,
                run_id=prior_run,
                backend=str(getattr(self.coordinator.backend, "backend_id", "") or ""),
            )
            if not bound.get("ok"):
                # start_task may have left run_id; non-fatal if already bound.
                if bound.get("reason") != "run_binding_conflict":
                    raise AppError(
                        str(bound.get("error") or bound.get("reason")),
                        status=409,
                        code=str(bound.get("reason") or "resume_bind_failed"),
                    )
            tick = self.coordinator.process_goal(goal_id)
            return {
                "ok": True,
                "api_version": API_VERSION,
                "request_id": goal_id,
                "goal_id": goal_id,
                "state": (tick.get("state") or started.get("state") or "running"),
                "task_id": tid,
                "mode": "resume",
                "action": tick.get("action") or "resumed",
                "run_id": prior_run,
            }

        # Bounded redispatch: leave task queued; one coordinator tick starts it.
        tick = self.coordinator.process_goal(goal_id)
        st = self.layer.get_goal(goal_id).get("goal") or {}
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "goal_id": goal_id,
            "state": st.get("state") or tick.get("state") or "queued",
            "task_id": tid,
            "mode": "redispatch",
            "action": tick.get("action") or "requeued",
        }


    def resolve(self, goal_id: str, decision_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            raise AppError("unknown request", status=404, code="not_found")
        snap = current.get("goal") if isinstance(current.get("goal"), Mapping) else {}
        pending = [d for d in (snap.get("pending_decisions") or []) if isinstance(d, Mapping)]
        target = next((d for d in pending if str(d.get("decision_id") or "") == decision_id), None)
        if target is None:
            resolved = [d for d in (snap.get("resolved_decisions") or []) if isinstance(d, Mapping)]
            if not any(str(d.get("decision_id") or "") == decision_id for d in resolved):
                raise AppError("unknown decision", status=404, code="not_found")
        details = target.get("details") if isinstance(target, Mapping) and isinstance(target.get("details"), Mapping) else {}
        backend_request_id = str(details.get("backend_request_id") or "")
        raw_verdict = str(payload.get("verdict") or "")
        if backend_request_id:
            resolver = getattr(self.coordinator.backend, "resolve_decision", None)
            if not callable(resolver):
                raise AppError("worker backend cannot resolve this decision", status=409, code="unsupported")
            try:
                transport = resolver(
                    backend_request_id,
                    verdict=raw_verdict,
                    reason=str(payload.get("reason") or "Resolved through the application API"),
                    answers=payload.get("answers") if isinstance(payload.get("answers"), list) else None,
                )
            except Exception as e:
                raise AppError(
                    f"worker decision failed: {type(e).__name__}",
                    status=409,
                    code="worker_decision_failed",
                ) from e
            if not isinstance(transport, Mapping) or not transport.get("ok"):
                raise AppError("worker decision was not applied", status=409, code="worker_decision_failed")
        actor = (
            str(current.get("submitter_id") or "")
            if isinstance(target, Mapping) and target.get("return_to_upper")
            else self.coordinator_id
        )
        backend_kind = str(details.get("backend_kind") or "")
        if backend_kind == "question" and raw_verdict == "answer":
            durable_verdict = "approve"
        elif raw_verdict in {"deny_job", "demand_safe_path"}:
            durable_verdict = "reject"
        else:
            durable_verdict = raw_verdict
        out = self.layer.resolve_decision(
            goal_id,
            decision_id=decision_id,
            verdict=durable_verdict,
            reason=str(payload.get("reason") or ""),
            actions=payload.get("actions"),
            extra={
                "actor_id": actor,
                "grant": payload.get("grant") if isinstance(payload.get("grant"), Mapping) else {},
            },
        )
        if not out.get("ok"):
            raise AppError(str(out.get("error") or out.get("reason")), status=409, code=str(out.get("reason")))
        # External decision applied to the worker — resume coordination without waiting for a separate tick.
        try:
            tick = self.coordinator.process_goal(goal_id)
        except Exception as e:
            tick = {"ok": False, "action": "tick_failed", "error": type(e).__name__}
        if isinstance(out, dict):
            out = {**out, "tick": tick if isinstance(tick, Mapping) else {"ok": False, "action": "tick_failed"}}
        return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "CollabApp/0.1"

    @property
    def app(self) -> CollabApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "api_token", "") or "")  # type: ignore[attr-defined]
        if not token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as e:
            raise AppError("invalid Content-Length") from e
        if length <= 0 or length > MAX_BODY_BYTES:
            raise AppError("JSON body required and must be <= 1 MiB", status=413, code="body_size")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            raise AppError(f"invalid JSON: {e}") from e
        if not isinstance(value, dict):
            raise AppError("JSON body must be an object")
        return value

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _run(self) -> None:
        if not self._authorized() and self.path != "/health":
            self._send(401, {"ok": False, "error": "unauthorized", "code": "unauthorized"})
            return
        parsed = urlparse(self.path)
        # Goal ids may contain a Unicode title slug.  HTTP clients percent-
        # encode it, so decode each already-separated path segment before
        # looking up durable ids.
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if self.command == "GET" and parsed.path == "/health":
            self._send(200, {"ok": True, "api_version": API_VERSION})
            return
        if parts == ["v1", "requests"]:
            if self.command == "POST":
                self._send(202, self.app.submit(self._json_body()))
            elif self.command == "GET":
                self._send(200, self.app.list_requests())
            else:
                raise AppError("method not allowed", status=405, code="method_not_allowed")
            return
        if len(parts) >= 3 and parts[:2] == ["v1", "requests"]:
            gid = parts[2]
            if len(parts) == 3 and self.command == "GET":
                self._send(200, self.app.status(gid))
                return
            if len(parts) == 4 and parts[3] == "events" and self.command == "GET":
                self._send(200, self.app.events(gid))
                return
            if len(parts) == 4 and parts[3] == "report" and self.command == "GET":
                self._send(200, self.app.report(gid))
                return
            if len(parts) == 4 and parts[3] == "cancel" and self.command == "POST":
                body = self._json_body()
                self._send(200, self.app.cancel(gid, str(body.get("reason") or "")))
                return
            if len(parts) == 4 and parts[3] == "retry" and self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    length = 0
                body = self._json_body() if length > 0 else {}
                self._send(200, self.app.retry(gid, body))
                return
            if len(parts) == 4 and parts[3] == "decisions" and self.command == "GET":
                self._send(200, self.app.list_decisions(gid))
                return
            if len(parts) == 5 and parts[3] == "decisions" and self.command == "GET":
                self._send(200, self.app.get_decision(gid, parts[4]))
                return
            if len(parts) == 5 and parts[3] == "decisions" and self.command == "POST":
                self._send(200, self.app.resolve(gid, parts[4], self._json_body()))
                return
        if parts == ["v1", "coordinator", "tick"] and self.command == "POST":
            self._send(200, self.app.coordinator.process_all())
            return
        raise AppError("route not found", status=404, code="not_found")

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._run()
        except AppError as e:
            self._send(e.status, {"ok": False, "code": e.code, "error": str(e)})
        except Exception as e:  # fail closed without a traceback/body leak
            self._send(500, {"ok": False, "code": "internal_error", "error": type(e).__name__})

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()


class CollabHttpServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: CollabApplication, *, api_token: str = "") -> None:
        host = address[0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise AppError("v0.1 application API only binds loopback", code="non_loopback_refused")
        super().__init__(address, _Handler)
        self.app = app
        self.api_token = api_token


class CoordinatorLoop:
    def __init__(self, app: CollabApplication, *, interval: float = 0.5) -> None:
        self.app = app
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="collab-coordinator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.app.coordinator.process_all()
            except Exception:
                # Goal stays durable and will be retried on the next tick.
                continue


__all__ = [
    "API_VERSION",
    "AppCoordinator",
    "AppError",
    "CollabApplication",
    "CollabHttpServer",
    "CoordinatorLoop",
    "DeterministicPlanner",
    "GoalPlanner",
    "LeadAdapterPlanner",
    "build_planning_request",
    "planning_response_schema",
    "project_forbidden_tools",
    "validate_plan",
    "worker_charter_for_task",
    "task_acceptance_criteria",
    "split_task_musts",
    "lead_error_retryable",
    "TASK_REVIEW_HINT",
    "GOAL_HTTP_TERMINAL",
    "TASK_HTTP_TERMINAL",
]
