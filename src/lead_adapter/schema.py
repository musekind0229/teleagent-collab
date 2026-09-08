"""Structured lead protocol schemas (条3). Decisions are strict JSON bound to application_id."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from typing import Any


PERMISSION_DECISIONS = frozenset({"once", "reject", "deny_job", "demand_safe_path"})
REVIEW_VERDICTS = frozenset({"pass", "fail"})
DECISION_KINDS = frozenset({"permission", "review"})


def new_application_id(prefix: str = "app") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def context_summary_of(payload: dict, *, max_len: int = 240) -> str:
    """Stable short digest so illegal replies cannot be detached from the ask."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    goal = str(payload.get("goal") or payload.get("task_goal") or "")[:80]
    return f"{digest}:{goal}"[:max_len]


def build_lead_request(
    *,
    kind: str,
    goal: str,
    authorized_scope: list | str | None,
    prohibitions: list | str | None,
    acceptance_criteria: Any,
    current_application: dict,
    application_id: str | None = None,
    charter: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """Every lead call carries full context — do not assume the process remembers prior turns."""
    if kind not in DECISION_KINDS:
        raise ValueError(f"kind must be one of {sorted(DECISION_KINDS)}")
    app_id = application_id or new_application_id(kind[:3])
    scope = authorized_scope
    if scope is None and charter:
        scope = charter.get("must") or charter.get("allowed_surfaces") or []
    prohib = prohibitions
    if prohib is None and charter:
        prohib = charter.get("must_not") or []
    accept = acceptance_criteria
    if accept is None and charter:
        accept = charter.get("acceptance") or charter.get("done_when") or {}
    goal_s = goal or (charter or {}).get("goal") or ""
    body = {
        "protocol": "collab-lead-v1",
        "kind": kind,
        "application_id": app_id,
        "task_goal": goal_s,
        "authorized_scope": scope if scope is not None else [],
        "prohibitions": prohib if prohib is not None else [],
        "acceptance_criteria": accept if accept is not None else {},
        "current_application": current_application or {},
        "issued_at": time.time(),
    }
    if extra:
        body["extra"] = extra
    body["context_summary"] = context_summary_of(
        {
            "kind": kind,
            "application_id": app_id,
            "goal": goal_s,
            "scope": body["authorized_scope"],
            "prohibitions": body["prohibitions"],
            "acceptance": body["acceptance_criteria"],
            "current": current_application,
        }
    )
    return body


def lead_permission_response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "application_id": {"type": "string"},
            "context_summary": {"type": "string"},
            "decision": {"type": "string", "enum": sorted(PERMISSION_DECISIONS)},
            "reason": {"type": "string"},
            "safe_path_hint": {"type": "string"},
        },
        "required": ["application_id", "decision", "reason"],
        "additionalProperties": False,
    }


def lead_review_response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "application_id": {"type": "string"},
            "context_summary": {"type": "string"},
            "verdict": {"type": "string", "enum": sorted(REVIEW_VERDICTS)},
            "reason": {"type": "string"},
        },
        "required": ["application_id", "verdict", "reason"],
        "additionalProperties": False,
    }


def pin_lead_response_schema(schema: dict, request: dict) -> dict:
    """Deep-copy schema and pin application_id / context_summary to exact request values.

    Used so CLI `--json-schema` const-constrains echo. Does not invent ids:
    const is the request's exact string (empty stays empty; validation still rejects).
    """
    if isinstance(schema, dict):
        pinned = copy.deepcopy(schema)
    else:
        pinned = {"type": "object", "properties": {}}
    props = pinned.get("properties")
    if not isinstance(props, dict):
        props = {}
        pinned["properties"] = props
    req = request if isinstance(request, dict) else {}

    app_id = req.get("application_id")
    app_id_s = "" if app_id is None else str(app_id)
    props["application_id"] = {"type": "string", "const": app_id_s}

    required = pinned.get("required")
    if not isinstance(required, list):
        required = []
    else:
        required = list(required)
    if "application_id" not in required:
        required.append("application_id")

    summary = req.get("context_summary")
    if summary is not None:
        props["context_summary"] = {"type": "string", "const": str(summary)}
    elif "context_summary" not in props:
        props["context_summary"] = {"type": "string"}
    if "context_summary" not in required:
        required.append("context_summary")

    pinned["required"] = required
    return pinned


def format_lead_request_prompt(request: dict, *, allow_hint: str = "") -> str:
    """Stateless prompt: full goal/scope/prohibitions/acceptance + current ask."""
    kind = request.get("kind")
    app_id = request.get("application_id")
    summary = request.get("context_summary")
    app_id_json = json.dumps("" if app_id is None else str(app_id), ensure_ascii=False)
    summary_json = json.dumps("" if summary is None else str(summary), ensure_ascii=False)
    echo_rules = (
        "Copy application_id and context_summary EXACTLY byte-for-byte from the request "
        "fields above (and from Full request JSON). No rewrite, no whitespace normalize, "
        "no commentary, no punctuation changes. "
        "reason/decision/verdict may vary; application_id and context_summary MUST be "
        "copied verbatim from the request fields above."
    )
    if kind == "permission":
        out_hint = (
            "Output strict JSON only: "
            "{"
            f'"application_id":{app_id_json},'
            f'"context_summary":{summary_json},'
            '"decision":"once|reject|deny_job|demand_safe_path",'
            '"reason":"..."'
            "} "
            f"{echo_rules} Never choose always."
        )
    else:
        out_hint = (
            "Output strict JSON only: "
            "{"
            f'"application_id":{app_id_json},'
            f'"context_summary":{summary_json},'
            '"verdict":"pass|fail",'
            '"reason":"..."'
            "} "
            f"{echo_rules}"
        )
    body = json.dumps(request, ensure_ascii=False, indent=2, default=str)
    return (
        "You are the team lead for a TeleAgent worker collaboration. "
        "This message is self-contained — do not assume prior conversation memory.\n"
        f"{out_hint}\n"
        f"{allow_hint}\n"
        f"Task goal: {request.get('task_goal')}\n"
        f"Authorized scope: {json.dumps(request.get('authorized_scope'), ensure_ascii=False)}\n"
        f"Prohibitions: {json.dumps(request.get('prohibitions'), ensure_ascii=False)}\n"
        f"Acceptance criteria: {json.dumps(request.get('acceptance_criteria'), ensure_ascii=False)}\n"
        f"application_id: {request.get('application_id')}\n"
        f"context_summary: {request.get('context_summary')}\n"
        f"Full request JSON:\n{body}\n"
    )


def _extract_json_obj(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


_ENVELOPE_KEYS = (
    "structuredOutput",
    "output",
    "message",
    "content",
    "data",
    "result",
    "response",
)


def _coerce_envelope_value(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _extract_json_obj(value)
    return None


def unwrap_structured(parsed: dict | None) -> dict | None:
    """Normalize Grok/Codex envelopes to the inner decision object.

    Walks structuredOutput/output/message/content/data/result/response plus one
    nesting level. Parses JSON-string structuredOutput (and other envelope
    strings). Prefers the inner object that already has decision/verdict even
    if application_id is nested oddly. Does not invent application_id.
    """
    if not isinstance(parsed, dict):
        return None
    if parsed.get("stopReason") == "cancelled":
        return None
    if parsed.get("structuredOutput") is None and parsed.get("structuredOutputError"):
        return None

    decision_hit: dict | None = None
    bind_hit: dict | None = None

    def visit(obj: dict, depth: int) -> None:
        nonlocal decision_hit, bind_hit
        if not isinstance(obj, dict):
            return
        # Inner envelopes first so structuredOutput wins over an outer wrapper.
        if depth > 0:
            for key in _ENVELOPE_KEYS:
                if key not in obj:
                    continue
                child = _coerce_envelope_value(obj.get(key))
                if child is not None:
                    visit(child, depth - 1)
        if decision_hit is None and ("decision" in obj or "verdict" in obj):
            decision_hit = obj
        if bind_hit is None and "application_id" in obj:
            bind_hit = obj

    visit(parsed, 2)
    if decision_hit is not None:
        return decision_hit
    if bind_hit is not None:
        return bind_hit
    text = parsed.get("text")
    if isinstance(text, str):
        inner = _extract_json_obj(text)
        if inner:
            return inner
    return None


class LeadDecisionError(ValueError):
    """Illegal / unbound / incomplete lead output — keep pending or safe-stop."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def validate_lead_decision(
    raw: str | None,
    parsed: dict | None,
    *,
    request: dict,
    kind: str | None = None,
) -> dict:
    """Validate strict JSON bound to application_id + context_summary.

    Raises LeadDecisionError on illegal output (caller keeps pending / safe-stop).
    """
    kind = kind or request.get("kind") or "permission"
    expected_id = str(request.get("application_id") or "")
    expected_summary = str(request.get("context_summary") or "")

    if raw == "TIMEOUT" or (isinstance(parsed, dict) and parsed.get("_lead_status") == "timeout"):
        raise LeadDecisionError("timeout", "lead call timed out")
    if isinstance(parsed, dict) and parsed.get("_lead_status") in ("error", "call_failed"):
        raise LeadDecisionError("call_failed", str(parsed.get("error") or "lead call failed"))

    inner = unwrap_structured(parsed) if isinstance(parsed, dict) else None
    if inner is None:
        inner = _extract_json_obj(raw)
    if not isinstance(inner, dict):
        raise LeadDecisionError("illegal_json", "no parseable JSON decision object")

    app_id = str(inner.get("application_id") or "").strip()
    if not app_id or app_id != expected_id:
        raise LeadDecisionError(
            "application_id_mismatch",
            f"expected {expected_id!r} got {app_id!r}",
        )

    # context_summary: if present must match; if absent we still accept when id binds
    got_summary = str(inner.get("context_summary") or "").strip()
    if got_summary and expected_summary and got_summary != expected_summary:
        raise LeadDecisionError(
            "context_summary_mismatch",
            f"expected {expected_summary!r} got {got_summary!r}",
        )

    if kind == "permission":
        decision = str(inner.get("decision") or "").strip().lower()
        if decision == "always":
            decision = "once"
            inner = dict(inner)
            inner["decision"] = "once"
            inner["_coerced_always"] = True
        if decision not in PERMISSION_DECISIONS:
            raise LeadDecisionError("illegal_decision", f"decision={decision!r}")
        if not str(inner.get("reason") or "").strip():
            raise LeadDecisionError("missing_reason", "reason required")
        return {
            "application_id": app_id,
            "context_summary": got_summary or expected_summary,
            "decision": decision,
            "reason": str(inner.get("reason") or ""),
            "safe_path_hint": str(inner.get("safe_path_hint") or ""),
            "_coerced_always": bool(inner.get("_coerced_always")),
        }

    if kind == "review":
        verdict = str(inner.get("verdict") or "").strip().lower()
        if verdict not in REVIEW_VERDICTS:
            raise LeadDecisionError("illegal_verdict", f"verdict={verdict!r}")
        if not str(inner.get("reason") or "").strip():
            raise LeadDecisionError("missing_reason", "reason required")
        return {
            "application_id": app_id,
            "context_summary": got_summary or expected_summary,
            "verdict": verdict,
            "reason": str(inner.get("reason") or ""),
        }

    raise LeadDecisionError("unknown_kind", f"kind={kind!r}")


__all__ = [
    "PERMISSION_DECISIONS",
    "REVIEW_VERDICTS",
    "DECISION_KINDS",
    "LeadDecisionError",
    "new_application_id",
    "context_summary_of",
    "build_lead_request",
    "lead_permission_response_schema",
    "lead_review_response_schema",
    "pin_lead_response_schema",
    "format_lead_request_prompt",
    "unwrap_structured",
    "validate_lead_decision",
]
