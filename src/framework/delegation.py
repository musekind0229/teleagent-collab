"""v0.2 P1: minimal delegation semantics (autonomy + return-to-upper).

Not a second engine. Goal/Task/Run and ownership stay as they are.
Kernel stores the declared autonomy scope; plan revision consults it.
Escalate conditions become pending decision kinds — never a silent retry.

Public kernel path only. No TeleAgent HTTP. No Hermes ledger. No glue rewrite.
"""
from __future__ import annotations

from typing import Any, Mapping

AUTONOMY_EXPLICIT_PLAN = "explicit_plan"
AUTONOMY_BOUNDED = "bounded_autonomy"
AUTONOMY_UNDECLARED = "undeclared"

AUTONOMY_MODES = frozenset(
    {AUTONOMY_EXPLICIT_PLAN, AUTONOMY_BOUNDED, AUTONOMY_UNDECLARED}
)

REASON_AUTONOMY_DENIED = "autonomy_denied"
REASON_UNKNOWN_AUTONOMY = "unknown_autonomy"
REASON_UNKNOWN_ESCALATE = "unknown_escalate_kind"

KIND_ESCALATE_OVER_BUDGET = "escalate_over_budget"
KIND_ESCALATE_OUT_OF_SCOPE = "escalate_out_of_scope"
KIND_ESCALATE_INSUFFICIENT_AUTH = "escalate_insufficient_auth"

ESCALATE_KINDS = frozenset(
    {
        KIND_ESCALATE_OVER_BUDGET,
        KIND_ESCALATE_OUT_OF_SCOPE,
        KIND_ESCALATE_INSUFFICIENT_AUTH,
    }
)

EVENT_CLASS_STATUS = "status"
EVENT_CLASS_DECISION = "decision_required"

_AUTONOMY_ALIASES = {
    "explicit_plan": AUTONOMY_EXPLICIT_PLAN,
    "explicit": AUTONOMY_EXPLICIT_PLAN,
    "plan": AUTONOMY_EXPLICIT_PLAN,
    "plan_mode": AUTONOMY_EXPLICIT_PLAN,
    "planned": AUTONOMY_EXPLICIT_PLAN,
    "bounded_autonomy": AUTONOMY_BOUNDED,
    "bounded": AUTONOMY_BOUNDED,
    "autonomy": AUTONOMY_BOUNDED,
    "local": AUTONOMY_BOUNDED,
    "undeclared": AUTONOMY_UNDECLARED,
    "unspecified": AUTONOMY_UNDECLARED,
    "default": AUTONOMY_UNDECLARED,
}

_ESCALATE_ALIASES = {
    "over_budget": KIND_ESCALATE_OVER_BUDGET,
    "overbudget": KIND_ESCALATE_OVER_BUDGET,
    "budget": KIND_ESCALATE_OVER_BUDGET,
    "budget_exhausted": KIND_ESCALATE_OVER_BUDGET,
    KIND_ESCALATE_OVER_BUDGET: KIND_ESCALATE_OVER_BUDGET,
    "out_of_scope": KIND_ESCALATE_OUT_OF_SCOPE,
    "scope": KIND_ESCALATE_OUT_OF_SCOPE,
    "out-of-scope": KIND_ESCALATE_OUT_OF_SCOPE,
    KIND_ESCALATE_OUT_OF_SCOPE: KIND_ESCALATE_OUT_OF_SCOPE,
    "insufficient_auth": KIND_ESCALATE_INSUFFICIENT_AUTH,
    "auth": KIND_ESCALATE_INSUFFICIENT_AUTH,
    "authorization": KIND_ESCALATE_INSUFFICIENT_AUTH,
    "unauthorized": KIND_ESCALATE_INSUFFICIENT_AUTH,
    KIND_ESCALATE_INSUFFICIENT_AUTH: KIND_ESCALATE_INSUFFICIENT_AUTH,
}

_REASSIGN_KEYS = (
    "assignee_role",
    "backend_requirement",
    "assignee",
    "executor_id",
    "worker",
    "backend",
    "coordinator_id",
)

_REWORK_FROM = frozenset({"failed", "succeeded"})
_REWORK_TO = frozenset({"queued", "running", "blocked", "review"})


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def undeclared_autonomy() -> dict[str, Any]:
    return {
        "mode": AUTONOMY_UNDECLARED,
        "allow_local_plan": True,
        "allow_rework": True,
        "allow_reassign": True,
    }


def normalize_autonomy(value: Any) -> dict[str, Any]:
    """Return a stored autonomy spec. Unknown/empty → undeclared (P0 plan rules)."""
    if value is None or value == "":
        return undeclared_autonomy()
    src: Mapping[str, Any]
    if isinstance(value, str):
        src = {"mode": value}
    elif isinstance(value, Mapping):
        nested = value.get("autonomy") if isinstance(value.get("autonomy"), Mapping) else None
        src = dict(nested) if nested is not None else dict(value)
        if "mode" not in src and value.get("mode"):
            src["mode"] = value.get("mode")
    else:
        return undeclared_autonomy()

    raw_mode = _norm(src.get("mode") or src.get("autonomy") or "").lower().replace("-", "_").replace(" ", "_")
    mode = _AUTONOMY_ALIASES.get(raw_mode, raw_mode)
    if mode not in AUTONOMY_MODES:
        return {
            **undeclared_autonomy(),
            "mode": raw_mode or AUTONOMY_UNDECLARED,
            "unknown": True,
        }

    if mode == AUTONOMY_EXPLICIT_PLAN:
        d_plan, d_rework, d_reassign = False, False, False
    elif mode == AUTONOMY_BOUNDED:
        d_plan, d_rework, d_reassign = True, True, True
    else:
        d_plan, d_rework, d_reassign = True, True, True

    return {
        "mode": mode,
        "allow_local_plan": _as_bool(src.get("allow_local_plan", src.get("allow_plan")), d_plan),
        "allow_rework": _as_bool(src.get("allow_rework"), d_rework),
        "allow_reassign": _as_bool(src.get("allow_reassign"), d_reassign),
    }


def autonomy_is_known(spec: Mapping[str, Any] | None) -> bool:
    spec = spec if isinstance(spec, Mapping) else {}
    mode = _norm(spec.get("mode"))
    return mode in AUTONOMY_MODES and mode != AUTONOMY_UNDECLARED and not spec.get("unknown")


def _tasks_by_id(plan: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    plan = plan if isinstance(plan, Mapping) else {}
    raw = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        tid = _norm(item.get("task_id"))
        if tid:
            out[tid] = dict(item)
    return out


def _plan_adds_or_drops_tasks(current: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> bool:
    old_ids = set(_tasks_by_id(current))
    new_ids = set(_tasks_by_id(new))
    return old_ids != new_ids


def _plan_reassigns(current: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> bool:
    old = _tasks_by_id(current)
    new_tasks = _tasks_by_id(new)
    for tid, rec in new_tasks.items():
        prev = old.get(tid)
        if prev is None:
            continue
        for k in _REASSIGN_KEYS:
            if k in rec and _norm(rec.get(k)) != _norm(prev.get(k)):
                return True
    return False


def _plan_reworks(current: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> bool:
    old = _tasks_by_id(current)
    new_tasks = _tasks_by_id(new)
    for tid, rec in new_tasks.items():
        prev = old.get(tid)
        if prev is None:
            continue
        old_st = _norm(prev.get("status") or "queued") or "queued"
        new_st = _norm(rec.get("status") or old_st) or old_st
        if old_st in _REWORK_FROM and new_st in _REWORK_TO:
            return True
        try:
            old_att = int(prev.get("attempt") or 0)
            new_att = int(rec.get("attempt") or old_att)
        except (TypeError, ValueError):
            old_att, new_att = 0, 0
        if new_att > old_att:
            return True
        if rec.get("rework") or rec.get("rework_of"):
            return True
    return False


def check_plan_against_autonomy(
    autonomy: Any,
    current_plan: Mapping[str, Any] | None,
    new_plan: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Whether a plan revision is allowed under the stored autonomy scope."""
    spec = normalize_autonomy(autonomy)
    if spec.get("unknown"):
        return False, REASON_UNKNOWN_AUTONOMY
    if spec.get("mode") in {"", AUTONOMY_UNDECLARED}:
        return True, "ready"

    if not spec.get("allow_local_plan") and _plan_adds_or_drops_tasks(current_plan, new_plan):
        return False, "explicit_plan forbids local planning"
    if not spec.get("allow_reassign") and _plan_reassigns(current_plan, new_plan):
        return False, "autonomy forbids reassign"
    if not spec.get("allow_rework") and _plan_reworks(current_plan, new_plan):
        return False, "autonomy forbids rework"
    if spec.get("mode") == AUTONOMY_EXPLICIT_PLAN and not spec.get("allow_local_plan"):
        # Any structural task-list change already caught; identical / tighten-in-place OK.
        if _plan_adds_or_drops_tasks(current_plan, new_plan):
            return False, "explicit_plan forbids local planning"
    return True, "ready"


def normalize_escalate_kind(value: Any) -> str:
    raw = _norm(value).lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    return _ESCALATE_ALIASES.get(raw, raw if raw in ESCALATE_KINDS else "")


def is_escalate_kind(kind: Any, *, return_to_upper: bool = False) -> bool:
    if return_to_upper:
        return True
    k = _norm(kind)
    if k in ESCALATE_KINDS:
        return True
    return k.startswith("escalate_") or k == "return_to_upper"


def event_class_for(*, kind: str = "", op: str = "", return_to_upper: bool = False) -> str:
    if is_escalate_kind(kind, return_to_upper=return_to_upper):
        return EVENT_CLASS_DECISION
    if op in {"escalate_to_upper", "return_to_upper"}:
        return EVENT_CLASS_DECISION
    if op == "open_decision" and is_escalate_kind(kind):
        return EVENT_CLASS_DECISION
    return EVENT_CLASS_STATUS
