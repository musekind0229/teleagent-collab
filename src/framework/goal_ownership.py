"""Knife 12: one effective coordinator per Goal + versioned plan revisions.

Ownership record ``{coordinator_id, version, updated_at}``.
Plan revisions go through a structured proposal; the kernel validates
legality then commits. Submissions from a stale ownership instance are
rejected. Handoff / succession MUST bump the ownership version.

Persisted under ``<persist_dir>/.collab-goal-ownership/<goal_id>.json``.

Public kernel path only. No TeleAgent HTTP. No Hermes ledger. No glue rewrite.
"""
from __future__ import annotations

import fnmatch
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from framework.delegation import (
    REASON_AUTONOMY_DENIED as DELEGATION_AUTONOMY_DENIED,
    check_plan_against_autonomy,
    normalize_autonomy,
    undeclared_autonomy,
)
from framework.lifecycle import TASK_STATES, LifecycleError, assert_transition

OWNERSHIP_DIRNAME = ".collab-goal-ownership"

STATUS_PROPOSED = "proposed"
STATUS_COMMITTED = "committed"
STATUS_REJECTED = "rejected"

REASON_READY = "ready"
REASON_STALE_OWNERSHIP = "stale_ownership"
REASON_NOT_COORDINATOR = "not_coordinator"
REASON_NO_OWNER = "no_owner"
REASON_ALREADY_OWNED = "already_owned"
REASON_ILLEGAL_PLAN = "illegal_plan"
REASON_STALE_PROPOSAL = "stale_proposal"
REASON_UNKNOWN_PROPOSAL = "unknown_proposal"
REASON_EMPTY_COORDINATOR = "empty_coordinator"
REASON_CONTRACT_EXPANSION = "contract_expansion"
REASON_AUTONOMY_DENIED = DELEGATION_AUTONOMY_DENIED

# Plan documents must not rewrite kernel-owned identity.
_FORBIDDEN_PLAN_KEYS = frozenset(
    {"coordinator_id", "ownership_version", "ownership", "version"}
)

# Goal-contract auth/budget ceiling. Plan may tighten; expansion is refused.
_ALLOW_KEYS = (
    "allow_secret_globs",
    "allow_paths",
    "allow_keys",
    "allowed_surfaces",
    "user_gate_permissions",
)
_BUDGET_LIMIT_KEYS = (
    "wall_sec",
    "max_reworks",
    "max_lead_calls",
    "max_attempts",
    "max_usage",
)

_ACTIVE_TASK_STATUSES = frozenset(
    {"running", "awaiting_decision", "review", "cancel_requested"}
)
_NEW_TASK_STATUSES = frozenset({"queued", "blocked"})


class GoalOwnershipError(RuntimeError):
    """Goal ownership / plan-revision protocol failure (not a success)."""


def _utc_now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    t = ts if ts is not None else _utc_now()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_goal_id(goal_id: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in (goal_id or "goal"))
    return slug[:80] or "goal"


def persist_path(persist_dir: str | Path, goal_id: str) -> Path:
    return Path(persist_dir) / OWNERSHIP_DIRNAME / f"{_safe_goal_id(goal_id)}.json"


def empty_plan(goal_id: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"tasks": []}
    if goal_id:
        out["goal_id"] = goal_id
    return out


def plan_from_mapped(mapped: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a revision plan from ``map_charter_to_goal_task`` output."""
    mapped = mapped if isinstance(mapped, Mapping) else {}
    goal = mapped.get("goal") if isinstance(mapped.get("goal"), Mapping) else {}
    task = mapped.get("task") if isinstance(mapped.get("task"), Mapping) else {}
    gid = str(goal.get("goal_id") or task.get("goal_id") or "")
    tasks = []
    if task:
        tasks.append(dict(task))
    plan: dict[str, Any] = {
        "goal_id": gid,
        "tasks": tasks,
        "desired_outcome": goal.get("desired_outcome"),
        "acceptance": goal.get("acceptance"),
        "title": goal.get("title"),
    }
    return plan


def _task_id_of(task: Mapping[str, Any]) -> str:
    return str(task.get("task_id") or "").strip()


def _tasks_by_id(plan: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    plan = plan if isinstance(plan, Mapping) else {}
    raw = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, Mapping):
            tid = _task_id_of(item)
            if tid:
                out[tid] = dict(item)
    return out


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return []


def extract_goal_contract(src: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pull Goal-contract auth/budget fields used as the plan-revision ceiling."""
    src = src if isinstance(src, Mapping) else {}
    nested = src.get("goal") if isinstance(src.get("goal"), Mapping) else {}
    contract: dict[str, Any] = {}
    budget = src.get("budget") if isinstance(src.get("budget"), Mapping) else None
    if budget is None and isinstance(nested.get("budget"), Mapping):
        budget = nested.get("budget")
    if isinstance(budget, Mapping):
        contract["budget"] = dict(budget)
    boundaries = src.get("boundaries") if isinstance(src.get("boundaries"), Mapping) else None
    if boundaries is None and isinstance(nested.get("boundaries"), Mapping):
        boundaries = nested.get("boundaries")
    b: dict[str, Any] = dict(boundaries) if isinstance(boundaries, Mapping) else {}
    for k in _ALLOW_KEYS:
        if k in src and k not in b:
            b[k] = list(src[k]) if isinstance(src[k], list) else src[k]
        elif k in nested and k not in b:
            b[k] = list(nested[k]) if isinstance(nested[k], list) else nested[k]
    if b:
        contract["boundaries"] = b
    return contract


def _allow_item_covered(item: str, ceiling: list[str]) -> bool:
    item_n = str(item or "").replace("\\", "/").strip()
    if not item_n:
        return True
    for raw in ceiling:
        c_n = str(raw or "").replace("\\", "/").strip()
        if not c_n:
            continue
        if item_n == c_n:
            return True
        if c_n in ("/**", "**", "*"):
            return True
        if c_n.endswith("/**"):
            prefix = c_n[:-3].rstrip("/")
            if not prefix or item_n == prefix or item_n.startswith(prefix + "/"):
                return True
        try:
            if fnmatch.fnmatch(item_n, c_n):
                return True
        except Exception:
            pass
        if c_n.endswith("/") and item_n.startswith(c_n):
            return True
        if item_n.startswith(c_n.rstrip("/") + "/"):
            return True
    return False


def _collect_allow_lists(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {k: [] for k in _ALLOW_KEYS}

    def _extend(src: Mapping[str, Any] | None) -> None:
        if not isinstance(src, Mapping):
            return
        for k in _ALLOW_KEYS:
            if k in src:
                found[k].extend(_as_str_list(src.get(k)))
        nested = src.get("boundaries") if isinstance(src.get("boundaries"), Mapping) else {}
        for k in _ALLOW_KEYS:
            if k in nested:
                found[k].extend(_as_str_list(nested.get(k)))
        inputs = src.get("inputs") if isinstance(src.get("inputs"), Mapping) else {}
        for k in _ALLOW_KEYS:
            if k in inputs:
                found[k].extend(_as_str_list(inputs.get(k)))

    _extend(plan)
    nested_goal = plan.get("goal") if isinstance(plan.get("goal"), Mapping) else {}
    _extend(nested_goal)
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    for item in tasks:
        if isinstance(item, Mapping):
            _extend(item)
    return found


def _check_contract_ceiling(
    plan: Mapping[str, Any],
    ceiling: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Refuse plan revisions that expand Goal contract auth/budget.

    Tightening (lower numeric caps, fewer/narrower allow_*, extra must/must_not)
    is allowed. Introducing or widening budget / allow_* / boundaries is not.
    Contract expansion stays a separate, currently unsupported path.
    """
    ceiling = ceiling if isinstance(ceiling, Mapping) else {}
    ceil_budget = ceiling.get("budget") if isinstance(ceiling.get("budget"), Mapping) else {}
    plan_budget = plan.get("budget") if isinstance(plan.get("budget"), Mapping) else None
    nested = plan.get("goal") if isinstance(plan.get("goal"), Mapping) else {}
    if plan_budget is None and isinstance(nested.get("budget"), Mapping):
        plan_budget = nested.get("budget")
    if isinstance(plan_budget, Mapping):
        if not ceil_budget:
            return False, "plan expands Goal contract budget (no budget on contract)"
        for k in _BUDGET_LIMIT_KEYS:
            if k not in plan_budget:
                continue
            try:
                new_v = float(plan_budget[k])
            except (TypeError, ValueError):
                return False, f"plan expands Goal contract budget (invalid {k})"
            if k not in ceil_budget:
                return False, f"plan expands Goal contract budget (new limit {k})"
            try:
                ceil_v = float(ceil_budget[k])
            except (TypeError, ValueError):
                continue
            if new_v > ceil_v:
                return False, f"plan expands Goal contract budget ({k})"

    ceil_bounds = ceiling.get("boundaries") if isinstance(ceiling.get("boundaries"), Mapping) else {}
    plan_bounds = plan.get("boundaries") if isinstance(plan.get("boundaries"), Mapping) else None
    if plan_bounds is None and isinstance(nested.get("boundaries"), Mapping):
        plan_bounds = nested.get("boundaries")
    if isinstance(plan_bounds, Mapping):
        ceil_must = set(_as_str_list(ceil_bounds.get("must")))
        new_must = set(_as_str_list(plan_bounds.get("must"))) if "must" in plan_bounds else ceil_must
        if not ceil_must.issubset(new_must):
            return False, "plan expands Goal contract boundaries (drops must)"
        ceil_must_not = set(_as_str_list(ceil_bounds.get("must_not")))
        new_must_not = (
            set(_as_str_list(plan_bounds.get("must_not"))) if "must_not" in plan_bounds else ceil_must_not
        )
        if not ceil_must_not.issubset(new_must_not):
            return False, "plan expands Goal contract boundaries (drops must_not)"

    ceil_allow = {k: _as_str_list(ceil_bounds.get(k)) for k in _ALLOW_KEYS}
    for k in _ALLOW_KEYS:
        if k in ceiling:
            ceil_allow[k] = _as_str_list(ceiling.get(k)) or ceil_allow[k]
    found_allow = _collect_allow_lists(plan)
    for k, items in found_allow.items():
        unique = []
        seen: set[str] = set()
        for it in items:
            if it not in seen:
                seen.add(it)
                unique.append(it)
        if not unique:
            continue
        ceiling_items = ceil_allow.get(k) or []
        if not ceiling_items:
            return False, f"plan expands Goal contract {k}"
        for it in unique:
            if not _allow_item_covered(it, ceiling_items):
                return False, f"plan expands Goal contract {k}"
    return True, REASON_READY


def validate_plan_revision(
    plan: Any,
    *,
    goal_id: str,
    current_plan: Mapping[str, Any] | None = None,
    goal_contract: Mapping[str, Any] | None = None,
    enforce_contract_ceiling: bool = True,
    autonomy: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Kernel legality for a Goal plan document.

    Ownership fields are kernel-owned. Task status changes must follow
    the public Task lifecycle. Active tasks cannot be dropped.
    Plan revisions must not expand Goal contract auth/budget
    (``budget``, ``allow_*``, ``boundaries``); tightening is allowed.
    Declared autonomy (explicit plan vs bounded) is respected when set.
    """
    if not isinstance(plan, Mapping):
        return False, "plan must be an object"
    forbidden = [k for k in _FORBIDDEN_PLAN_KEYS if k in plan]
    if forbidden:
        return False, f"plan cannot rewrite ownership ({', '.join(sorted(forbidden))})"
    plan_gid = str(plan.get("goal_id") or "").strip()
    if plan_gid and plan_gid != str(goal_id):
        return False, "plan.goal_id mismatch"
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return False, "plan.tasks must be a list"
    seen: list[str] = []
    new_by_id: dict[str, Mapping[str, Any]] = {}
    for i, item in enumerate(tasks):
        if not isinstance(item, Mapping):
            return False, f"plan.tasks[{i}] must be an object"
        tid = _task_id_of(item)
        if not tid:
            return False, f"plan.tasks[{i}] task_id required"
        if tid in new_by_id:
            return False, f"duplicate task_id {tid}"
        seen.append(tid)
        new_by_id[tid] = item
        tg = str(item.get("goal_id") or "").strip()
        if tg and tg != str(goal_id):
            return False, f"task {tid} goal_id mismatch"
        if "depends_on" in item and item.get("depends_on") is not None:
            if not isinstance(item.get("depends_on"), list):
                return False, f"task {tid} depends_on must be a list"
            for dep in item.get("depends_on") or []:
                if not isinstance(dep, str) or not dep.strip():
                    return False, f"task {tid} depends_on entries must be non-empty strings"
        status = item.get("status")
        if status is not None:
            st = str(status)
            if st not in TASK_STATES:
                return False, f"task {tid} illegal status {st!r}"

    old_by_id = _tasks_by_id(current_plan)
    for tid, old in old_by_id.items():
        if tid not in new_by_id:
            st = str(old.get("status") or "queued")
            if st in _ACTIVE_TASK_STATUSES:
                return False, f"cannot drop active task {tid} status={st}"
    for tid, item in new_by_id.items():
        new_st = str(item.get("status") or "queued")
        if tid not in old_by_id:
            if new_st not in _NEW_TASK_STATUSES:
                return False, f"new task {tid} must start queued/blocked, not {new_st}"
            continue
        old_st = str(old_by_id[tid].get("status") or "queued")
        if old_st != new_st:
            try:
                assert_transition("task", old_st, new_st)
            except LifecycleError as e:
                return False, f"illegal task transition {tid}: {e}"
    if enforce_contract_ceiling:
        ceiling = extract_goal_contract(goal_contract) if isinstance(goal_contract, Mapping) else {}
        if not ceiling:
            ceiling = extract_goal_contract(current_plan)
        ok_ceil, why_ceil = _check_contract_ceiling(plan, ceiling)
        if not ok_ceil:
            return False, why_ceil
    ok_auto, why_auto = check_plan_against_autonomy(autonomy, current_plan, plan)
    if not ok_auto:
        return False, why_auto
    return True, REASON_READY


@dataclass
class GoalOwnership:
    """One effective coordinator for a Goal, plus a monotonic instance version."""

    goal_id: str
    coordinator_id: str = ""
    version: int = 0
    updated_at: float = 0.0
    predecessor_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "coordinator_id": self.coordinator_id,
            "version": int(self.version),
            "updated_at": float(self.updated_at),
            "updated_at_iso": _iso(self.updated_at) if self.updated_at else "",
            "predecessor_id": self.predecessor_id,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "GoalOwnership":
        d = d if isinstance(d, Mapping) else {}
        return cls(
            goal_id=str(d.get("goal_id") or ""),
            coordinator_id=str(d.get("coordinator_id") or ""),
            version=int(d.get("version") or 0),
            updated_at=float(d.get("updated_at") or 0.0),
            predecessor_id=str(d.get("predecessor_id") or ""),
        )


@dataclass
class PlanRevisionProposal:
    """Structured plan-revision proposal. Kernel commits only after legality."""

    proposal_id: str
    goal_id: str
    coordinator_id: str
    ownership_version: int
    plan: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    created_at: float = 0.0
    status: str = STATUS_PROPOSED
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "goal_id": self.goal_id,
            "coordinator_id": self.coordinator_id,
            "ownership_version": int(self.ownership_version),
            "plan": dict(self.plan),
            "reason": self.reason,
            "created_at": float(self.created_at),
            "created_at_iso": _iso(self.created_at) if self.created_at else "",
            "status": self.status,
            "reject_reason": self.reject_reason,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "PlanRevisionProposal | None":
        if not isinstance(d, Mapping) or not d.get("proposal_id"):
            return None
        plan = d.get("plan") if isinstance(d.get("plan"), Mapping) else {}
        return cls(
            proposal_id=str(d.get("proposal_id") or ""),
            goal_id=str(d.get("goal_id") or ""),
            coordinator_id=str(d.get("coordinator_id") or ""),
            ownership_version=int(d.get("ownership_version") or 0),
            plan=dict(plan),
            reason=str(d.get("reason") or ""),
            created_at=float(d.get("created_at") or 0.0),
            status=str(d.get("status") or STATUS_PROPOSED),
            reject_reason=str(d.get("reject_reason") or ""),
        )


_CACHE: dict[tuple[str, str], "GoalOwnershipStore"] = {}
_CACHE_MU = threading.Lock()


def reset_goal_ownership_cache() -> None:
    with _CACHE_MU:
        _CACHE.clear()


class GoalOwnershipStore:
    """Persisted Goal ownership + plan revision log. One coordinator at a time."""

    def __init__(self, *, goal_id: str, persist_dir: str | Path) -> None:
        self.goal_id = str(goal_id or "").strip() or "goal"
        self.persist_dir = Path(persist_dir)
        self.ownership = GoalOwnership(goal_id=self.goal_id)
        self.plan: dict[str, Any] = empty_plan(self.goal_id)
        self.plan_revision: int = 0
        self.goal_contract: dict[str, Any] = {}
        self.autonomy: dict[str, Any] = undeclared_autonomy()
        self.proposals: dict[str, PlanRevisionProposal] = {}
        self.history: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self._mu = threading.RLock()

    @property
    def path(self) -> Path:
        return persist_path(self.persist_dir, self.goal_id)

    def current(self) -> GoalOwnership:
        with self._mu:
            return GoalOwnership(
                goal_id=self.ownership.goal_id,
                coordinator_id=self.ownership.coordinator_id,
                version=self.ownership.version,
                updated_at=self.ownership.updated_at,
                predecessor_id=self.ownership.predecessor_id,
            )

    def effective_coordinator(self) -> str:
        return self.current().coordinator_id

    def check_submitter(self, coordinator_id: str, ownership_version: int) -> tuple[bool, str]:
        """Accept only the live ownership instance.

        Right coordinator + current version → ready.
        Any other version → stale_ownership (expired instance).
        Current version but wrong coordinator → not_coordinator.
        """
        cid = str(coordinator_id or "").strip()
        try:
            ver = int(ownership_version)
        except (TypeError, ValueError):
            return False, REASON_STALE_OWNERSHIP
        with self._mu:
            own = self.ownership
            if not own.coordinator_id or int(own.version) <= 0:
                return False, REASON_NO_OWNER
            if not cid:
                return False, REASON_EMPTY_COORDINATOR
            if cid == own.coordinator_id and ver == int(own.version):
                return True, REASON_READY
            if ver != int(own.version):
                return False, REASON_STALE_OWNERSHIP
            return False, REASON_NOT_COORDINATOR

    def set_autonomy(self, autonomy: Any) -> dict[str, Any]:
        """Store declared autonomy scope. Does not bump ownership version."""
        spec = normalize_autonomy(autonomy)
        with self._mu:
            self.autonomy = spec
            self._persist_unlocked()
            return {"ok": True, "autonomy": dict(self.autonomy)}

    def claim(
        self,
        coordinator_id: str,
        *,
        initial_plan: Mapping[str, Any] | None = None,
        autonomy: Any = None,
        goal_contract: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """First coordinator at version=1. Same coordinator is idempotent (no bump)."""
        cid = str(coordinator_id or "").strip()
        if not cid:
            return {
                "ok": False,
                "reason": REASON_EMPTY_COORDINATOR,
                "error": "coordinator_id required",
                "ownership": self.current().to_dict(),
            }
        with self._mu:
            own = self.ownership
            if own.coordinator_id and own.version > 0:
                if own.coordinator_id == cid:
                    return {
                        "ok": True,
                        "reason": REASON_READY,
                        "created": False,
                        "bumped": False,
                        "ownership": own.to_dict(),
                    }
                return {
                    "ok": False,
                    "reason": REASON_ALREADY_OWNED,
                    "error": (
                        f"goal {self.goal_id} already has coordinator "
                        f"{own.coordinator_id} v{own.version}"
                    ),
                    "ownership": own.to_dict(),
                }
            now = _utc_now()
            if autonomy is not None:
                self.autonomy = normalize_autonomy(autonomy)
            if initial_plan is not None:
                ok, why = validate_plan_revision(
                    initial_plan,
                    goal_id=self.goal_id,
                    current_plan=self.plan,
                    enforce_contract_ceiling=False,
                    autonomy=undeclared_autonomy(),
                )
                if not ok:
                    return {
                        "ok": False,
                        "reason": REASON_ILLEGAL_PLAN,
                        "error": why,
                        "ownership": own.to_dict(),
                    }
                self.plan = dict(initial_plan)
                if "goal_id" not in self.plan:
                    self.plan["goal_id"] = self.goal_id
                self.goal_contract = extract_goal_contract(self.plan)
                self.plan_revision = 1 if self.plan.get("tasks") else 0
            if isinstance(goal_contract, Mapping) and goal_contract:
                extracted = extract_goal_contract(goal_contract)
                if extracted:
                    self.goal_contract = extracted
            self.ownership = GoalOwnership(
                goal_id=self.goal_id,
                coordinator_id=cid,
                version=1,
                updated_at=now,
                predecessor_id="",
            )
            self.history.append(
                {
                    "op": "claim",
                    "coordinator_id": cid,
                    "version": 1,
                    "at": now,
                    "at_iso": _iso(now),
                }
            )
            self.notes.append(f"claim coordinator={cid} version=1")
            self._persist_unlocked()
            return {
                "ok": True,
                "reason": REASON_READY,
                "created": True,
                "bumped": False,
                "ownership": self.ownership.to_dict(),
            }

    def handoff(
        self,
        from_coordinator_id: str,
        to_coordinator_id: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Succession. Always bumps version. Old instance becomes stale."""
        src = str(from_coordinator_id or "").strip()
        dst = str(to_coordinator_id or "").strip()
        if not src or not dst:
            return {
                "ok": False,
                "reason": REASON_EMPTY_COORDINATOR,
                "error": "handoff requires from and to coordinator_id",
                "ownership": self.current().to_dict(),
                "bumped": False,
            }
        with self._mu:
            own = self.ownership
            if not own.coordinator_id or own.version <= 0:
                return {
                    "ok": False,
                    "reason": REASON_NO_OWNER,
                    "error": "no coordinator to hand off",
                    "ownership": own.to_dict(),
                    "bumped": False,
                }
            if src != own.coordinator_id:
                # Wrong person: stale if they presented an expired version.
                if expected_version is not None and int(expected_version) != int(own.version):
                    reason = REASON_STALE_OWNERSHIP
                else:
                    reason = REASON_NOT_COORDINATOR
                return {
                    "ok": False,
                    "reason": reason,
                    "error": (
                        f"handoff denied: effective coordinator is "
                        f"{own.coordinator_id} v{own.version}"
                    ),
                    "ownership": own.to_dict(),
                    "bumped": False,
                }
            if expected_version is not None and int(expected_version) != int(own.version):
                return {
                    "ok": False,
                    "reason": REASON_STALE_OWNERSHIP,
                    "error": (
                        f"handoff denied: stale ownership v{expected_version}; "
                        f"current is v{own.version}"
                    ),
                    "ownership": own.to_dict(),
                    "bumped": False,
                }
            now = _utc_now()
            new_ver = int(own.version) + 1
            self.ownership = GoalOwnership(
                goal_id=self.goal_id,
                coordinator_id=dst,
                version=new_ver,
                updated_at=now,
                predecessor_id=src,
            )
            self.history.append(
                {
                    "op": "handoff",
                    "from": src,
                    "to": dst,
                    "version": new_ver,
                    "predecessor_version": new_ver - 1,
                    "at": now,
                    "at_iso": _iso(now),
                }
            )
            self.notes.append(f"handoff {src} -> {dst} version={new_ver}")
            self._persist_unlocked()
            return {
                "ok": True,
                "reason": REASON_READY,
                "bumped": True,
                "from": src,
                "to": dst,
                "version": new_ver,
                "ownership": self.ownership.to_dict(),
            }

    def succeed(
        self,
        coordinator_id: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Same-coordinator succession: still bumps the ownership version."""
        return self.handoff(
            coordinator_id,
            coordinator_id,
            expected_version=expected_version,
        )

    def propose_plan_revision(
        self,
        *,
        coordinator_id: str,
        ownership_version: int,
        plan: Mapping[str, Any],
        reason: str = "",
        proposal_id: str = "",
    ) -> PlanRevisionProposal:
        """Record a structured proposal. Stale instances are rejected, not committed."""
        pid = (proposal_id or "").strip() or f"prop_{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        rec = PlanRevisionProposal(
            proposal_id=pid,
            goal_id=self.goal_id,
            coordinator_id=str(coordinator_id or "").strip(),
            ownership_version=int(ownership_version or 0),
            plan=dict(plan) if isinstance(plan, Mapping) else {},
            reason=str(reason or ""),
            created_at=now,
            status=STATUS_PROPOSED,
        )
        with self._mu:
            ok, why = self.check_submitter(rec.coordinator_id, rec.ownership_version)
            if not ok:
                rec.status = STATUS_REJECTED
                rec.reject_reason = why
                self.proposals[pid] = rec
                self.history.append(
                    {
                        "op": "propose_rejected",
                        "proposal_id": pid,
                        "coordinator_id": rec.coordinator_id,
                        "ownership_version": rec.ownership_version,
                        "reason": why,
                        "at": now,
                        "at_iso": _iso(now),
                    }
                )
                self.notes.append(
                    f"propose rejected {pid} coordinator={rec.coordinator_id} "
                    f"v{rec.ownership_version} reason={why}"
                )
                self._persist_unlocked()
                return rec
            legal, detail = validate_plan_revision(
                rec.plan,
                goal_id=self.goal_id,
                current_plan=self.plan,
                goal_contract=self.goal_contract,
                autonomy=self.autonomy,
            )
            if not legal:
                if "expands Goal contract" in detail:
                    reject = REASON_CONTRACT_EXPANSION
                elif detail == REASON_AUTONOMY_DENIED or "forbids" in detail or "autonomy" in detail:
                    reject = REASON_AUTONOMY_DENIED
                else:
                    reject = REASON_ILLEGAL_PLAN
                rec.status = STATUS_REJECTED
                rec.reject_reason = reject
                rec.reason = rec.reason or detail
                self.proposals[pid] = rec
                self.history.append(
                    {
                        "op": "propose_rejected",
                        "proposal_id": pid,
                        "coordinator_id": rec.coordinator_id,
                        "reason": reject,
                        "detail": detail,
                        "at": now,
                        "at_iso": _iso(now),
                    }
                )
                self.notes.append(f"propose rejected {pid} {reject}: {detail}")
                self._persist_unlocked()
                return rec
            self.proposals[pid] = rec
            self.history.append(
                {
                    "op": "propose",
                    "proposal_id": pid,
                    "coordinator_id": rec.coordinator_id,
                    "ownership_version": rec.ownership_version,
                    "at": now,
                    "at_iso": _iso(now),
                }
            )
            self.notes.append(
                f"propose {pid} coordinator={rec.coordinator_id} v{rec.ownership_version}"
            )
            self._persist_unlocked()
            return rec

    def commit_revision(
        self,
        proposal_id: str,
        *,
        coordinator_id: str | None = None,
        ownership_version: int | None = None,
    ) -> dict[str, Any]:
        """Validate legality against live ownership, then commit the plan."""
        pid = str(proposal_id or "").strip()
        with self._mu:
            rec = self.proposals.get(pid)
            if rec is None:
                return {
                    "ok": False,
                    "reason": REASON_UNKNOWN_PROPOSAL,
                    "error": f"unknown proposal {pid}",
                    "ownership": self.ownership.to_dict(),
                }
            if rec.status == STATUS_COMMITTED:
                return {
                    "ok": True,
                    "reason": REASON_READY,
                    "idempotent": True,
                    "plan_revision": self.plan_revision,
                    "proposal_id": pid,
                    "plan": dict(self.plan),
                    "ownership": self.ownership.to_dict(),
                }
            if rec.status == STATUS_REJECTED:
                return {
                    "ok": False,
                    "reason": rec.reject_reason or REASON_STALE_PROPOSAL,
                    "error": (
                        rec.reject_reason
                        or "proposal was rejected; commit refused"
                    ),
                    "proposal_id": pid,
                    "ownership": self.ownership.to_dict(),
                }
            cid = str(coordinator_id if coordinator_id is not None else rec.coordinator_id)
            ver = int(
                ownership_version if ownership_version is not None else rec.ownership_version
            )
            ok, why = self.check_submitter(cid, ver)
            if not ok:
                rec.status = STATUS_REJECTED
                rec.reject_reason = why
                self.history.append(
                    {
                        "op": "commit_rejected",
                        "proposal_id": pid,
                        "reason": why,
                        "at": _utc_now(),
                    }
                )
                self.notes.append(f"commit rejected {pid} reason={why}")
                self._persist_unlocked()
                return {
                    "ok": False,
                    "reason": why,
                    "error": (
                        "stale ownership instance; submit refused"
                        if why == REASON_STALE_OWNERSHIP
                        else f"commit refused: {why}"
                    ),
                    "proposal_id": pid,
                    "ownership": self.ownership.to_dict(),
                }
            # Proposal was minted under a (possibly older) instance — must still match live.
            if rec.ownership_version != int(self.ownership.version) or rec.coordinator_id != self.ownership.coordinator_id:
                rec.status = STATUS_REJECTED
                rec.reject_reason = REASON_STALE_OWNERSHIP
                self.history.append(
                    {
                        "op": "commit_rejected",
                        "proposal_id": pid,
                        "reason": REASON_STALE_OWNERSHIP,
                        "at": _utc_now(),
                    }
                )
                self.notes.append(f"commit rejected {pid} stale proposal instance")
                self._persist_unlocked()
                return {
                    "ok": False,
                    "reason": REASON_STALE_OWNERSHIP,
                    "error": "stale ownership instance; submit refused",
                    "proposal_id": pid,
                    "ownership": self.ownership.to_dict(),
                }
            legal, detail = validate_plan_revision(
                rec.plan,
                goal_id=self.goal_id,
                current_plan=self.plan,
                goal_contract=self.goal_contract,
                autonomy=self.autonomy,
            )
            if not legal:
                if "expands Goal contract" in detail:
                    reject = REASON_CONTRACT_EXPANSION
                elif detail == REASON_AUTONOMY_DENIED or "forbids" in detail or "autonomy" in detail:
                    reject = REASON_AUTONOMY_DENIED
                else:
                    reject = REASON_ILLEGAL_PLAN
                rec.status = STATUS_REJECTED
                rec.reject_reason = reject
                self.history.append(
                    {
                        "op": "commit_rejected",
                        "proposal_id": pid,
                        "reason": reject,
                        "detail": detail,
                        "at": _utc_now(),
                    }
                )
                self.notes.append(f"commit rejected {pid} {reject}: {detail}")
                self._persist_unlocked()
                return {
                    "ok": False,
                    "reason": reject,
                    "error": detail,
                    "proposal_id": pid,
                    "ownership": self.ownership.to_dict(),
                }
            now = _utc_now()
            self.plan = dict(rec.plan)
            if "goal_id" not in self.plan:
                self.plan["goal_id"] = self.goal_id
            self.plan_revision = int(self.plan_revision) + 1
            rec.status = STATUS_COMMITTED
            rec.reject_reason = ""
            self.history.append(
                {
                    "op": "commit",
                    "proposal_id": pid,
                    "plan_revision": self.plan_revision,
                    "coordinator_id": rec.coordinator_id,
                    "ownership_version": rec.ownership_version,
                    "at": now,
                    "at_iso": _iso(now),
                }
            )
            self.notes.append(
                f"commit {pid} plan_revision={self.plan_revision} "
                f"coordinator={rec.coordinator_id} v{rec.ownership_version}"
            )
            self._persist_unlocked()
            return {
                "ok": True,
                "reason": REASON_READY,
                "plan_revision": self.plan_revision,
                "proposal_id": pid,
                "plan": dict(self.plan),
                "ownership": self.ownership.to_dict(),
            }

    def submit_plan_revision(
        self,
        *,
        coordinator_id: str,
        ownership_version: int,
        plan: Mapping[str, Any],
        reason: str = "",
        proposal_id: str = "",
    ) -> dict[str, Any]:
        """Propose then commit. One call for a live coordinator instance."""
        prop = self.propose_plan_revision(
            coordinator_id=coordinator_id,
            ownership_version=ownership_version,
            plan=plan,
            reason=reason,
            proposal_id=proposal_id,
        )
        if prop.status == STATUS_REJECTED:
            return {
                "ok": False,
                "reason": prop.reject_reason or REASON_STALE_OWNERSHIP,
                "error": (
                    "stale ownership instance; submit refused"
                    if prop.reject_reason == REASON_STALE_OWNERSHIP
                    else (prop.reason or prop.reject_reason or "proposal rejected")
                ),
                "proposal_id": prop.proposal_id,
                "proposal": prop.to_dict(),
                "ownership": self.current().to_dict(),
            }
        committed = self.commit_revision(prop.proposal_id)
        committed["proposal"] = prop.to_dict()
        return committed

    def to_dict(self) -> dict[str, Any]:
        with self._mu:
            return {
                "goal_id": self.goal_id,
                "contract_version": "contract.v0.1-draft",
                "ownership": self.ownership.to_dict(),
                "plan_revision": int(self.plan_revision),
                "plan": dict(self.plan),
                "goal_contract": dict(self.goal_contract),
                "autonomy": dict(self.autonomy),
                "proposals": {k: v.to_dict() for k, v in self.proposals.items()},
                "history": list(self.history),
                "notes": list(self.notes),
                "persist_path": str(self.path),
                "effective_coordinator": self.ownership.coordinator_id,
            }

    def _persist_unlocked(self) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "goal_id": self.goal_id,
                "ownership": self.ownership.to_dict(),
                "plan_revision": int(self.plan_revision),
                "plan": dict(self.plan),
                "goal_contract": dict(self.goal_contract),
                "autonomy": dict(self.autonomy),
                "proposals": {k: v.to_dict() for k, v in self.proposals.items()},
                "history": list(self.history)[-200:],
                "notes": list(self.notes)[-50:],
            }
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            self.notes.append(f"persist failed: {e}")

    def persist(self) -> Path:
        with self._mu:
            self._persist_unlocked()
            return self.path

    def load_into(self, payload: Mapping[str, Any]) -> None:
        with self._mu:
            own_raw = payload.get("ownership") if isinstance(payload.get("ownership"), Mapping) else payload
            loaded = GoalOwnership.from_dict(own_raw if isinstance(own_raw, Mapping) else {})
            if loaded.goal_id:
                self.ownership = loaded
                self.ownership.goal_id = self.goal_id
            self.plan_revision = int(payload.get("plan_revision") or 0)
            plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else None
            self.plan = dict(plan) if plan is not None else empty_plan(self.goal_id)
            contract = payload.get("goal_contract") if isinstance(payload.get("goal_contract"), Mapping) else None
            self.goal_contract = dict(contract) if contract is not None else extract_goal_contract(self.plan)
            self.autonomy = normalize_autonomy(payload.get("autonomy"))
            self.proposals = {}
            raw = payload.get("proposals") if isinstance(payload.get("proposals"), Mapping) else {}
            for k, v in raw.items():
                rec = PlanRevisionProposal.from_dict(v if isinstance(v, Mapping) else {})
                if rec is not None:
                    self.proposals[str(k)] = rec
            self.history = [dict(x) for x in (payload.get("history") or []) if isinstance(x, Mapping)]
            self.notes = [str(x) for x in (payload.get("notes") or [])]

    @classmethod
    def open(
        cls,
        goal_id: str,
        persist_dir: str | Path,
        *,
        use_cache: bool = True,
    ) -> "GoalOwnershipStore":
        gid = str(goal_id or "").strip() or "goal"
        root = Path(persist_dir)
        key = (str(root.resolve()) if root.exists() else str(root), gid)
        if use_cache:
            with _CACHE_MU:
                hit = _CACHE.get(key)
                if hit is not None:
                    return hit
        store = cls(goal_id=gid, persist_dir=root)
        path = persist_path(root, gid)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                store.load_into(payload)
                store.notes.append("loaded existing Goal ownership")
        else:
            store.notes.append("opened empty Goal ownership")
            store._persist_unlocked()
        if use_cache:
            with _CACHE_MU:
                _CACHE[key] = store
        return store


def open_goal_ownership(
    goal_id: str,
    persist_dir: str | Path,
    *,
    coordinator_id: str = "",
) -> GoalOwnershipStore:
    store = GoalOwnershipStore.open(goal_id, persist_dir)
    if coordinator_id:
        store.claim(coordinator_id)
    return store


def attach_goal_ownership(
    result: dict[str, Any],
    store: GoalOwnershipStore | None,
) -> dict[str, Any]:
    """Readonly snapshot. Does not flip ok/fail (projection-safe)."""
    if store is None or not isinstance(result, dict):
        return result
    result = dict(result)
    snap = store.to_dict()
    result["goal_ownership"] = {
        "coordinator_id": snap["ownership"]["coordinator_id"],
        "version": snap["ownership"]["version"],
        "updated_at": snap["ownership"]["updated_at"],
        "updated_at_iso": snap["ownership"].get("updated_at_iso") or "",
        "plan_revision": snap["plan_revision"],
        "goal_id": snap["goal_id"],
        "readonly": True,
    }
    result["goal_id"] = result.get("goal_id") or store.goal_id
    return result
