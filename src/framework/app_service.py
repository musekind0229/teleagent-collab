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
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlparse

from execution_backend.base import (
    REQUIRED_CAPABILITY_NAMES,
    SKIP_PERMISSIONS_WARNING,
    BackendError,
    ExecutionBackend,
    default_capabilities,
    unmet_capabilities,
)
from execution_backend.inprocess_v1 import InProcessExecutionBackend
from framework import contract_render
from framework.artifact_contamination import scan_file, summarize
from framework.artifact_handoff import (
    HandoffError,
    handoff_direct_dependency_artifacts,
    scheduler_task_workspace,
    workspace_paths_match,
)
from framework.input_manifest import (
    InputManifestError,
    capability_document,
    merge_manifest_input_files,
    project_input_manifest,
    snapshot_metadata,
)
from framework.failure_projection import goal_level_failure_brief, project_failed_tasks
from framework.need_human import goal_need_human_view, sanitize_reason
from framework.concurrency import (
    IN_FLIGHT,
    occupies_run_slot,
    as_limit,
    budget_block_reason,
    directory_key,
    dispatch_directory,
    global_decision_blocks,
    public_concurrency,
    remaining_slots,
    scheduler_view,
    task_is_ready,
)
from framework.delegation import KIND_CHECKPOINT
from framework.durable_api import DurableLayer
from framework.progress_budget import (
    BudgetValidationError,
    artifact_checkpoint,
    bound_task_progress,
    budget_mode,
    budget_of,
    budget_submit_issues,
    derive_goal_progress,
    enforcement_level,
    format_seconds,
    normalize_budget,
    on_no_progress,
    parse_iso,
    token_total,
    tool_call_total,
    usage_fields,
    usage_report,
    utc_iso,
)
from lead_adapter.schema import context_summary_of, unwrap_structured

API_VERSION = "collab-app.v0.1"
MAX_BODY_BYTES = 1024 * 1024
MAX_PLAN_TASKS = 8


class AppError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str = "invalid_request",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = int(status)
        self.code = code
        # Merged into the HTTP body by _Handler. ok/code/error stay authoritative.
        self.extra = dict(extra) if isinstance(extra, Mapping) else {}
        super().__init__(message)


_ERROR_BODY_RESERVED = frozenset({"ok", "code", "error"})
_CONTAMINATION_MESSAGE = (
    "Fail this review so the worker can redo it, or re-open the request "
    "with acceptance.allow_aigc_marks=true if the marks are intended."
)


def _count_map(value: Any) -> dict[str, int]:
    """Copy integer counts only. Drop strings so file text cannot ride along."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for key, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        out[str(key)] = count
    return out


def _public_contamination(findings: Any) -> dict[str, dict[str, Any]]:
    """``{name: {aigc_marks, invisible, encoding}}``. Counts and encoding only."""
    if not isinstance(findings, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, scan in findings.items():
        if not isinstance(scan, Mapping):
            continue
        encoding = scan.get("encoding")
        if (
            not isinstance(encoding, str)
            or len(encoding) > 32
            or any(ord(ch) < 32 for ch in encoding)
        ):
            encoding = ""
        out[str(name)] = {
            "aigc_marks": _count_map(scan.get("aigc_marks")),
            "invisible": _count_map(scan.get("invisible")),
            "encoding": encoding,
        }
    return out


def _is_artifact_contaminated(exc: BaseException) -> bool:
    """Detect without importing win_collab (class name or ``findings``)."""
    if type(exc).__name__ == "ArtifactContaminatedError":
        return True
    return isinstance(getattr(exc, "findings", None), Mapping)


def _contamination_summary(exc: BaseException) -> str:
    summary = getattr(exc, "summary", None)
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    text = str(exc).strip()
    marker = "Artifact content contaminated (AIGC mark / invisible chars): "
    if text.startswith(marker):
        return text[len(marker) :].strip()
    return text


def _artifact_contaminated_error(exc: BaseException) -> AppError:
    summary = _contamination_summary(exc)
    return AppError(
        f"artifact contaminated: {summary}. {_CONTAMINATION_MESSAGE}",
        status=409,
        code="artifact_contaminated",
        extra={
            "contamination": _public_contamination(getattr(exc, "findings", None)),
            "hint": "allow_aigc_marks",
        },
    )


def _app_error_body(err: AppError) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "code": err.code, "error": str(err)}
    extra = getattr(err, "extra", None)
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            if str(key) in _ERROR_BODY_RESERVED:
                continue
            body[str(key)] = value
    return body


def planner_capability(planner: Any) -> dict[str, Any]:
    """Honest planner flags. Unknown planners do not claim decomposition or review."""
    name = str(getattr(planner, "name", "") or "unknown")
    if name == "deterministic.single_task":
        return {"name": name, "decomposes": False, "lead_review": False}
    if name == "lead_adapter.plan_v1" or name.startswith("lead_adapter"):
        return {"name": name, "decomposes": True, "lead_review": True}
    return {"name": name, "decomposes": False, "lead_review": False}


def compose_capabilities(
    backend: Any,
    planner: Any,
    *,
    max_parallel_per_goal: int = 2,
    max_parallel_global: int = 4,
) -> dict[str, Any]:
    """Backend document with this process's planner overlaid.

    ``acceptance.lead_review`` is true only when the backend can carry a lead
    review AND this planner actually reviews. Exact-content stays a backend fact.
    """
    raw_fn = getattr(backend, "capabilities", None)
    raw: Any
    if callable(raw_fn):
        try:
            raw = raw_fn()
        except Exception:
            raw = None
    else:
        raw = None
    if not isinstance(raw, dict):
        raw = default_capabilities(backend_id=str(getattr(backend, "backend_id", "unknown")))
    caps = json.loads(json.dumps(raw, default=str))
    caps["api_version"] = API_VERSION
    planner_doc = planner_capability(planner)
    caps["planner"] = planner_doc
    acceptance = caps.get("acceptance") if isinstance(caps.get("acceptance"), dict) else {}
    acceptance["lead_review"] = bool(acceptance.get("lead_review")) and bool(planner_doc["lead_review"])
    caps["acceptance"] = acceptance
    if not isinstance(caps.get("warnings"), list):
        caps["warnings"] = []
    caps["concurrency"] = public_concurrency(
        max_parallel_per_goal=max_parallel_per_goal,
        max_parallel_global=max_parallel_global,
        backend_caps=caps,
    )
    return caps


def parse_required_capabilities(payload: Mapping[str, Any]) -> list[str]:
    """Fixed vocabulary. Unknown names are ``invalid_request`` before any Goal exists."""
    if "required_capabilities" not in payload or payload.get("required_capabilities") is None:
        return []
    raw = payload.get("required_capabilities")
    if not isinstance(raw, list):
        raise AppError(
            "required_capabilities must be a list of capability names",
            code="invalid_request",
        )
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AppError(
                "required_capabilities must be a list of capability names",
                code="invalid_request",
            )
        name = item.strip()
        if name not in REQUIRED_CAPABILITY_NAMES:
            raise AppError(f"unknown capability {name!r}", code="invalid_request")
        if name not in found:
            found.append(name)
    return found


def budget_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Goal budget from a submit body.

    A missing key keeps today's default. An explicit null or non-object is
    ``invalid_request``. Unknown keys are kept. Bad types of known fields are
    ``invalid_request`` and are not coerced.
    """
    if "budget" not in payload:
        return {"wall_sec": 300, "max_reworks": 1}
    raw = payload.get("budget")
    if not isinstance(raw, Mapping):
        raise AppError("budget must be an object")
    try:
        return normalize_budget(raw)
    except BudgetValidationError as exc:
        raise AppError(str(exc)) from exc


def parse_acknowledge_prompt_only(payload: Mapping[str, Any]) -> bool:
    if "acknowledge_prompt_only_inputs" not in payload or payload.get("acknowledge_prompt_only_inputs") is None:
        return False
    value = payload.get("acknowledge_prompt_only_inputs")
    if not isinstance(value, bool):
        raise AppError(
            "acknowledge_prompt_only_inputs must be a boolean",
            code="invalid_request",
        )
    return value


def prompt_only_skip_blocked(caps: Mapping[str, Any]) -> bool:
    """External pins are prompt text while the backend auto-approves tool use."""
    external = caps.get("external_inputs") if isinstance(caps.get("external_inputs"), Mapping) else {}
    return external.get("enforcement") == "prompt_only" and caps.get("skip_permissions") is True


def _append_unique(out: list[str], seen: set[str], item: Any) -> None:
    if not isinstance(item, str):
        return
    text = item.strip()
    if not text or text in seen:
        return
    seen.add(text)
    out.append(text)


def goal_status_warnings(snap: Mapping[str, Any], caps: Mapping[str, Any]) -> list[str]:
    """Persisted goal/task warnings plus capability warnings that apply to this goal."""
    out: list[str] = []
    seen: set[str] = set()
    goal = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
    for item in goal.get("warnings") or []:
        _append_unique(out, seen, item)
    for task in snap.get("tasks") or []:
        if not isinstance(task, Mapping):
            continue
        result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
        for item in result.get("warnings") or []:
            _append_unique(out, seen, item)
        review = result.get("review") if isinstance(result.get("review"), Mapping) else {}
        if review.get("status") == "unsupported":
            from execution_backend.antigravity_cli_v1 import ACCEPTANCE_UNVERIFIED_WARNING

            _append_unique(out, seen, ACCEPTANCE_UNVERIFIED_WARNING)
    for item in caps.get("warnings") or []:
        _append_unique(out, seen, item)
    return out


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
    """Relative workspace paths only.

    Backslash and drive-letter forms are rejected on Linux as well as Windows.
    ``pathlib`` on POSIX does not treat ``\\\\`` as a separator, so ``..\\\\x``
    would otherwise look like a single filename.
    """
    artifacts = _strings(value, field="acceptance.artifacts", required=True)
    out: list[str] = []
    for raw in artifacts:
        try:
            out.append(contract_render.safe_relative_artifact(raw))
        except contract_render.ContractRenderError as exc:
            raise AppError(
                f"artifact must be a relative path inside the task workspace: {raw!r}"
            ) from exc
    return out


def _task_id(goal_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{goal_id}\0{key}".encode("utf-8")).hexdigest()[:16]
    return f"task_{digest}"


GOAL_HTTP_TERMINAL = frozenset({"completed", "failed", "cancelled"})
TASK_HTTP_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MAX_FORBIDDEN_TOOLS = 32
MAX_EXTERNAL_INPUTS = 8
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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


def project_external_inputs(payload: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return Goal.external_inputs after shape validation.

    Absent or null means no pins. Each item is an absolute ``path`` and a
    64-hex ``sha256``, at most eight. Optional ``metadata`` is only the closed
    snapshot record (kind, basename source, taken_at). File-inside-repo, hash
    match, link, and size checks stay in ``win_collab.validate_charter``.
    """
    src = payload if isinstance(payload, Mapping) else {}
    if "external_inputs" not in src or src.get("external_inputs") is None:
        return []
    raw = src.get("external_inputs")
    if not isinstance(raw, list):
        raise AppError(
            "external_inputs must be a list of pinned files",
            code="invalid_external_inputs",
        )
    if len(raw) > MAX_EXTERNAL_INPUTS:
        raise AppError(
            f"external_inputs must have at most {MAX_EXTERNAL_INPUTS} entries",
            code="invalid_external_inputs",
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise AppError(
                "each external input requires only path and sha256",
                code="invalid_external_inputs",
            )
        extra = set(item) - {"path", "sha256", "metadata"}
        if extra or not {"path", "sha256"} <= set(item):
            raise AppError(
                "each external input requires only path and sha256",
                code="invalid_external_inputs",
            )
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise AppError(
                "external input path and sha256 must be strings",
                code="invalid_external_inputs",
            )
        path = path.strip()
        digest = digest.strip()
        if not path or not Path(path).is_absolute():
            raise AppError(
                "external input path must be absolute",
                code="invalid_external_inputs",
            )
        if _SHA256_RE.fullmatch(digest) is None:
            raise AppError(
                "external input sha256 must be 64 hex characters",
                code="invalid_external_inputs",
            )
        if path in seen:
            raise AppError(
                "external_inputs paths must be unique",
                code="invalid_external_inputs",
            )
        seen.add(path)
        metadata = None
        if "metadata" in item:
            try:
                metadata = snapshot_metadata(item.get("metadata"))
            except InputManifestError as exc:
                raise AppError(str(exc), code="invalid_external_inputs") from exc
        row: dict[str, Any] = {"path": path, "sha256": digest}
        if metadata is not None:
            row["metadata"] = metadata
        out.append(row)
    return out


def allows_aigc_marks(
    goal: Mapping[str, Any] | None,
    task: Mapping[str, Any] | None = None,
) -> bool:
    """True only for a boolean opt-out of the artifact content check.

    The check stays on unless ``allow_aigc_marks`` is the boolean ``True`` on
    ``goal["acceptance"]`` or ``task["inputs"]``. The string ``"true"`` and
    other truthy values do not opt out. ``task_acceptance_criteria`` stays the
    artifact/text view of ``done_when``; this flag is not inferred from it.
    The scanner module does not read this switch.
    """
    goal_obj = goal if isinstance(goal, Mapping) else {}
    task_obj = task if isinstance(task, Mapping) else {}
    acceptance = goal_obj.get("acceptance") if isinstance(goal_obj.get("acceptance"), Mapping) else {}
    inputs = task_obj.get("inputs") if isinstance(task_obj.get("inputs"), Mapping) else {}
    return acceptance.get("allow_aigc_marks") is True or inputs.get("allow_aigc_marks") is True


_GOAL_EXACT_RE = re.compile(r"(?P<path>\S+)\s+must\s+contain\s+exactly\s+(?P<body>.+)$", re.IGNORECASE)


def goal_acceptance_applies(
    text: str,
    task: Mapping[str, Any],
    siblings: list[Any] | None,
) -> bool:
    """Whether a Goal-level acceptance text gates this Task.

    Prose applies to every Task (and is reported unsupported where nothing
    can check it). An exact-content criterion names one file: it gates the
    Task(s) whose expected_artifacts contain that path. When no Task declares
    it, the sink Tasks (nothing depends on them) carry it, so the check is
    never silently skipped. Without sibling context the old behaviour holds.
    """
    match = _GOAL_EXACT_RE.match(str(text or "").strip())
    if not match or siblings is None:
        return True
    rows = [r for r in siblings if isinstance(r, Mapping)]
    if len(rows) <= 1:
        return True
    target = match.group("path").strip().strip("\"'")
    me = str(task.get("task_id") or "")
    owners = [
        str(r.get("task_id") or "")
        for r in rows
        if target in [str(a).strip() for a in (r.get("expected_artifacts") or [])]
    ]
    if owners:
        return me in owners
    depended = {str(d) for r in rows for d in (r.get("depends_on") or [])}
    sinks = [str(r.get("task_id") or "") for r in rows if str(r.get("task_id") or "") not in depended]
    return me in sinks


def worker_charter_for_task(
    *,
    goal: Mapping[str, Any] | None,
    task: Mapping[str, Any],
    siblings: list[Any] | None = None,
) -> dict[str, Any]:
    """Per-task worker contract. Do not send the whole Goal as the worker goal.

    ``siblings`` (the Goal's tasks) scopes an exact-content Goal acceptance to
    the Task that delivers the named file (see goal_acceptance_applies).
    """
    goal_obj = goal if isinstance(goal, Mapping) else {}
    boundaries = goal_obj.get("boundaries") if isinstance(goal_obj.get("boundaries"), Mapping) else {}
    instruction = str((task.get("inputs") or {}).get("instruction") or task.get("title") or "").strip()
    budget = goal_obj.get("budget") if isinstance(goal_obj.get("budget"), Mapping) else {}
    raw_done = task.get("done_when")
    if isinstance(raw_done, Mapping):
        done_when: Any = dict(raw_done)
    else:
        done_when = raw_done or {}
    charter: dict[str, Any] = {
        "goal": instruction,
        "must": [str(x) for x in (boundaries.get("must") or [])],
        "must_not": [str(x) for x in (boundaries.get("must_not") or [])],
        "done_when": done_when,
        "timeout_sec": budget.get("wall_sec"),
        "max_redos": budget.get("max_reworks"),
    }
    acceptance_src = goal_obj.get("acceptance")
    if isinstance(acceptance_src, Mapping) and "text" in acceptance_src:
        acc_text = acceptance_src.get("text")
        if not isinstance(acc_text, str):
            raise AppError("acceptance.text must be a string")
        if goal_acceptance_applies(acc_text, task, siblings):
            charter["acceptance"] = acc_text
    elif isinstance(acceptance_src, str):
        if goal_acceptance_applies(acceptance_src, task, siblings):
            charter["acceptance"] = acceptance_src
    raw_forbidden = goal_obj.get("forbidden_tools")
    if isinstance(raw_forbidden, list):
        forbidden = [str(x).strip() for x in raw_forbidden if str(x).strip()]
        if forbidden:
            charter["forbidden_tools"] = forbidden
    raw_external = goal_obj.get("external_inputs")
    if isinstance(raw_external, list) and raw_external:
        pinned: list[dict[str, str]] = []
        for item in raw_external:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            digest = item.get("sha256")
            if isinstance(path, str) and path.strip() and isinstance(digest, str) and digest.strip():
                pinned.append({"path": path.strip(), "sha256": digest.strip()})
        if pinned:
            charter["external_inputs"] = pinned
    if allows_aigc_marks(goal_obj, task):
        charter["allow_aigc_marks"] = True
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


def _decision_details(decision: Mapping[str, Any]) -> Mapping[str, Any]:
    details = decision.get("details")
    return details if isinstance(details, Mapping) else {}


def _pending_lead_error(decision: Mapping[str, Any]) -> Mapping[str, Any]:
    """Lead error on the decision: ``details`` first, then the top-level copy."""
    details = _decision_details(decision)
    err = details.get("lead_error") if isinstance(details.get("lead_error"), Mapping) else {}
    if not err and isinstance(decision.get("lead_error"), Mapping):
        err = dict(decision.get("lead_error") or {})
    return err


def _pending_lead_attempts(decision: Mapping[str, Any]) -> int:
    return int(_decision_details(decision).get("lead_attempts") or 0)


def _lead_auto_resolve_capable(planner: Any, backend: Any, backend_kind: str) -> bool:
    """Planner can decide, the backend can apply it, and this kind is auto-resolved."""
    return bool(
        callable(getattr(planner, "decide_action", None))
        and callable(getattr(backend, "resolve_decision", None))
        and str(backend_kind or "") in AUTO_RESOLVE_BACKEND_KINDS
    )


def _lead_attempts_exhausted(decision: Mapping[str, Any]) -> bool:
    """True when the lead must stop and the decision waits for a human.

    Exhausted means ``attempts >= MAX_LEAD_ATTEMPTS_PER_DECISION``, or at least
    one attempt whose ``lead_error.retryable`` is not true (missing counts as
    not retryable).
    """
    err = _pending_lead_error(decision)
    attempts = _pending_lead_attempts(decision)
    return attempts >= MAX_LEAD_ATTEMPTS_PER_DECISION or (
        attempts >= 1 and not err.get("retryable", False)
    )


def _lead_will_decide(planner: Any, backend: Any, decision: Mapping[str, Any]) -> bool:
    """True when the next tick will ask the lead instead of a human."""
    if not isinstance(decision, Mapping):
        return False
    kind = str(_decision_details(decision).get("backend_kind") or "")
    if not _lead_auto_resolve_capable(planner, backend, kind):
        return False
    return not _lead_attempts_exhausted(decision)
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


def review_contamination_summary(payload: Any) -> str:
    """``summarize`` of contamination dicts carried on a review payload.

    Windows ``snapshot`` stores each artifact as a dict with a ``contamination``
    object from ``scan_bytes``. Empty when nothing is contaminated.
    """
    if not isinstance(payload, Mapping):
        return ""
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return ""
    findings: dict[str, Any] = {}
    for name, meta in artifacts.items():
        if not isinstance(meta, Mapping):
            continue
        scan = meta.get("contamination")
        if isinstance(scan, Mapping):
            findings[str(name)] = dict(scan)
    return summarize(findings)


def _artifact_label(item: str, root: Path | None, resolved: Path) -> str:
    raw = Path(item)
    if not raw.is_absolute():
        return raw.as_posix()
    if root is not None:
        try:
            return resolved.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return resolved.name


def gate_collected_artifacts(result: Any, *, allow_aigc_marks: bool = False) -> Any:
    """Fail an otherwise successful backend result whose artifacts are contaminated.

    Runs only when ``result["ok"]`` is true and ``allow_aigc_marks`` is false.
    Artifact entries may be absolute paths or paths relative to
    ``result["workspace"]``. Missing paths are skipped. On a hit, sets
    ``ok`` false, ``error`` to ``artifact_contaminated: <summarize()>``, and
    ``artifact_contamination`` to the per-artifact scan dicts. Opt-out returns
    the result unchanged. The scanner itself stays free of this policy.
    """
    if allow_aigc_marks or not isinstance(result, dict) or not result.get("ok"):
        return result
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return result
    workspace = result.get("workspace")
    root = Path(workspace) if isinstance(workspace, str) and workspace.strip() else None
    findings: dict[str, Any] = {}
    for item in artifacts:
        if not isinstance(item, str) or not item.strip():
            continue
        candidate = Path(item)
        if not candidate.is_absolute():
            if root is None:
                continue
            candidate = root / item
        if not candidate.is_file():
            continue
        try:
            scan = scan_file(candidate)
        except OSError:
            continue
        findings[_artifact_label(item, root, candidate)] = scan
    line = summarize(findings)
    if not line:
        return result
    failed = dict(result)
    failed["ok"] = False
    failed["error"] = f"artifact_contaminated: {line}"
    failed["artifact_contamination"] = findings
    return failed


def _gate_backend_result(snap: Mapping[str, Any], task: Mapping[str, Any], result: Any) -> Any:
    goal = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
    return gate_collected_artifacts(result, allow_aigc_marks=allows_aigc_marks(goal, task))


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


_DONE_WHEN_KEYS = frozenset({"artifacts", "text"})
# Goal-level acceptance keys the service actually reads.
GOAL_ACCEPTANCE_KEYS = frozenset({"artifacts", "text", "allow_aigc_marks"})


def _validate_done_when(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate per-task ``done_when``. Absent means the row has none.

    Only a mapping with ``artifacts`` (safe relative paths) and/or ``text``
    (string) is legal. Strings, lists, numbers, and unknown keys are
    ``invalid_plan``. ``done_when.artifacts`` is always passed through
    ``_safe_artifacts`` and must not replace the row's artifacts unsafely.
    """
    if "done_when" not in row or row.get("done_when") is None:
        return None
    done = row.get("done_when")
    if not isinstance(done, Mapping):
        raise AppError(
            "done_when must be an object with keys 'artifacts' and/or 'text'",
            code="invalid_plan",
        )
    unknown = sorted(str(key) for key in done.keys() if key not in _DONE_WHEN_KEYS)
    if unknown:
        raise AppError(
            "done_when has unknown keys: " + ", ".join(unknown),
            code="invalid_plan",
        )
    out: dict[str, Any] = {}
    if "artifacts" in done:
        try:
            out["artifacts"] = _safe_artifacts(done.get("artifacts"))
        except AppError as exc:
            raise AppError(str(exc), code="invalid_plan") from exc
    if "text" in done:
        text = done.get("text")
        if not isinstance(text, str):
            raise AppError("done_when.text must be a string", code="invalid_plan")
        out["text"] = text
    return out


def _task_done_when(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep artifacts from the validated plan row and copy ``text`` when it is a string."""
    done_when: dict[str, Any] = {"artifacts": list(row.get("artifacts") or [])}
    row_done = row.get("done_when")
    if isinstance(row_done, Mapping):
        text = row_done.get("text")
        if isinstance(text, str):
            done_when["text"] = text
    return done_when


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
        done = _validate_done_when(row)
        has_row_artifacts = "artifacts" in row and row.get("artifacts") is not None
        if has_row_artifacts:
            try:
                artifacts = _safe_artifacts(row.get("artifacts"))
            except AppError as exc:
                # Planner output problems are invalid_plan, same as done_when (#11).
                raise AppError(str(exc), code="invalid_plan") from exc
        elif done is not None and "artifacts" in done:
            # No row artifacts: the validated done_when list becomes the artifacts.
            artifacts = list(done["artifacts"])
        else:
            artifacts = _safe_artifacts(row.get("artifacts"))
        if done is not None and "artifacts" in done:
            allowed = set(artifacts)
            if any(item not in allowed for item in done["artifacts"]):
                raise AppError(
                    "done_when.artifacts must be a subset of task artifacts",
                    code="invalid_plan",
                )
        clean_row: dict[str, Any] = {
            "task_key": key,
            "title": title,
            "instruction": instruction,
            "depends_on": _strings(row.get("depends_on") or [], field="task.depends_on"),
            "artifacts": list(artifacts),
        }
        if done is not None:
            stored_done: dict[str, Any] = {}
            if "artifacts" in done:
                stored_done["artifacts"] = list(done["artifacts"])
            if "text" in done:
                stored_done["text"] = done["text"]
            clean_row["done_when"] = stored_done
        clean.append(clean_row)
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
        done_when: dict[str, Any] = {"artifacts": list(artifacts)}
        if "text" in acceptance:
            acc_text = acceptance.get("text")
            if not isinstance(acc_text, str):
                raise AppError("acceptance.text must be a string")
            done_when["text"] = acc_text
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
                    "done_when": done_when,
                }
            ],
        }
        return validate_plan(raw, request)


def _candidate_artifacts(task: Mapping[str, Any] | None) -> list[str]:
    """Expected artifacts that exist as regular files in the task's bound workspace.

    Read-only stat. Unsafe names, symlinks and anything resolving outside the
    workspace are skipped. Existence is not acceptance.
    """
    if not isinstance(task, Mapping):
        return []
    ws = str(task.get("workspace") or "").strip()
    if not ws:
        return []
    root = Path(ws)
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    found: list[str] = []
    for name in list(task.get("expected_artifacts") or [])[:64]:
        rel = str(name or "").strip()
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts or "\\" in rel:
            continue
        cand = root / rel
        try:
            if cand.is_symlink() or not cand.is_file():
                continue
            cand.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        found.append(rel)
    return found


def _annotate_task_failure(brief: dict[str, Any], task: Mapping[str, Any] | None) -> None:
    """Stage, unreviewed candidate presence and review status on a task failure (#12/#2)."""
    brief.setdefault("stage", "worker")
    present = _candidate_artifacts(task)
    brief["candidate_available"] = bool(present)
    brief["candidate_artifacts"] = present
    review = None
    if isinstance(task, Mapping):
        result = task.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("review"), Mapping):
            review = result.get("review")
        elif isinstance(task.get("review"), Mapping):
            review = task.get("review")
    status = review.get("status") if isinstance(review, Mapping) else None
    brief["review_status"] = str(status or "not_requested")


def acceptance_status_view(
    state: str, tasks: list[Any], caps: Mapping[str, Any]
) -> dict[str, Any]:
    """Separate execution, artifacts, independent checks, technical review,
    business acceptance and deployment (#2). Never claims what was not checked.

    ``completed`` means execution finished and every gate the backend can run
    passed. It is not business acceptance; the service never performs that.
    """
    rows = [t for t in tasks if isinstance(t, Mapping)]
    state = str(state or "")
    if state == "completed":
        execution = "succeeded"
    elif state == "failed":
        execution = "failed"
        for t in rows:
            if str(t.get("status") or "") == "failed":
                from framework.failure_projection import failure_brief

                if failure_brief(t).get("source") == "worker_timeout":
                    execution = "timeout"
                    break
    elif state == "cancelled":
        execution = "cancelled"
    else:
        execution = "in_progress"
    missing: list[str] = []
    finished = 0
    for t in rows:
        result = t.get("result") if isinstance(t.get("result"), Mapping) else None
        if result is None:
            continue
        finished += 1
        for item in result.get("missing") or result.get("missing_artifacts") or []:
            if isinstance(item, str) and item not in missing:
                missing.append(item)
    if missing:
        artifacts = "incomplete"
    elif rows and finished == len(rows) and execution == "succeeded":
        artifacts = "complete"
    else:
        artifacts = "unknown"
    reviews = []
    for t in rows:
        result = t.get("result") if isinstance(t.get("result"), Mapping) else {}
        review = result.get("review") if isinstance(result.get("review"), Mapping) else None
        if review is not None:
            reviews.append(review)
    exact = [r for r in reviews if r.get("source") == "agy_exact_content"]
    if not exact:
        independent = "not_run"
    elif all(r.get("status") == "passed" for r in exact):
        independent = "passed"
    else:
        independent = "failed"
    acceptance_caps = caps.get("acceptance") if isinstance(caps.get("acceptance"), Mapping) else {}
    unsupported = any(r.get("status") == "unsupported" for r in reviews)
    if acceptance_caps.get("lead_review") is not True:
        technical = "unsupported" if unsupported else "not_available"
    else:
        technical = "via_lead_gate" if execution == "succeeded" else "not_concluded"
    return {
        "execution": execution,
        "artifacts": artifacts,
        "missing_artifacts": missing[:32],
        "independent_checks": independent,
        "technical_review": technical,
        "business_acceptance": "not_performed",
        "deployed": "not_tracked",
        "note": "completed is not business acceptance; read the artifacts before accepting",
    }


def _lead_failure_of(parsed: Any) -> tuple[str, str] | None:
    """(status, message) for a safe_failure envelope, else None."""
    if not isinstance(parsed, Mapping):
        return None
    status = parsed.get("_lead_status")
    if not status:
        return None
    from framework.need_human import sanitize_reason

    status_text = sanitize_reason(str(status), max_len=40) or "error"
    # Adapter errors can carry CLI stderr: redact secret-looking text, one line.
    message = sanitize_reason(str(parsed.get("error") or status_text), max_len=300)
    return status_text, message


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
        # Read the adapter's canonical failure envelope BEFORE unwrapping:
        # unwrap_structured() returns None for {_lead_status, error}, which used
        # to turn timeout/call_failed/error into a generic invalid_plan (#16).
        failure = _lead_failure_of(parsed)
        if failure is None:
            parsed = unwrap_structured(parsed)
            failure = _lead_failure_of(parsed)
        if failure is not None:
            lead_status, message = failure
            raise AppError(
                f"lead planning failed [{lead_status}]: {message}",
                status=503,
                code="lead_unavailable",
                extra={"lead_status": lead_status},
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
        max_parallel_per_goal: int = 2,
        max_parallel_global: int = 4,
        stale_after_sec: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.layer = layer
        self.planner = planner or DeterministicPlanner()
        self.backend = backend or InProcessExecutionBackend()
        self.workspaces_root = Path(workspaces_root)
        self.max_parallel_per_goal = as_limit(max_parallel_per_goal) or 2
        self.max_parallel_global = as_limit(max_parallel_global) or 4
        try:
            stale = float(stale_after_sec)
        except (TypeError, ValueError):
            stale = 120.0
        self.stale_after_sec = stale if stale > 0 else 120.0
        # goal_id -> epoch seconds while a planner call is in flight (in-memory only).
        self._planning_since: dict[str, float] = {}
        self._clock = clock or time.time
        # Claims whose start_run is in progress in this process. A running
        # task with no run_id is "dispatch in progress" only while its token
        # is here; a restarted process fails that task closed.
        self._inflight_claims: set[str] = set()
        # HTTP ticks and the background loop may fire together.  Serialize
        # coordination so a queued Task is dispatched at most once by this
        # service instance. Released only around start_run.
        self._lock = threading.RLock()

    def now(self) -> float:
        return float(self._clock())

    def _service_caps(self) -> dict[str, Any]:
        fn = getattr(self.backend, "capabilities", None)
        if not callable(fn):
            return {}
        try:
            raw = fn()
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def goal_progress_view(
        self,
        snap: Mapping[str, Any],
        scheduler: Mapping[str, Any] | None,
        caps: Mapping[str, Any],
    ) -> dict[str, Any]:
        progress_cap = caps.get("progress") if isinstance(caps.get("progress"), Mapping) else {}
        view = derive_goal_progress(
            snap,
            scheduler=scheduler,
            capability=progress_cap,
            stale_after_sec=self.stale_after_sec,
            now=self.now(),
        )
        started = self._planning_since.get(str(snap.get("goal_id") or ""))
        if started is not None and not (snap.get("tasks") or []):
            # The lead is planning in this process; no worker exists yet (#5).
            view = dict(view)
            view["state"] = "planning"
            view["phase"] = "planning"
            view["source"] = "coordinator"
            view["planning_started_at"] = utc_iso(started)
            view["recent_events"] = ["phase planning (lead call in flight; no worker started)"]
        return view

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
            self._planning_since[goal_id] = time.time()
            try:
                plan = self.planner.plan(snap)
            except Exception as e:
                self._planning_since.pop(goal_id, None)
                detail = f"planner failed: {type(e).__name__}"
                failure_extra: dict[str, Any] = {"source": "planner", "code": "planner_error"}
                if isinstance(e, AppError):
                    detail = f"planner failed [{e.code}]: {str(e)[:300]}"
                    failure_extra["code"] = e.code
                    lead_status = e.extra.get("lead_status") if isinstance(e.extra, Mapping) else None
                    if lead_status:
                        failure_extra["lead_status"] = str(lead_status)
                failed = self.layer.fail_goal(
                    goal_id,
                    phase="planning",
                    error=detail,
                    extra=failure_extra,
                )
                return {**failed, "action": "planning_failed"}
            self._planning_since.pop(goal_id, None)
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
                        "done_when": _task_done_when(row),
                        "assignee_role": "executor",
                        "backend_requirement": getattr(self.backend, "backend_id", ""),
                    },
                )
                if not added.get("ok"):
                    return added
            current = self.layer.get_goal(goal_id)
            snap = current["goal"]
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]

        return self._advance_goal(goal_id)

    _PREFERRED_TICK_ACTIONS = frozenset(
        {"decision_required", "decision_resolved", "backend_gate_unprojected"}
    )
    _TICK_TERMINAL = frozenset({"completed", "failed", "cancelled", "cancel_requested"})

    def _advance_goal(self, goal_id: str) -> dict[str, Any]:
        """Observe every in-flight task, then start ready tasks up to capacity.

        One busy observe does not end the tick. A capacity wait is not a failure.
        """
        blocked = self._budget_stop(goal_id)
        if blocked is not None:
            return blocked
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            return current
        snap = current["goal"]
        tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        outcomes: list[dict[str, Any]] = []
        for task in tasks:
            status = str(task.get("status") or "")
            if status == "awaiting_decision":
                outcomes.append(
                    {
                        "ok": True,
                        "goal_id": goal_id,
                        "state": "running",
                        "action": "decision_required",
                    }
                )
                continue
            if status != "running":
                continue
            observed = self._observe_running_task(snap, task)
            if observed is None:
                continue
            outcomes.append(observed)
        skipped: set[str] = set()
        while True:
            current = self.layer.get_goal(goal_id)
            if not current.get("ok"):
                return current
            snap = current["goal"]
            if snap.get("state") in self._TICK_TERMINAL:
                break
            blocked = self._budget_stop(goal_id)
            if blocked is not None:
                return blocked
            current = self.layer.get_goal(goal_id)
            if not current.get("ok"):
                return current
            snap = current["goal"]
            if snap.get("state") in self._TICK_TERMINAL:
                break
            if global_decision_blocks(snap.get("pending_decisions") or []):
                break
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            status_by_id = {str(t.get("task_id") or ""): str(t.get("status") or "") for t in tasks}
            ready = [
                t
                for t in tasks
                if task_is_ready(t, status_by_id) and str(t.get("task_id") or "") not in skipped
            ]
            if not ready:
                break
            if self._slots_for(goal_id) <= 0:
                break
            _rows, held, _running = self._occupancy()
            chosen = None
            for task in ready:
                key = directory_key(
                    dispatch_directory(task, workspaces_root=self.workspaces_root, goal_id=goal_id)
                )
                if key not in held:
                    chosen = task
                    break
            if chosen is None:
                break
            task_id = str(chosen.get("task_id") or "")
            outcome = self._dispatch_one(goal_id, task_id)
            if outcome is None:
                skipped.add(task_id)
                continue
            if outcome.get("_return"):
                returned = dict(outcome["_return"])
                return returned
            outcomes.append(outcome)
        return self._select_outcome(goal_id, outcomes)

    def _select_outcome(self, goal_id: str, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        current = self.layer.get_goal(goal_id)
        snap = current.get("goal") if current.get("ok") else {}
        state = str((snap or {}).get("state") or "")
        preferred = [row for row in outcomes if row.get("action") in self._PREFERRED_TICK_ACTIONS]
        finished = [row for row in outcomes if row.get("action") == "task_finished"]
        running = [row for row in outcomes if row.get("action") == "worker_running"]
        if preferred:
            return preferred[-1]
        if finished:
            return finished[-1]
        if running:
            return running[-1]
        raws = [row for row in outcomes if "action" not in row and (row.get("task") or row.get("reason"))]
        if raws:
            payload = dict(raws[-1])
            payload.pop("_stop", None)
            payload.pop("_return", None)
            if state:
                payload["state"] = state
            return payload
        if state in {"completed", "failed", "cancelled"}:
            return {"ok": True, "goal_id": goal_id, "state": state, "action": "terminal"}
        return {"ok": True, "goal_id": goal_id, "state": state or "running", "action": "waiting"}

    def _budget_stop(self, goal_id: str) -> dict[str, Any] | None:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            return current
        snap = current["goal"]
        if snap.get("state") in self._TICK_TERMINAL:
            return None
        reason = budget_block_reason(snap, now=self.now())
        if not reason:
            return None
        tasks = [t for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        created = snap.get("created_at")
        elapsed = None
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            elapsed = self.now() - float(created)
        checkpoints: list[dict[str, Any]] = []
        last_progress_at = None
        last_progress_epoch: float | None = None
        wall = "wall_sec" in reason
        for task in tasks:
            if str(task.get("status") or "") not in IN_FLIGHT:
                continue
            run_id = str(task.get("run_id") or "").strip()
            observed: Mapping[str, Any] | None = None
            if run_id:
                try:
                    raw_obs = self.backend.observe_run(run_id)
                except Exception:
                    raw_obs = None
                if isinstance(raw_obs, Mapping):
                    observed = raw_obs
            progress = self._progress_from_observation(
                observed,
                workspace=str(task.get("workspace") or ""),
            )
            if progress:
                self.layer.record_task_progress(goal_id, str(task.get("task_id") or ""), progress)
            ck = progress.get("artifacts_checkpoint") if isinstance(progress.get("artifacts_checkpoint"), list) else []
            for item in ck:
                if isinstance(item, Mapping) and len(checkpoints) < 32:
                    checkpoints.append(dict(item))
            lp = progress.get("last_progress_at")
            lp_epoch = parse_iso(lp)
            if lp_epoch is not None and (last_progress_epoch is None or lp_epoch > last_progress_epoch):
                last_progress_epoch = lp_epoch
                last_progress_at = lp if isinstance(lp, str) else None
            if run_id:
                self._cancel_run(run_id)
            result: dict[str, Any] = {"ok": False, "run_id": run_id, "error": reason}
            if wall:
                result["error_source"] = "worker_timeout"
                if elapsed is not None:
                    result["elapsed"] = elapsed
                result["last_progress_at"] = last_progress_at
                result["artifacts_checkpoint"] = list(ck)
            self.layer.finish_task(
                goal_id,
                str(task.get("task_id") or ""),
                succeeded=False,
                close_goal=True,
                result=result,
            )
        extra = None
        if wall:
            extra = {
                "elapsed": elapsed,
                "last_progress_at": last_progress_at,
                "artifacts_checkpoint": checkpoints,
            }
        failed = self.layer.fail_goal(goal_id, phase="budget", error=reason, extra=extra)
        return {**failed, "action": "budget_exhausted"}

    def _cancel_run(self, run_id: str) -> None:
        if not str(run_id or "").strip():
            return
        try:
            self.backend.cancel(run_id)
        except Exception:
            return

    def _progress_from_observation(self, observation: Mapping[str, Any] | None, *, workspace: str) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if isinstance(observation, Mapping) and isinstance(observation.get("progress"), Mapping):
            raw = dict(observation["progress"])
        if isinstance(observation, Mapping):
            usage = observation.get("usage")
            if isinstance(usage, Mapping) and "usage" not in raw and "usage_fields" not in raw:
                raw["usage"] = dict(usage)
            calls = tool_call_total(observation if isinstance(observation, Mapping) else None)
            if calls is not None and "tool_calls" not in raw:
                raw["tool_calls"] = calls
        if workspace and not raw.get("artifacts_checkpoint"):
            entries, latest = artifact_checkpoint(workspace)
            if entries:
                raw["artifacts_checkpoint"] = entries
            if latest is not None and not raw.get("last_progress_at"):
                raw["last_progress_at"] = utc_iso(latest)
        if not raw:
            return {}
        return bound_task_progress(raw)

    def _note_task_progress(
        self,
        goal_id: str,
        task_id: str,
        observation: Mapping[str, Any] | None,
        *,
        workspace: str,
    ) -> None:
        progress = self._progress_from_observation(observation, workspace=workspace)
        if not progress:
            return
        self.layer.record_task_progress(goal_id, task_id, progress)

    def _meter_source(self, caps: Mapping[str, Any]) -> str:
        metering = caps.get("metering") if isinstance(caps.get("metering"), Mapping) else {}
        source = str(metering.get("source") or "")
        if source in {"worker_self_reported", "none"}:
            return source
        usage = caps.get("usage") if isinstance(caps.get("usage"), Mapping) else {}
        if usage.get("source") == "worker_self_reported":
            return "worker_self_reported"
        return "none"

    def _usage_rows(self, tasks: list[Mapping[str, Any]]) -> tuple[int, int, dict[str, dict[str, Any]], str]:
        """Sum reported totals across in-flight tasks. Returns tokens, tool calls, fields, source."""
        caps = self._service_caps()
        source = self._meter_source(caps)
        if source == "none":
            source = "worker_self_reported"
        tokens = 0
        calls = 0
        saw_tokens = False
        saw_calls = False
        fields_by_task: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if str(task.get("status") or "") not in IN_FLIGHT:
                continue
            progress = task.get("progress") if isinstance(task.get("progress"), Mapping) else {}
            fields = usage_fields(progress.get("usage_fields"))
            tid = str(task.get("task_id") or "")
            if fields:
                fields_by_task[tid] = fields
            total = token_total(fields)
            if total is not None:
                saw_tokens = True
                tokens += total
            counted = progress.get("tool_calls")
            if isinstance(counted, int) and not isinstance(counted, bool) and counted >= 0:
                saw_calls = True
                calls += counted
        return (tokens if saw_tokens else -1), (calls if saw_calls else -1), fields_by_task, source

    def _enforce_live(self, goal_id: str) -> dict[str, Any] | None:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            return None
        snap = current["goal"]
        if snap.get("state") in self._TICK_TERMINAL:
            return None
        budget = budget_of(snap)
        if budget_mode(budget) != "enforce":
            return None
        caps = self._service_caps()
        tasks = [t for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        token_sum, call_sum, fields_by_task, source = self._usage_rows(tasks)
        tripped: tuple[str, int, int, str] | None = None
        limit_tokens = budget.get("max_tokens")
        if (
            enforcement_level(caps, "max_tokens") == "enforced_live"
            and isinstance(limit_tokens, int)
            and not isinstance(limit_tokens, bool)
            and token_sum >= 0
            and token_sum > limit_tokens
        ):
            tripped = ("max_tokens", int(limit_tokens), int(token_sum), "enforced_live")
        limit_calls = budget.get("max_tool_calls")
        if tripped is None and (
            enforcement_level(caps, "max_tool_calls") == "enforced_live"
            and isinstance(limit_calls, int)
            and not isinstance(limit_calls, bool)
            and call_sum >= 0
            and call_sum > limit_calls
        ):
            tripped = ("max_tool_calls", int(limit_calls), int(call_sum), "enforced_live")
        if tripped is None:
            return None
        field, limit, used, level = tripped
        self.layer.set_budget_status(
            goal_id,
            {
                field: {
                    "limit": limit,
                    "used": used,
                    "source": source,
                    "exceeded": True,
                    "enforced": level,
                }
            },
        )
        error = f"budget_exceeded {field}"
        for task in tasks:
            if str(task.get("status") or "") not in IN_FLIGHT:
                continue
            run_id = str(task.get("run_id") or "").strip()
            self._cancel_run(run_id)
            tid = str(task.get("task_id") or "")
            fields = fields_by_task.get(tid) or {}
            result: dict[str, Any] = {"ok": False, "run_id": run_id, "error": error}
            if fields:
                result["usage"] = dict(fields)
                report = usage_report(fields, source=source)
                if report:
                    result["usage_report"] = report
            self.layer.finish_task(goal_id, tid, succeeded=False, close_goal=False, result=result)
        reconciled = self.layer.reconcile_goal_progress(goal_id)
        return {
            "ok": False,
            "goal_id": goal_id,
            "state": reconciled.get("state") or "failed",
            "action": "budget_exceeded",
            "error": error,
        }

    def _enforce_no_progress(self, goal_id: str, task_id: str) -> dict[str, Any] | None:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            return None
        snap = current["goal"]
        if snap.get("state") in self._TICK_TERMINAL:
            return None
        budget = budget_of(snap)
        limit = budget.get("no_progress_sec")
        if not isinstance(limit, (int, float)) or isinstance(limit, bool) or float(limit) <= 0:
            return None
        caps = self._service_caps()
        if enforcement_level(caps, "no_progress_sec") != "enforced":
            return None
        tasks = [t for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        task = next((t for t in tasks if str(t.get("task_id") or "") == task_id), None)
        if task is None or str(task.get("status") or "") != "running":
            return None
        pending = [
            d
            for d in (snap.get("pending_decisions") or [])
            if isinstance(d, Mapping) and str(d.get("task_id") or "") == task_id
        ]
        if pending:
            return None
        progress = task.get("progress") if isinstance(task.get("progress"), Mapping) else {}
        anchor = parse_iso(progress.get("last_progress_at"))
        if anchor is None:
            started = task.get("run_started_at")
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                anchor = float(started)
        if anchor is None:
            return None
        if self.now() - anchor < float(limit):
            return None
        mode = budget_mode(budget)
        label = format_seconds(float(limit))
        if mode != "enforce":
            self.layer.set_budget_status(
                goal_id,
                {
                    "no_progress_sec": {
                        "limit": float(limit),
                        "exceeded": True,
                        "enforced": "report_only",
                    }
                },
            )
            return None
        run_id = str(task.get("run_id") or "").strip()
        self._cancel_run(run_id)
        checkpoint = progress.get("artifacts_checkpoint") if isinstance(progress.get("artifacts_checkpoint"), list) else []
        if on_no_progress(budget) == "fail":
            failed = self._fail_task(
                goal_id,
                task_id,
                {
                    "ok": False,
                    "run_id": run_id,
                    "error": "no_progress_timeout",
                    "last_progress_at": progress.get("last_progress_at"),
                    "artifacts_checkpoint": list(checkpoint),
                },
            )
            return {**failed, "action": "task_finished"}
        summary = f"checkpoint: no progress for {label}s"
        workspace = str(task.get("workspace") or "")
        self.layer.append_goal_warnings(goal_id, [summary])
        opened = self.layer.open_checkpoint(
            goal_id,
            task_id=task_id,
            run_id=run_id,
            summary=summary,
            result={
                "ok": False,
                "run_id": run_id,
                "workspace": workspace,
                "partial": True,
                "need_human": True,
                "artifacts_checkpoint": list(checkpoint),
                "last_progress_at": progress.get("last_progress_at"),
                "error": summary,
            },
            reason=summary,
        )
        decision = opened.get("decision") if isinstance(opened.get("decision"), Mapping) else {}
        return {
            "ok": True,
            "goal_id": goal_id,
            "state": "running",
            "action": "decision_required",
            "decision_id": decision.get("decision_id"),
        }

    def _enforce_budgets(self, goal_id: str, task_id: str) -> dict[str, Any] | None:
        live = self._enforce_live(goal_id)
        if live is not None:
            return live
        return self._enforce_no_progress(goal_id, task_id)

    def _attach_usage_report(self, result: Any) -> Any:
        if not isinstance(result, dict) or not isinstance(result.get("usage"), Mapping):
            return result
        source = self._meter_source(self._service_caps())
        if source == "none":
            source = "worker_self_reported"
        report = usage_report(result.get("usage"), source=source)
        if not report:
            return result
        out = dict(result)
        out["usage_report"] = report
        return out

    def _post_hoc_budget(self, snap: Mapping[str, Any], task: Mapping[str, Any], result: Any) -> dict[str, Any] | None:
        """After a run is collected. Post-hoc overage parks the task; it does not drop artifacts."""
        if not isinstance(result, dict):
            return None
        goal_id = str(snap.get("goal_id") or "")
        task_id = str(task.get("task_id") or "")
        if not goal_id or not task_id:
            return None
        budget = budget_of(snap)
        caps = self._service_caps()
        mode = budget_mode(budget)
        source = self._meter_source(caps)
        if source == "none":
            source = "worker_self_reported"
        fields = usage_fields(result.get("usage"))
        warnings: list[str] = []
        parked_summary = ""
        status_row: dict[str, Any] = {}
        limit_tokens = budget.get("max_tokens")
        used_tokens = token_total(fields)
        token_level = enforcement_level(caps, "max_tokens")
        if (
            isinstance(limit_tokens, int)
            and not isinstance(limit_tokens, bool)
            and used_tokens is not None
            and token_level in {"post_hoc", "enforced_live"}
            and used_tokens > limit_tokens
        ):
            status_row["max_tokens"] = {
                "limit": int(limit_tokens),
                "used": int(used_tokens),
                "source": source,
                "exceeded": True,
                "enforced": token_level,
            }
            if mode == "enforce" and token_level == "post_hoc":
                result["budget_exceeded"] = True
                parked_summary = "budget exceeded: continue or stop"
                warnings.append("budget max_tokens exceeded (post_hoc); not a bill")
            elif mode == "enforce" and token_level == "enforced_live":
                result["ok"] = False
                result["budget_exceeded"] = True
                result["error"] = "budget_exceeded max_tokens"
                self._cancel_run(str(result.get("run_id") or task.get("run_id") or ""))
                warnings.append("budget max_tokens exceeded; not a bill")
            elif mode != "enforce":
                warnings.append("budget max_tokens exceeded (report_only); not a bill")
        calls = tool_call_total(result)
        limit_calls = budget.get("max_tool_calls")
        call_level = enforcement_level(caps, "max_tool_calls")
        if (
            not parked_summary
            and isinstance(limit_calls, int)
            and not isinstance(limit_calls, bool)
            and calls is not None
            and call_level in {"post_hoc", "enforced_live"}
            and calls > limit_calls
        ):
            status_row["max_tool_calls"] = {
                "limit": int(limit_calls),
                "used": int(calls),
                "source": source,
                "exceeded": True,
                "enforced": call_level,
            }
            if mode == "enforce" and call_level == "post_hoc":
                result["budget_exceeded"] = True
                parked_summary = "budget exceeded: continue or stop"
                warnings.append("budget max_tool_calls exceeded (post_hoc); not a bill")
            elif mode == "enforce" and call_level == "enforced_live":
                result["ok"] = False
                result["budget_exceeded"] = True
                result["error"] = "budget_exceeded max_tool_calls"
                self._cancel_run(str(result.get("run_id") or task.get("run_id") or ""))
                warnings.append("budget max_tool_calls exceeded; not a bill")
            elif mode != "enforce":
                warnings.append("budget max_tool_calls exceeded (report_only); not a bill")
        if status_row:
            self.layer.set_budget_status(goal_id, status_row)
        if warnings:
            self.layer.append_goal_warnings(goal_id, warnings)
        if not parked_summary or mode != "enforce":
            return None
        run_id = str(result.get("run_id") or task.get("run_id") or "")
        opened = self.layer.open_checkpoint(
            goal_id,
            task_id=task_id,
            run_id=run_id,
            summary=parked_summary,
            result=result,
            reason=parked_summary,
        )
        decision = opened.get("decision") if isinstance(opened.get("decision"), Mapping) else {}
        return {
            "ok": True,
            "goal_id": goal_id,
            "state": "running",
            "action": "decision_required",
            "decision_id": decision.get("decision_id"),
        }

    def _fail_task(self, goal_id: str, task_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        finished = self.layer.finish_task(
            goal_id,
            task_id,
            succeeded=False,
            close_goal=False,
            result=result,
        )
        reconciled = self.layer.reconcile_goal_progress(goal_id)
        payload = dict(finished) if isinstance(finished, dict) else {"ok": False}
        state = reconciled.get("state") or payload.get("state")
        if state:
            payload["state"] = state
        return payload

    def _backend_run_limit(self) -> int | None:
        fn = getattr(self.backend, "capabilities", None)
        if not callable(fn):
            return None
        try:
            raw = fn()
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        conc = raw.get("concurrency")
        if not isinstance(conc, dict):
            return None
        return as_limit(conc.get("max_runs"))

    def _goal_task_rows(self) -> list[tuple[str, list[dict[str, Any]]]]:
        out: list[tuple[str, list[dict[str, Any]]]] = []
        listed = self.layer.list_goals()
        for row in listed.get("goals") or []:
            if not isinstance(row, Mapping):
                continue
            gid = str(row.get("goal_id") or "")
            if not gid:
                continue
            got = self.layer.get_goal(gid)
            if not got.get("ok"):
                continue
            snap = got.get("goal") if isinstance(got.get("goal"), Mapping) else {}
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            out.append((gid, tasks))
        return out

    def _occupancy(self) -> tuple[list[tuple[str, list[dict[str, Any]]]], set[str], dict[str, int]]:
        rows = self._goal_task_rows()
        held: set[str] = set()
        running: dict[str, int] = {}
        for gid, tasks in rows:
            count = 0
            for task in tasks:
                if str(task.get("status") or "") not in IN_FLIGHT:
                    continue
                if occupies_run_slot(task):
                    count += 1
                # A parked checkpoint keeps its workspace claim for "continue".
                key = directory_key(
                    dispatch_directory(task, workspaces_root=self.workspaces_root, goal_id=gid)
                )
                if key:
                    held.add(key)
            running[gid] = count
        return rows, held, running

    def _slots_for(self, goal_id: str) -> int:
        _rows, _held, running = self._occupancy()
        total = sum(running.values())
        return remaining_slots(
            per_goal=self.max_parallel_per_goal,
            running_here=int(running.get(goal_id) or 0),
            global_limit=self.max_parallel_global,
            running_total=total,
            backend_limit=self._backend_run_limit(),
        )

    def scheduler_snapshot(
        self,
        goal_id: str,
        snap: Mapping[str, Any],
        public_caps: Mapping[str, Any],
    ) -> dict[str, Any]:
        conc = public_caps.get("concurrency") if isinstance(public_caps.get("concurrency"), Mapping) else {}
        per = as_limit(conc.get("max_parallel_per_goal")) or self.max_parallel_per_goal
        glob = as_limit(conc.get("max_parallel_global")) or self.max_parallel_global
        backend_limit = as_limit(conc.get("backend_max_runs"))
        running_elsewhere = 0
        held: set[str] = set()
        for gid, tasks in self._goal_task_rows():
            for task in tasks:
                if str(task.get("status") or "") not in IN_FLIGHT:
                    continue
                if gid != goal_id and occupies_run_slot(task):
                    running_elsewhere += 1
                key = directory_key(
                    dispatch_directory(task, workspaces_root=self.workspaces_root, goal_id=gid)
                )
                if key:
                    held.add(key)
        return scheduler_view(
            tasks=snap.get("tasks") or [],
            pending=snap.get("pending_decisions") or [],
            per_goal=per,
            global_limit=glob,
            backend_limit=backend_limit,
            running_elsewhere=running_elsewhere,
            held_keys=held,
            workspaces_root=self.workspaces_root,
            goal_id=goal_id,
        )

    def _after_observe(
        self,
        snap: Mapping[str, Any],
        task: Mapping[str, Any],
        observation: Mapping[str, Any],
        *,
        observe_error: str,
    ) -> dict[str, Any]:
        """Record progress, collect a finished run, or enforce budgets on a busy one."""
        goal_id = str(snap.get("goal_id") or task.get("goal_id") or "")
        task_id = str(task.get("task_id") or "")
        run_id = str(task.get("run_id") or "").strip()
        self._note_task_progress(
            goal_id,
            task_id,
            observation,
            workspace=str(task.get("workspace") or ""),
        )
        if not observation.get("busy"):
            try:
                result = self.backend.collect_result(run_id)
            except Exception as exc:
                result = {"ok": False, "run_id": run_id, "error": f"{observe_error}: {type(exc).__name__}"}
            fresh = self.layer.get_goal(goal_id)
            if fresh.get("ok") and isinstance(fresh.get("goal"), Mapping):
                snap = fresh["goal"]
            return self._finish_gated(snap, task, result)
        enforced = self._enforce_budgets(goal_id, task_id)
        if enforced is not None:
            return enforced
        return {"ok": True, "goal_id": goal_id, "state": "running", "action": "worker_running"}

    def _observe_fresh_run(self, snap: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
        """Observe a run that was just bound. Do not open permission decisions yet."""
        goal_id = str(snap.get("goal_id") or task.get("goal_id") or "")
        run_id = str(task.get("run_id") or "").strip()
        try:
            observation = self.backend.observe_run(run_id)
            if not isinstance(observation, Mapping):
                raise TypeError("observe_run did not return an object")
        except Exception as exc:
            result = {
                "ok": False,
                "run_id": run_id,
                "error": f"backend observation failed: {type(exc).__name__}",
            }
            return self._finish_gated(snap, task, result)
        return self._after_observe(snap, task, observation, observe_error="backend observation failed")

    def _observe_running_task(self, snap: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any] | None:
        """Poll one running task. A decision resolved here is not observed again this tick."""
        goal_id = str(snap.get("goal_id") or task.get("goal_id") or "")
        task_id = str(task.get("task_id") or "")
        fresh = self.layer.get_goal(goal_id)
        if fresh.get("ok") and isinstance(fresh.get("goal"), Mapping):
            snap = fresh["goal"]
            found = next(
                (
                    row
                    for row in (snap.get("tasks") or [])
                    if isinstance(row, Mapping) and str(row.get("task_id") or "") == task_id
                ),
                None,
            )
            if found is None or str(found.get("status") or "") != "running":
                return None
            task = found
        existing_decision = next(
            (
                d
                for d in (snap.get("pending_decisions") or [])
                if isinstance(d, Mapping) and str(d.get("task_id") or "") == task_id
            ),
            None,
        )
        if existing_decision is not None:
            return self._continue_pending_decision(snap, task, existing_decision)
        run_id = str(task.get("run_id") or "").strip()
        claim = str(task.get("dispatch_claim") or "")
        if not run_id:
            if claim and claim in self._inflight_claims:
                return None
            return self._fail_task(
                goal_id,
                task_id,
                {"ok": False, "error": "running task has no persisted run handle"},
            )
        try:
            pending_code, pending = self.backend.list_pending_actions(session_id=run_id)
        except Exception as e:
            pending_code, pending = 503, []
            pending_error = f"pending action scan failed: {type(e).__name__}"
        else:
            pending_error = ""
        if int(pending_code) >= 300:
            return self._fail_task(
                goal_id,
                task_id,
                {"ok": False, "run_id": run_id, "error": pending_error or "pending action scan failed"},
            )
        if pending:
            try:
                early = self.backend.observe_run(run_id)
            except Exception:
                early = None
            if isinstance(early, Mapping):
                self._note_task_progress(
                    goal_id,
                    task_id,
                    early,
                    workspace=str(task.get("workspace") or ""),
                )
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
                        str(row.get("kind") or "") for row in pending if isinstance(row, Mapping)
                    ],
                }
            request_id = str(action.get("request_id") or "").strip()
            if not request_id:
                return self._fail_task(
                    goal_id,
                    task_id,
                    {"ok": False, "run_id": run_id, "error": "backend decision has no request_id"},
                )
            backend_kind = str(action.get("kind") or "permission")
            public_kind = {
                "permission": "action_approval",
                "question": "question",
                "review": "artifact_review",
                "system_action": "system_action_approval",
            }[backend_kind]
            decision_id = f"dec_{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:12]}"
            decision_title = f"TeleAgent {backend_kind}"
            decision_details: dict[str, Any] = {
                "backend_kind": backend_kind,
                "backend_request_id": request_id,
                "context_hash": action.get("context_hash"),
                "payload": action.get("payload"),
            }
            if backend_kind == "review":
                contamination_summary = review_contamination_summary(action.get("payload"))
                if contamination_summary:
                    decision_title = "TeleAgent review (CONTAMINATED)"
                    decision_details["summary"] = contamination_summary
            if backend_kind == "permission" and isinstance(action.get("scope"), list):
                decision_details["scope"] = action.get("scope")
                try:
                    from win_collab.core import format_permission_scope_summary

                    scope_summary = format_permission_scope_summary(
                        action.get("payload") or {},
                        action.get("scope"),
                    )
                except Exception:
                    scope_summary = None
                if scope_summary:
                    decision_details["summary"] = scope_summary
            opened = self.layer.open_decision(
                goal_id,
                kind=public_kind,
                task_id=task_id,
                run_id=run_id,
                decision_id=decision_id,
                request_id=request_id,
                actions=[{"request_id": request_id, "kind": backend_kind}],
                title=decision_title,
                return_to_upper=False,
                details=decision_details,
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
            if not isinstance(observation, Mapping):
                raise TypeError("observe_run did not return an object")
        except Exception as e:
            result = {
                "ok": False,
                "run_id": run_id,
                "error": f"backend resume failed: {type(e).__name__}",
            }
            return self._finish_gated(snap, task, result)
        return self._after_observe(snap, task, observation, observe_error="backend resume failed")

    def _dispatch_one(self, goal_id: str, task_id: str) -> dict[str, Any] | None:
        """Claim, handoff, start_run outside the coordinator lock, then bind.

        The claim token stays in ``_inflight_claims`` until this method returns,
        and only while the coordinator lock is held around the add/discard.
        ``start_run`` is the only section that drops the lock.
        """
        token = f"claim_{uuid.uuid4().hex[:12]}"
        self._inflight_claims.add(token)
        try:
            claimed = self.layer.claim_task_dispatch(goal_id, task_id, claim=token)
            if not claimed.get("ok"):
                return None
            task = dict(claimed.get("task") or {})
            current = self.layer.get_goal(goal_id)
            snap = current.get("goal") if current.get("ok") else {}
            if not isinstance(snap, Mapping):
                snap = {}
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            directory = dispatch_directory(task, workspaces_root=self.workspaces_root, goal_id=goal_id)
            root = Path(directory)
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return self._fail_task(
                    goal_id,
                    task_id,
                    {"ok": False, "error": f"backend dispatch failed: {type(exc).__name__}"},
                )
            try:
                staged = handoff_direct_dependency_artifacts(
                    task=task,
                    siblings=tasks,
                    dest_root=root,
                    workspaces_root=self.workspaces_root,
                )
            except HandoffError as exc:
                return self._fail_task(
                    goal_id,
                    task_id,
                    {"ok": False, "error": f"dependency handoff failed: {exc}"},
                )
            if staged:
                inputs = dict(task.get("inputs") or {})
                inputs["input_files"] = [row["relative"] for row in staged]
                task["inputs"] = inputs
            goal_obj = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
            try:
                merge_manifest_input_files(task, goal_obj, root)
            except InputManifestError as exc:
                return self._fail_task(goal_id, task_id, {"ok": False, "error": str(exc)})
            try:
                charter = worker_charter_for_task(
                    goal=goal_obj, task=task, siblings=list(snap.get("tasks") or [])
                )
            except AppError as exc:
                return self._fail_task(goal_id, task_id, {"ok": False, "error": str(exc)})
            try:
                normalized = contract_render.normalize_worker_contract(charter)
                contract_render.render_contract_section(normalized)
            except contract_render.ContractRenderError as exc:
                return self._fail_task(
                    goal_id,
                    task_id,
                    {"ok": False, "error": f"contract_render_error: {exc}"},
                )
            self._lock.release()
            try:
                try:
                    launched = self.backend.start_run(
                        title=str(task.get("title") or task_id),
                        directory=directory,
                        instruction=str((task.get("inputs") or {}).get("instruction") or ""),
                        artifacts=[str(x) for x in (task.get("expected_artifacts") or [])],
                        charter=charter,
                    )
                except contract_render.ContractRenderError as exc:
                    launched = {"ok": False, "error": f"contract_render_error: {exc}"}
                except Exception as exc:
                    detail = sanitize_reason(str(exc))[:300]
                    if detail:
                        error = f"backend dispatch failed: {type(exc).__name__}: {detail}"
                    else:
                        error = f"backend dispatch failed: {type(exc).__name__}"
                    launched = {"ok": False, "error": error, "error_source": "spawn"}
            finally:
                self._lock.acquire()
            if not isinstance(launched, Mapping):
                launched = {"ok": False, "error": "backend dispatch failed: bad result"}
            run_id = str(launched.get("run_id") or launched.get("native_handle") or "")
            if not launched.get("ok") or not run_id:
                return self._fail_task(goal_id, task_id, dict(launched))
            bound = self.layer.bind_task_run(
                goal_id,
                task_id,
                run_id=run_id,
                native_handle=str(launched.get("native_handle") or ""),
                backend=str(launched.get("backend") or getattr(self.backend, "backend_id", "")),
                workspace=directory,
            )
            if not bound.get("ok"):
                return {"_return": bound}
            self.layer.mark_run_started(goal_id, task_id, started_at=self.now())
            bound_task = bound.get("task") if isinstance(bound.get("task"), Mapping) else {}
            if bound_task.get("workspace"):
                task["workspace"] = bound_task.get("workspace")
            task["run_id"] = run_id
            task["status"] = "running"
            fresh = self.layer.get_goal(goal_id)
            if fresh.get("ok") and isinstance(fresh.get("goal"), Mapping):
                snap = fresh["goal"]
            # Pending actions are scanned on a later tick. This tick only
            # distinguishes a still-busy run from one that already finished,
            # so a successor can start without waiting a tick.
            return self._observe_fresh_run(snap, task)
        finally:
            self._inflight_claims.discard(token)

    def _apply_agy_acceptance(self, snap: Mapping[str, Any], task: Mapping[str, Any], result: Any) -> Any:
        """Shared agy gate. Other backends keep contamination-only gating."""
        if not isinstance(result, dict):
            return result
        backend_id = str(result.get("backend") or getattr(self.backend, "backend_id", "") or "")
        if not backend_id.startswith("antigravity"):
            return result
        # Exception stubs (resume/observe failure) have no workspace. Do not
        # overwrite that error with an acceptance miss on a file that was never collected.
        if "workspace" not in result and "artifacts" not in result:
            return result
        goal = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
        try:
            charter = worker_charter_for_task(
                goal=goal, task=task, siblings=list(snap.get("tasks") or [])
            )
        except AppError as exc:
            failed = dict(result)
            failed["ok"] = False
            failed["error"] = str(exc)
            failed["review"] = {
                "status": "failed",
                "source": "none",
                "evidence": str(exc)[:240],
            }
            return failed
        workdir = result.get("workspace")
        if not isinstance(workdir, str) or not workdir.strip():
            workdir = str(
                scheduler_task_workspace(
                    self.workspaces_root,
                    str(snap.get("goal_id") or task.get("goal_id") or ""),
                    str(task.get("task_id") or ""),
                )
            )
        from execution_backend.antigravity_cli_v1 import apply_agy_acceptance_gate

        return apply_agy_acceptance_gate(charter=charter, workdir=workdir, result=result)

    def _trusted_workspace_text(self, task: Mapping[str, Any]) -> str:
        """Scheduler directory for ``task``. Empty when it cannot be named.

        Prefers the value persisted at dispatch. Otherwise the deterministic
        scheduler path, and only when that path is already a real directory.
        """
        raw = task.get("workspace")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        gid = str(task.get("goal_id") or "").strip()
        tid = str(task.get("task_id") or "").strip()
        if not gid or not tid:
            return ""
        candidate = scheduler_task_workspace(self.workspaces_root, gid, tid)
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                return ""
            if hasattr(candidate, "is_junction") and candidate.is_junction():
                return ""
        except OSError:
            return ""
        return str(candidate)

    def _with_trusted_workspace(self, task: Mapping[str, Any], result: Any) -> Any:
        """Force ``result["workspace"]`` to the scheduler directory.

        Exception stubs (no artifacts and no workspace) are left alone so an
        observation failure is not rewritten as an acceptance miss. A backend
        workspace that differs from the trusted directory is ignored.
        """
        if not isinstance(result, dict):
            return result
        reported = result.get("workspace")
        reported_text = reported.strip() if isinstance(reported, str) else ""
        if "artifacts" not in result and not reported_text:
            return result
        trusted = self._trusted_workspace_text(task)
        if not trusted:
            return result
        if reported_text and workspace_paths_match(reported_text, trusted):
            return result
        out = dict(result)
        out["workspace"] = trusted
        return out

    def _finish_gated(self, snap: Mapping[str, Any], task: Mapping[str, Any], result: Any) -> dict[str, Any]:
        """Acceptance gate, then contamination scan, then finish_task.

        Agy exact-content failures stay failures. Prose acceptance that no
        reviewer checked is a warning, not a pass. Contamination still runs
        when the result is otherwise ok.
        """
        result = self._with_trusted_workspace(task, result)
        result = self._apply_agy_acceptance(snap, task, result)
        result = _gate_backend_result(snap, task, result)
        result = self._attach_usage_report(result)
        parked = self._post_hoc_budget(snap, task, result)
        if parked is not None:
            return parked
        goal_id = str(snap.get("goal_id") or "")
        if isinstance(result, Mapping):
            warns = [
                str(item).strip()
                for item in (result.get("warnings") or [])
                if isinstance(item, str) and str(item).strip()
            ]
            if warns and goal_id:
                self.layer.append_goal_warnings(goal_id, warns)
        finished = self.layer.finish_task(
            goal_id,
            str(task["task_id"]),
            succeeded=bool(isinstance(result, Mapping) and result.get("ok")),
            close_goal=False,
            result=result if isinstance(result, Mapping) else {"ok": False, "error": "backend result was not an object"},
        )
        reconciled = self.layer.reconcile_goal_progress(goal_id) if goal_id else {}
        state = reconciled.get("state") or finished.get("state")
        out = {**finished, "action": "task_finished"}
        if state:
            out["state"] = state
        return out

    def lead_will_decide(self, decision: Mapping[str, Any]) -> bool:
        """Whether the next tick resolves ``decision`` by the lead, not a human."""
        return _lead_will_decide(self.planner, self.backend, decision)

    def _continue_pending_decision(
        self,
        snap: Mapping[str, Any],
        task: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        details = _decision_details(decision)
        backend_kind = str(details.get("backend_kind") or "")
        if not self.lead_will_decide(decision):
            required: dict[str, Any] = {
                "ok": True,
                "goal_id": snap.get("goal_id"),
                "state": "running",
                "action": "decision_required",
                "decision_id": decision.get("decision_id"),
            }
            # Capable but exhausted: surface the lead error. Incapable kinds
            # (question / system_action, or no decide/resolve hooks) stay quiet.
            if _lead_auto_resolve_capable(self.planner, self.backend, backend_kind):
                err = _pending_lead_error(decision)
                required["lead_error"] = err or {"code": "lead_exhausted", "retryable": False}
            return required
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
        if not _lead_auto_resolve_capable(self.planner, self.backend, backend_kind):
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



def _run_win_gui_doctor(
    *,
    simulated: bool = False,
    simulate_status: str | None = None,
    port_open_fn: Callable[[str, int], bool] | None = None,
    creds_presence_fn: Callable[[], Any] | None = None,
) -> Any:
    from teleagent_adapter.doctor import doctor

    return doctor(
        base_url="http://127.0.0.1:4399",
        platform="win32",
        simulated=simulated,
        simulate_status=simulate_status,
        port_open_fn=port_open_fn,
        creds_presence_fn=creds_presence_fn,
    )


def _probe_from_doctor_report(report: Any) -> dict[str, Any]:
    from teleagent_adapter.base import AdapterStatus

    status = str(getattr(report, "status", "") or "")
    ok = status == AdapterStatus.OK.value
    details = [str(x) for x in (getattr(report, "details", None) or [])]
    detail = "; ".join(details[:3])
    reason = sanitize_reason(detail or status or "connection_not_ready")
    return {
        "ok": ok,
        "status": status,
        "reason": reason if not ok else "",
        "base_url": getattr(report, "base_url", "") or "",
        "simulated": bool(getattr(report, "simulated", False)),
        "details": [sanitize_reason(x) for x in details],
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
    ``details`` is additive; ``ok`` / ``status`` / ``reason`` / ``base_url`` / ``simulated`` stay the gate.
    """
    report = _run_win_gui_doctor(
        simulated=simulated,
        simulate_status=simulate_status,
        port_open_fn=port_open_fn,
        creds_presence_fn=creds_presence_fn,
    )
    return _probe_from_doctor_report(report)


_HOLDER_KEYS = ("controller_id", "pid", "state_dir", "base_url")


def _readonly_creds_presence() -> Any:
    """Doctor creds probe with the wrap channel forced off.

    Daily readiness must not spawn stdin_wrap. Presence still uses process env
    and foreign environ reads; a missing secret stays missing.
    """
    import os

    from teleagent_adapter.windows_process_environ import (
        WIN_CREDS_CHANNEL_ENV,
        probe_windows_creds_presence,
    )

    env = dict(os.environ)
    env[WIN_CREDS_CHANNEL_ENV] = "off"
    return probe_windows_creds_presence(environ=env)


def _public_lock_holder(raw: Any) -> dict[str, Any] | None:
    """Holder JSON is diagnostic and may be stale. Never echo secret-shaped extras."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in _HOLDER_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        out[key] = value
    return out or None


def _fetch_session_status_readonly(base_url: str) -> Any:
    """GET /session/status via the existing win_collab client.

    Does not POST /session, claim the desktop lock, or start stdin_wrap.
    """
    from win_collab.client import Client, discover

    discovered_base, creds = discover()
    base = str(base_url or discovered_base).strip().rstrip("/")
    client = Client(base=base, creds=creds)
    return client.call("GET", "/session/status")


def _read_lock_holder_readonly(base_url: str) -> dict[str, Any] | None:
    from win_collab.desktop_lock import read_holder

    return read_holder(base_url)


def _classify_desktop_occupancy(status_obj: Any) -> dict[str, Any]:
    """Whole-desktop idle | busy | unknown from the strict session parsers.

    glue.parse_session_activity is the three-state parser. foreign_desktop_busy
    is the dispatch fail-closed check (no owned sessions). They must agree
    before the desktop is reported idle.
    """
    import glue as g

    session_types: dict[str, int] = {}
    session_count = 0
    state = "unknown"
    if isinstance(status_obj, dict):
        activities: list[str] = []
        for key, val in status_obj.items():
            session_count += 1
            label = "invalid"
            if isinstance(val, dict):
                raw = val.get("type") if val.get("type") is not None else val.get("status")
                label = str(raw) if raw is not None else "missing"
            session_types[label] = session_types.get(label, 0) + 1
            activities.append(g.parse_session_activity(status_obj, str(key)))
        if not status_obj:
            probe = g.parse_session_activity(status_obj, "_ready_probe")
            state = "idle" if probe == "idle" else "unknown"
        elif any(item == "busy" for item in activities):
            state = "busy"
        elif any(item != "idle" for item in activities):
            state = "unknown"
        else:
            state = "idle"
        if state == "idle":
            blocked = True
            try:
                from win_collab.desktop_lock import foreign_desktop_busy

                blocked = bool(foreign_desktop_busy(status_obj, ()))
            except Exception:
                blocked = True
            if blocked:
                state = "unknown"
    return {
        "state": state,
        "session_count": session_count,
        "session_types": session_types,
    }


def _occupancy_block_reasons(state: str, *, detail: str = "") -> list[str]:
    if state == "busy":
        lines = [
            "DO NOT DISPATCH: desktop session occupancy is busy",
            "不要派工：桌面 session 正忙",
        ]
    else:
        lines = [
            "DO NOT DISPATCH: desktop session occupancy is unknown",
            "不要派工：无法确认桌面 session 是否空闲",
        ]
    cleaned = sanitize_reason(detail)
    if cleaned:
        lines.append(cleaned)
    return lines


# src/framework/app_service.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_RUNNING_TIP_NAME = "running_tip.json"
_TIP_MISMATCH_ZH = "不要派工：运行 tip 与磁盘 HEAD 不一致，请重启 collab-service 后再派工"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

# In-memory tip of this process. Captured once at import so a later HEAD move
# cannot be mistaken for the code this process already loaded.
_PROCESS_START_TIP = ""


def repo_root() -> Path:
    return _REPO_ROOT


def default_running_tip_path(persist_root: str | Path | None = None) -> Path:
    if persist_root is None:
        return _REPO_ROOT / ".collab-app" / _DEFAULT_RUNNING_TIP_NAME
    return Path(persist_root) / _DEFAULT_RUNNING_TIP_NAME


def read_git_head(repo_root: str | Path | None = None) -> str:
    """``git rev-parse HEAD`` in ``repo_root``. Empty string when unknown."""
    root = _REPO_ROOT if repo_root is None else Path(repo_root)
    run_kwargs: dict[str, Any] = {
        "cwd": str(root),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 15,
        "check": False,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], **run_kwargs)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def process_start_tip() -> str:
    """Tip loaded with this process. Stable for the life of the process."""
    return _PROCESS_START_TIP


def _coerce_tip(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _invoke_tip(fn: Callable[[], Any]) -> str:
    try:
        return _coerce_tip(fn())
    except Exception:
        return ""


def _pid_is_alive(pid: Any) -> bool:
    """True when ``pid`` is a live process.

    Windows ``os.kill(pid, 0)`` terminates the process via TerminateProcess.
    Query with PROCESS_QUERY_LIMITED_INFORMATION and never signal it.
    Access denied still counts as alive so a live service is not ignored.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def _fallback_running_tip() -> str:
    started = process_start_tip().strip()
    if started:
        return started
    return read_git_head(_REPO_ROOT)


def read_running_tip_for_ready(tip_path: str | Path | None = None) -> str:
    """Tip of a live service recorded on disk, else this process's start tip.

    A missing file, a dead pid, or an unreadable record means no live stale
    service. An empty tip on a still-live pid is returned as empty so the
    compare fails closed.
    """
    path = default_running_tip_path() if tip_path is None else Path(tip_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _fallback_running_tip()
    if not isinstance(raw, dict):
        return _fallback_running_tip()
    pid = raw.get("pid")
    if not _pid_is_alive(pid):
        return _fallback_running_tip()
    return _coerce_tip(raw.get("tip"))


def write_running_tip(
    tip_path: str | Path,
    *,
    tip: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """Record this service process's tip. Caller must not commit the file."""
    path = Path(tip_path)
    recorded = process_start_tip() if tip is None else _coerce_tip(tip)
    recorded_pid = os.getpid() if pid is None else int(pid)
    body = {"tip": recorded, "pid": recorded_pid}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
    return body


def _format_tip_mismatch(running_tip: str, head_tip: str) -> str:
    running = running_tip.strip() or "unknown"
    head = head_tip.strip() or "unknown"
    return (
        f"DO NOT DISPATCH: running tip <{running}> != HEAD <{head}> "
        "(restart collab-service to align)"
    )


def assess_tip_consistency(
    *,
    running_tip: str | None = None,
    head_tip: str | None = None,
    running_tip_fn: Callable[[], Any] | None = None,
    head_tip_fn: Callable[[], Any] | None = None,
    tip_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fail closed unless the running tip and ``HEAD`` are the same non-empty SHA."""
    if running_tip is None:
        running = _invoke_tip(running_tip_fn) if running_tip_fn is not None else read_running_tip_for_ready(tip_path)
    else:
        running = _coerce_tip(running_tip)
    if head_tip is None:
        head = _invoke_tip(head_tip_fn) if head_tip_fn is not None else read_git_head(_REPO_ROOT)
    else:
        head = _coerce_tip(head_tip)
    ok = bool(running) and bool(head) and running == head
    return {
        "ok": ok,
        "running_tip": running,
        "head_tip": head,
        "reason": "" if ok else _format_tip_mismatch(running, head),
    }


def tip_mismatch_lines(assessment: Mapping[str, Any]) -> list[str]:
    reason = str(assessment.get("reason") or "").strip()
    if not reason:
        return []
    lines = [reason]
    if _TIP_MISMATCH_ZH not in reason:
        lines.append(_TIP_MISMATCH_ZH)
    return lines


def _refuse_dispatch_if_tip_mismatch() -> None:
    """This process's start tip vs the checkout HEAD. Raises AppError on drift."""
    assessment = assess_tip_consistency(
        running_tip=process_start_tip(),
        head_tip=read_git_head(_REPO_ROOT),
    )
    if assessment["ok"]:
        return
    raise AppError(
        "\n".join(tip_mismatch_lines(assessment)),
        status=409,
        code="running_tip_mismatch",
    )


_PROCESS_START_TIP = read_git_head(_REPO_ROOT)


def assess_win_gui_readiness(
    *,
    simulated: bool = False,
    simulate_status: str | None = None,
    port_open_fn: Callable[[str, int], bool] | None = None,
    creds_presence_fn: Callable[[], Any] | None = None,
    session_status_fn: Callable[[str], Any] | None = None,
    lock_holder_fn: Callable[[str], Any] | None = None,
    running_tip: str | None = None,
    head_tip: str | None = None,
    running_tip_fn: Callable[[], Any] | None = None,
    head_tip_fn: Callable[[], Any] | None = None,
    tip_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only daily gate: doctor + /session/status + lock-holder metadata.

    Does not start stdin_wrap, create a session, claim or steal the desktop
    lock, or stop the GUI. ``busy``, ``unknown``, and a running tip that
    differs from HEAD fail closed.
    Lock-holder JSON is reported even when stale and does not by itself allow
    or deny dispatch.
    """
    report = _run_win_gui_doctor(
        simulated=simulated,
        simulate_status=simulate_status,
        port_open_fn=port_open_fn,
        creds_presence_fn=creds_presence_fn or _readonly_creds_presence,
    )
    gui = _probe_from_doctor_report(report)
    base_url = str(gui.get("base_url") or "")
    holder_fn = lock_holder_fn or _read_lock_holder_readonly
    lock_holder: dict[str, Any] | None = None
    if base_url:
        try:
            lock_holder = _public_lock_holder(holder_fn(base_url))
        except Exception:
            lock_holder = None

    occupancy: dict[str, Any]
    reasons: list[str]
    if not gui.get("ok"):
        occupancy = {"state": "unknown", "session_count": 0, "session_types": {}}
        reasons = _occupancy_block_reasons("unknown", detail=str(gui.get("reason") or gui.get("status") or ""))
    else:
        fetch = session_status_fn or _fetch_session_status_readonly
        try:
            status_obj = fetch(base_url)
        except Exception as exc:
            occupancy = {"state": "unknown", "session_count": 0, "session_types": {}}
            reasons = _occupancy_block_reasons(
                "unknown",
                detail=f"session status read failed: {type(exc).__name__}: {exc}",
            )
        else:
            occupancy = _classify_desktop_occupancy(status_obj)
            if occupancy["state"] == "idle":
                reasons = []
            else:
                reasons = _occupancy_block_reasons(str(occupancy["state"]))

    tip = assess_tip_consistency(
        running_tip=running_tip,
        head_tip=head_tip,
        running_tip_fn=running_tip_fn,
        head_tip_fn=head_tip_fn,
        tip_path=tip_path,
    )
    if not tip["ok"]:
        reasons.extend(tip_mismatch_lines(tip))
    dispatch_allowed = bool(gui.get("ok")) and occupancy["state"] == "idle" and bool(tip["ok"])
    ready = dispatch_allowed
    return {
        "ready": ready,
        "gui": {
            "status": gui.get("status") or "",
            "ok": bool(gui.get("ok")),
            "reason": gui.get("reason") or "",
            "simulated": bool(gui.get("simulated")),
            "details": list(gui.get("details") or []),
        },
        "base_url": base_url,
        "occupancy": occupancy,
        "session_count": occupancy["session_count"],
        "session_types": occupancy["session_types"],
        "lock_holder": lock_holder,
        "dispatch_allowed": dispatch_allowed,
        "reasons": reasons,
        "running_tip": tip["running_tip"],
        "head_tip": tip["head_tip"],
        "tip_ok": bool(tip["ok"]),
    }



def public_pending_decisions(rows: Any) -> list[dict[str, Any]]:
    """Stable public shape for status/events: kind includes system_action_approval.

    ``lead_error`` is copied onto the public row when the durable decision stored
    it at the top level or under ``details`` (annotate_decision writes both).
    """
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        item = {
            "decision_id": str(row.get("decision_id") or ""),
            "request_id": str(row.get("request_id") or ""),
            "kind": str(row.get("kind") or ""),
            "title": str(row.get("title") or ""),
            "reason": str(row.get("reason") or ""),
            "task_id": str(row.get("task_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "status": str(row.get("status") or ""),
            "backend_kind": str(details.get("backend_kind") or ""),
            "backend_request_id": str(details.get("backend_request_id") or ""),
            "details": dict(details),
        }
        lead_error = row.get("lead_error")
        if not isinstance(lead_error, Mapping):
            lead_error = details.get("lead_error")
        if isinstance(lead_error, Mapping) and lead_error:
            item["lead_error"] = dict(lead_error)
        out.append(item)
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
        max_parallel_per_goal: int = 2,
        max_parallel_global: int = 4,
        stale_after_sec: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.persist_root = Path(persist_root)
        self.layer = DurableLayer.open(self.persist_root, use_cache=False)
        self.coordinator_id = coordinator_id
        self.max_parallel_per_goal = as_limit(max_parallel_per_goal) or 2
        self.max_parallel_global = as_limit(max_parallel_global) or 4
        self.coordinator = AppCoordinator(
            self.layer,
            planner=planner,
            backend=backend,
            workspaces_root=self.persist_root / "workspaces",
            max_parallel_per_goal=self.max_parallel_per_goal,
            max_parallel_global=self.max_parallel_global,
            stale_after_sec=stale_after_sec,
            clock=clock,
        )
        self._connection_probe = connection_probe

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _refuse_dispatch_if_tip_mismatch()
        if not isinstance(payload, Mapping):
            raise AppError("request body must be a JSON object")
        goal_text = str(payload.get("goal") or payload.get("desired_outcome") or "").strip()
        if not goal_text:
            raise AppError("goal is required")
        acceptance = payload.get("acceptance")
        if not isinstance(acceptance, Mapping):
            raise AppError("acceptance object is required")
        if "text" in acceptance and not isinstance(acceptance.get("text"), str):
            raise AppError("acceptance.text must be a string")
        unknown_acceptance = sorted(str(k) for k in acceptance if k not in GOAL_ACCEPTANCE_KEYS)
        if unknown_acceptance:
            # An unknown criterion would be dropped before the worker and never
            # checked; refuse it instead of pretending it is part of the contract.
            raise AppError("acceptance has unknown keys: " + ", ".join(unknown_acceptance))
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
        budget = budget_from_payload(payload)
        submit_key = str(payload.get("idempotency_key") or payload.get("request_id") or "").strip()
        if not submit_key:
            submit_key = f"req_{uuid.uuid4().hex}"
        client_id = str(payload.get("client_id") or "local-api").strip() or "local-api"
        title = str(payload.get("title") or goal_text[:80]).strip()
        forbidden_tools = project_forbidden_tools(payload=payload)
        external_inputs = project_external_inputs(payload)
        try:
            input_manifest = project_input_manifest(payload)
        except InputManifestError as exc:
            raise AppError(str(exc), code=exc.code) from exc
        required = parse_required_capabilities(payload)
        acknowledge_prompt_only = parse_acknowledge_prompt_only(payload)
        # Capability refusals happen before submit_goal so a 409 does not bind
        # the idempotency key or leave a Goal to dispatch.
        caps = self.capabilities()
        missing = unmet_capabilities(caps, required)
        if missing:
            raise AppError(
                "required capabilities are not available on this backend",
                status=409,
                code="capability_unavailable",
                extra={"missing": missing, "capabilities": caps},
            )
        if external_inputs and prompt_only_skip_blocked(caps) and not acknowledge_prompt_only:
            raise AppError(
                "pinned external inputs are prompt-only while this backend runs with "
                "skip-permissions; set acknowledge_prompt_only_inputs true to record the downgrade",
                status=409,
                code="capability_unavailable",
                extra={"missing": ["external_input_enforcement"], "capabilities": caps},
            )
        budget_missing, budget_warnings = budget_submit_issues(budget, caps)
        if budget_missing:
            raise AppError(
                "budget field is not enforceable on this backend",
                status=409,
                code="capability_unavailable",
                extra={"missing": budget_missing, "capabilities": caps},
            )
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
        if external_inputs:
            goal["external_inputs"] = external_inputs
        if input_manifest:
            goal["input_manifest"] = input_manifest
        submit_warnings: list[str] = []
        if external_inputs and prompt_only_skip_blocked(caps) and acknowledge_prompt_only:
            submit_warnings.append(SKIP_PERMISSIONS_WARNING)
        for text in budget_warnings:
            if text not in submit_warnings:
                submit_warnings.append(text)
        if submit_warnings:
            goal["warnings"] = submit_warnings
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

    def _publish_pending(self, rows: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Public pending rows tagged ``awaiting`` lead or human, plus counts.

        Uses the coordinator's planner and backend, the same predicate as
        ``AppCoordinator.lead_will_decide``. ``pending_decisions`` /
        ``pending_decision_count`` / ``awaiting_decision`` stay unchanged.
        """
        pending = public_pending_decisions(rows)
        for row in pending:
            row["awaiting"] = "lead" if self.coordinator.lead_will_decide(row) else "human"
        return pending, {
            "awaiting_lead_count": sum(1 for row in pending if row.get("awaiting") == "lead"),
            "awaiting_human_count": sum(1 for row in pending if row.get("awaiting") == "human"),
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
        pending, awaiting_counts = self._publish_pending(snap.get("pending_decisions") or [])
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
            **awaiting_counts,
            "failure": failure,
            "need_human": bool(nh_view.get("need_human")),
            "failure_reason": nh_view.get("failure_reason") or "",
            "updated_at": snap.get("updated_at_iso"),
        }
        caps = self.capabilities()
        backend = caps.get("backend") if isinstance(caps.get("backend"), Mapping) else {}
        planner = caps.get("planner") if isinstance(caps.get("planner"), Mapping) else {}
        out["warnings"] = goal_status_warnings(snap, caps)
        out["capabilities_ref"] = {
            "backend": str(backend.get("id") or ""),
            "planner": str(planner.get("name") or ""),
        }
        out["scheduler"] = self.coordinator.scheduler_snapshot(goal_id, snap, caps)
        raw_budget_status = snap.get("budget_status")
        out["budget_status"] = dict(raw_budget_status) if isinstance(raw_budget_status, Mapping) else {}
        out["progress"] = self.coordinator.goal_progress_view(snap, out["scheduler"], caps)
        out["acceptance_status"] = acceptance_status_view(
            str(out.get("state") or ""), tasks if isinstance(tasks, list) else [], caps
        )
        # Pending decisions are not task failures. Observation-window timeouts
        # never reach this method; they are a client wait, not a goal state.
        if str(out.get("state") or "") == "failed" and not pending:
            projected = project_failed_tasks(tasks if isinstance(tasks, list) else [])
            if projected is not None:
                # need_human already filled failure_reason; do not replace it.
                if not str(out.get("failure_reason") or "").strip():
                    out["failure_reason"] = projected["failure_reason"]
                by_id = {
                    str(t.get("task_id") or ""): t
                    for t in (tasks if isinstance(tasks, list) else [])
                    if isinstance(t, Mapping)
                }
                for brief in projected["failures"]:
                    _annotate_task_failure(brief, by_id.get(brief.get("task_id") or ""))
                out["primary_failure"] = projected["primary_failure"]
                out["failures"] = projected["failures"]
            else:
                goal_failure = goal_level_failure_brief(snap.get("failure"))
                if goal_failure is not None:
                    # Planning/coordination failed before any task result exists.
                    if not str(out.get("failure_reason") or "").strip():
                        out["failure_reason"] = goal_failure["error"]
                    out["primary_failure"] = goal_failure
                    out["failures"] = [goal_failure]
        return out

    def capabilities(self) -> dict[str, Any]:
        """GET /v1/capabilities. No secrets. Planner is this process's planner."""
        doc = compose_capabilities(
            self.coordinator.backend,
            self.coordinator.planner,
            max_parallel_per_goal=self.coordinator.max_parallel_per_goal,
            max_parallel_global=self.coordinator.max_parallel_global,
        )
        doc.update(capability_document())
        return {"ok": True, **doc}

    def health(self) -> dict[str, Any]:
        """Unauthenticated liveness plus backend id and planner name. No secrets."""
        caps = self.capabilities()
        backend = caps.get("backend") if isinstance(caps.get("backend"), Mapping) else {}
        planner = caps.get("planner") if isinstance(caps.get("planner"), Mapping) else {}
        return {
            "ok": True,
            "api_version": API_VERSION,
            "backend": str(backend.get("id") or ""),
            "planner": str(planner.get("name") or ""),
        }

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
        pending, awaiting_counts = self._publish_pending(snap.get("pending_decisions") or [])
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "pending_decisions": pending,
            "pending_decision_count": len(pending),
            "awaiting_decision": bool(pending),
            **awaiting_counts,
        }

    def get_decision(self, goal_id: str, decision_id: str) -> dict[str, Any]:
        """GET …/decisions/{decision_id}: one pending public row."""
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            raise AppError(str(current.get("error")), status=404, code="not_found")
        snap = current.get("goal") if isinstance(current.get("goal"), Mapping) else {}
        pending, _awaiting_counts = self._publish_pending(snap.get("pending_decisions") or [])
        want = str(decision_id or "")
        row = next((item for item in pending if str(item.get("decision_id") or "") == want), None)
        if row is None:
            raise AppError("decision not found", status=404, code="not_found")
        awaiting = str(row.get("awaiting") or "human")
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "decision": row,
            "pending_decision_count": 1,
            "awaiting_decision": True,
            "awaiting_lead_count": 1 if awaiting == "lead" else 0,
            "awaiting_human_count": 0 if awaiting == "lead" else 1,
        }

    def events(self, goal_id: str) -> dict[str, Any]:
        out = self.layer.list_events(goal_id)
        if not out.get("ok"):
            raise AppError(str(out.get("error")), status=404, code="not_found")
        pending, awaiting_counts = self._publish_pending(out.get("pending") or out.get("pending_decisions") or [])
        return {
            **out,
            "pending": pending,
            "pending_decisions": pending,
            "pending_decision_count": len(pending),
            "awaiting_decision": bool(pending),
            **awaiting_counts,
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
        if "linux" in backend_id:
            report = assess_linux_gui_readiness()
            ok = bool(report.get("ready") and report.get("dispatch_allowed"))
            if ok:
                return {"ok": True, "status": "ok", "reason": ""}
            hints = report.get("hints") or report.get("reasons") or []
            reason = sanitize_reason("; ".join(str(item) for item in hints) or "linux gui not ready")
            return {"ok": False, "status": "not_ready", "reason": reason}
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


    def _apply_checkpoint_verdict(self, goal_id: str, decision: Mapping[str, Any]) -> None:
        """Human continue retries once. Stop fails the task and keeps files.

        Continue is not a grant. A second resolve of the same decision is
        idempotent and does not reach this method.
        """
        task_id = str(decision.get("task_id") or "")
        verdict = str(decision.get("verdict_class") or decision.get("verdict") or "")
        if verdict == "continue":
            requeued = self.layer.requeue_checkpoint_task(goal_id, task_id)
            if not requeued.get("ok"):
                raise AppError(
                    str(requeued.get("error") or requeued.get("reason") or "checkpoint continue failed"),
                    status=409,
                    code=str(requeued.get("reason") or "checkpoint_continue_failed"),
                )
            return
        if verdict != "stop":
            return
        current = self.layer.get_goal(goal_id)
        snap = current.get("goal") if isinstance(current.get("goal"), Mapping) else {}
        task = next(
            (
                row
                for row in (snap.get("tasks") or [])
                if isinstance(row, Mapping) and str(row.get("task_id") or "") == task_id
            ),
            None,
        )
        prior = task.get("result") if isinstance(task, Mapping) and isinstance(task.get("result"), Mapping) else {}
        stopped = dict(prior)
        stopped["ok"] = False
        if not str(stopped.get("error") or "").strip():
            stopped["error"] = "checkpoint stop"
        stopped["checkpoint_stopped"] = True
        finished = self.layer.finish_task(
            goal_id,
            task_id,
            succeeded=False,
            close_goal=False,
            result=stopped,
        )
        if not finished.get("ok"):
            raise AppError(
                str(finished.get("error") or finished.get("reason") or "checkpoint stop failed"),
                status=409,
                code=str(finished.get("reason") or "checkpoint_stop_failed"),
            )
        self.layer.reconcile_goal_progress(goal_id)

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
            except AppError:
                raise
            except Exception as e:
                # Contamination keeps counts (no file text). Other ValueError
                # text is controller copy and is sanitized. Unexpected types
                # stay as the type name so a traceback or secret cannot leak.
                if _is_artifact_contaminated(e):
                    raise _artifact_contaminated_error(e) from e
                if isinstance(e, ValueError):
                    raise AppError(
                        "worker decision rejected: " + sanitize_reason(str(e)),
                        status=409,
                        code="worker_decision_rejected",
                    ) from e
                if isinstance(e, BackendError):
                    raise AppError(
                        "worker decision failed: " + sanitize_reason(str(e)),
                        status=409,
                        code="worker_decision_failed",
                    ) from e
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
        decision = out.get("decision") if isinstance(out.get("decision"), Mapping) else {}
        if str(decision.get("kind") or "") == KIND_CHECKPOINT and not out.get("idempotent"):
            self._apply_checkpoint_verdict(goal_id, decision)
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
            self._send(200, self.app.health())
            return
        if parts == ["v1", "capabilities"]:
            if self.command == "GET":
                self._send(200, self.app.capabilities())
            else:
                raise AppError("method not allowed", status=405, code="method_not_allowed")
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
            self._send(e.status, _app_error_body(e))
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
    "project_external_inputs",
    "validate_plan",
    "worker_charter_for_task",
    "allows_aigc_marks",
    "gate_collected_artifacts",
    "review_contamination_summary",
    "task_acceptance_criteria",
    "split_task_musts",
    "lead_error_retryable",
    "TASK_REVIEW_HINT",
    "GOAL_HTTP_TERMINAL",
    "TASK_HTTP_TERMINAL",
    "assess_linux_gui_readiness",
]


def assess_linux_gui_readiness(**kwargs):
    """Linux daily gate. Implemented in ``framework.linux_ready`` (lazy import)."""
    from framework.linux_ready import assess_linux_gui_readiness as _impl

    return _impl(**kwargs)
