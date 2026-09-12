"""Knife 11: Goal-level budget account + task-dependency dispatch gate.

Child Task + rework + approval costs roll up to the parent Goal.
Minting a new task_id / run_id does not reset the account.
Dispatch reserves; finish reconciles (unused reservation is released).
Over-budget must not report success.

Unsatisfied Task.depends_on stay queued. Before queued→running: deps,
budget reserve, then workdir claim (knife 10).

Public ExecutionBackend path only. No TeleAgent HTTP. No Hermes ledger.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from framework.lifecycle import assert_transition, map_error_class
from framework.task_deps import (
    depends_on_strings,
    unsatisfied_deps,
)

BUDGET_DIRNAME = ".collab-goal-budget"
STATUS_HELD = "held"
STATUS_RECONCILED = "reconciled"
STATUS_RELEASED = "released"
STATUS_DENIED = "denied"

REASON_READY = "ready"
REASON_UNSATISFIED_DEPS = "unsatisfied_deps"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_WORKDIR_OCCUPIED = "workdir_occupied"
REASON_MAX_PARALLEL = "max_parallel"


class GoalBudgetError(RuntimeError):
    """Goal budget protocol failure (not a success)."""


def _utc_now() -> float:
    return time.time()


def _safe_goal_id(goal_id: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in (goal_id or "goal"))
    return slug[:80] or "goal"


@dataclass
class BudgetCost:
    """One cost vector. Attempts / reworks / approvals / wall / usage."""

    wall_sec: float = 0.0
    attempts: int = 0
    reworks: int = 0
    approvals: int = 0
    usage: float = 0.0

    def add(self, other: "BudgetCost") -> "BudgetCost":
        return BudgetCost(
            wall_sec=float(self.wall_sec) + float(other.wall_sec),
            attempts=int(self.attempts) + int(other.attempts),
            reworks=int(self.reworks) + int(other.reworks),
            approvals=int(self.approvals) + int(other.approvals),
            usage=float(self.usage) + float(other.usage),
        )

    def sub(self, other: "BudgetCost") -> "BudgetCost":
        return BudgetCost(
            wall_sec=max(0.0, float(self.wall_sec) - float(other.wall_sec)),
            attempts=max(0, int(self.attempts) - int(other.attempts)),
            reworks=max(0, int(self.reworks) - int(other.reworks)),
            approvals=max(0, int(self.approvals) - int(other.approvals)),
            usage=max(0.0, float(self.usage) - float(other.usage)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_sec": float(self.wall_sec),
            "attempts": int(self.attempts),
            "reworks": int(self.reworks),
            "approvals": int(self.approvals),
            "usage": float(self.usage),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "BudgetCost":
        d = d or {}
        return cls(
            wall_sec=float(d.get("wall_sec") or 0.0),
            attempts=int(d.get("attempts") or 0),
            reworks=int(d.get("reworks") or 0),
            approvals=int(d.get("approvals") or 0),
            usage=float(d.get("usage") or 0.0),
        )


@dataclass
class BudgetLimits:
    wall_sec: float = 360.0
    max_attempts: int = 8
    max_reworks: int = 8
    max_approvals: int = 8
    max_usage: float | None = None  # None = unlimited

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_sec": float(self.wall_sec),
            "max_attempts": int(self.max_attempts),
            "max_reworks": int(self.max_reworks),
            "max_approvals": int(self.max_approvals),
            "max_usage": None if self.max_usage is None else float(self.max_usage),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "BudgetLimits":
        d = d or {}
        usage = d.get("max_usage")
        return cls(
            wall_sec=float(d.get("wall_sec") if d.get("wall_sec") is not None else 360.0),
            max_attempts=int(d.get("max_attempts") if d.get("max_attempts") is not None else 8),
            max_reworks=int(d.get("max_reworks") if d.get("max_reworks") is not None else 8),
            max_approvals=int(d.get("max_approvals") if d.get("max_approvals") is not None else 8),
            max_usage=None if usage is None else float(usage),
        )


def _used_exceeds(used: BudgetCost, limits: BudgetLimits) -> list[str]:
    bad: list[str] = []
    if used.wall_sec > float(limits.wall_sec) + 1e-9:
        bad.append("wall_sec")
    if used.attempts > int(limits.max_attempts):
        bad.append("attempts")
    if used.reworks > int(limits.max_reworks):
        bad.append("reworks")
    if used.approvals > int(limits.max_approvals):
        bad.append("approvals")
    if limits.max_usage is not None and used.usage > float(limits.max_usage) + 1e-9:
        bad.append("usage")
    return bad


def remaining_of(limits: BudgetLimits, used: BudgetCost) -> BudgetCost:
    usage_left = (
        float("inf")
        if limits.max_usage is None
        else max(0.0, float(limits.max_usage) - float(used.usage))
    )
    return BudgetCost(
        wall_sec=max(0.0, float(limits.wall_sec) - float(used.wall_sec)),
        attempts=max(0, int(limits.max_attempts) - int(used.attempts)),
        reworks=max(0, int(limits.max_reworks) - int(used.reworks)),
        approvals=max(0, int(limits.max_approvals) - int(used.approvals)),
        usage=usage_left if usage_left != float("inf") else 0.0,
    )


@dataclass
class Reservation:
    reservation_id: str
    goal_id: str
    task_id: str = ""
    run_id: str = ""
    cost: BudgetCost = field(default_factory=BudgetCost)
    granted: bool = False
    status: str = STATUS_DENIED
    reason: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "cost": self.cost.to_dict(),
            "granted": bool(self.granted),
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "Reservation | None":
        if not isinstance(d, Mapping) or not d.get("reservation_id"):
            return None
        return cls(
            reservation_id=str(d.get("reservation_id") or ""),
            goal_id=str(d.get("goal_id") or ""),
            task_id=str(d.get("task_id") or ""),
            run_id=str(d.get("run_id") or ""),
            cost=BudgetCost.from_dict(d.get("cost") if isinstance(d.get("cost"), Mapping) else {}),
            granted=bool(d.get("granted")),
            status=str(d.get("status") or STATUS_DENIED),
            reason=str(d.get("reason") or ""),
            created_at=float(d.get("created_at") or 0.0),
        )


def limits_from_charter(
    charter: Mapping[str, Any] | None,
    goal: Mapping[str, Any] | None = None,
) -> BudgetLimits:
    """Build limits from Goal.budget / charter fields. Generous defaults."""
    ch = charter if isinstance(charter, Mapping) else {}
    g = goal if isinstance(goal, Mapping) else {}
    gb = g.get("budget") if isinstance(g.get("budget"), Mapping) else {}
    cb = ch.get("budget") if isinstance(ch.get("budget"), Mapping) else {}

    def _num(keys: tuple[str, ...], default: float, conv=float):
        for src in (cb, gb, ch):
            for k in keys:
                if k in src and src.get(k) is not None:
                    try:
                        return conv(src.get(k))
                    except (TypeError, ValueError):
                        continue
        return default

    wall = _num(("wall_sec", "timeout_sec"), 360.0, float)
    if wall < 1:
        wall = 1.0
    reworks = int(_num(("max_reworks",), 1, int))
    attempts = _num(("max_attempts",), None, int)
    if attempts is None:
        attempts = max(8, int(reworks) + 1)
    approvals = _num(("max_approvals", "max_lead_calls"), None, int)
    if approvals is None:
        approvals = max(8, int(attempts))
    usage_raw = None
    for src in (cb, gb, ch):
        if src.get("max_usage") is not None:
            try:
                usage_raw = float(src.get("max_usage"))
            except (TypeError, ValueError):
                usage_raw = None
            break
    return BudgetLimits(
        wall_sec=float(wall),
        max_attempts=int(attempts),
        max_reworks=max(0, int(reworks)),
        max_approvals=max(0, int(approvals)),
        max_usage=usage_raw,
    )


def estimate_dispatch_cost(
    charter: Mapping[str, Any] | None,
    *,
    remaining_wall: float,
    attempt_n: int = 1,
    need_review: bool = False,
) -> BudgetCost:
    """Hold attempt/approval/rework counts plus a small wall slice.

    Do not reserve the entire remaining Goal wall — that races the live
    deadline and starves sibling Tasks. Live ``exhausted_wall`` still blocks.
    """
    rem = max(0.0, float(remaining_wall))
    slice_sec = min(1.0, rem) if rem > 0 else 0.0
    return BudgetCost(
        wall_sec=slice_sec,
        attempts=1,
        reworks=1 if int(attempt_n) > 1 else 0,
        approvals=1 if need_review else 0,
        usage=0.0,
    )


def persist_path(persist_dir: str | Path, goal_id: str) -> Path:
    root = Path(persist_dir)
    return root / BUDGET_DIRNAME / f"{_safe_goal_id(goal_id)}.json"


_CACHE: dict[tuple[str, str], "GoalBudget"] = {}
_CACHE_MU = threading.Lock()


def reset_goal_budget_cache() -> None:
    with _CACHE_MU:
        _CACHE.clear()


class GoalBudget:
    """Parent Goal account. Child Task/Run costs roll up; new ids do not reset."""

    def __init__(
        self,
        *,
        goal_id: str,
        persist_dir: str | Path,
        limits: BudgetLimits | None = None,
        started_at: float | None = None,
        wall_deadline: float | None = None,
    ) -> None:
        self.goal_id = str(goal_id or "").strip() or "goal"
        self.persist_dir = Path(persist_dir)
        self.limits = limits or BudgetLimits()
        now = _utc_now()
        self.started_at = float(started_at if started_at is not None else now)
        self.wall_deadline = float(
            wall_deadline if wall_deadline is not None else (self.started_at + float(self.limits.wall_sec))
        )
        self.consumed = BudgetCost()
        self.reserved = BudgetCost()
        self.reservations: dict[str, Reservation] = {}
        self.ledger: list[dict[str, Any]] = []
        self._mu = threading.RLock()
        self.notes: list[str] = []

    # --- identity / persist ---

    @property
    def path(self) -> Path:
        return persist_path(self.persist_dir, self.goal_id)

    def remaining_wall_sec(self) -> float:
        return max(0.0, self.wall_deadline - _utc_now())

    def exhausted_wall(self) -> bool:
        return _utc_now() >= self.wall_deadline

    def committed(self) -> BudgetCost:
        return self.consumed.add(self.reserved)

    def remaining(self) -> BudgetCost:
        used = self.committed()
        # wall remaining is the tighter of account wall_sec vs live deadline
        live_wall = min(remaining_of(self.limits, used).wall_sec, self.remaining_wall_sec())
        rem = remaining_of(self.limits, used)
        rem.wall_sec = live_wall
        return rem

    def over_budget(self) -> bool:
        if self.exhausted_wall() and (self.consumed.attempts > 0 or self.reserved.attempts > 0):
            # wall clock expired after work started — still over if consumed exceeds
            pass
        dims = _used_exceeds(self.consumed, self.limits)
        if self.exhausted_wall() and self.consumed.wall_sec > float(self.limits.wall_sec):
            if "wall_sec" not in dims:
                dims.append("wall_sec")
        return bool(dims) or bool(_used_exceeds(self.committed(), self.limits))

    def over_budget_dims(self) -> list[str]:
        dims = _used_exceeds(self.consumed, self.limits)
        extra = _used_exceeds(self.committed(), self.limits)
        out: list[str] = []
        for d in dims + extra:
            if d not in out:
                out.append(d)
        if self.exhausted_wall() and "wall_sec" not in out:
            # live wall expiry blocks further dispatch even if accounted wall_sec is under limit
            out.append("wall_sec")
        return out

    def can_reserve(self, cost: BudgetCost) -> tuple[bool, str]:
        if self.exhausted_wall() or self.remaining_wall_sec() <= 0:
            return False, REASON_BUDGET_EXHAUSTED
        projected = self.committed().add(cost)
        bad = _used_exceeds(projected, self.limits)
        if bad:
            return False, REASON_BUDGET_EXHAUSTED
        return True, REASON_READY

    def reserve(
        self,
        reservation_id: str = "",
        *,
        task_id: str = "",
        run_id: str = "",
        cost: BudgetCost | None = None,
    ) -> Reservation:
        """Hold capacity before a child Task/Run starts. Does not mint a new account."""
        rid = (reservation_id or "").strip() or f"rsv_{uuid.uuid4().hex[:12]}"
        cost = cost or BudgetCost(attempts=1)
        with self._mu:
            ok, reason = self.can_reserve(cost)
            rec = Reservation(
                reservation_id=rid,
                goal_id=self.goal_id,
                task_id=task_id,
                run_id=run_id or rid,
                cost=cost,
                granted=ok,
                status=STATUS_HELD if ok else STATUS_DENIED,
                reason=reason if ok else REASON_BUDGET_EXHAUSTED,
                created_at=_utc_now(),
            )
            if ok:
                self.reserved = self.reserved.add(cost)
                self.reservations[rid] = rec
                self.ledger.append(
                    {
                        "op": "reserve",
                        "reservation_id": rid,
                        "task_id": task_id,
                        "run_id": rec.run_id,
                        "cost": cost.to_dict(),
                        "at": rec.created_at,
                    }
                )
                self.notes.append(
                    f"reserve {rid} task={task_id} attempts={cost.attempts} "
                    f"reworks={cost.reworks} approvals={cost.approvals} wall={cost.wall_sec:.3f}"
                )
            else:
                self.ledger.append(
                    {
                        "op": "reserve_denied",
                        "reservation_id": rid,
                        "task_id": task_id,
                        "reason": rec.reason,
                        "cost": cost.to_dict(),
                        "at": rec.created_at,
                    }
                )
                self.notes.append(f"reserve denied {rid} task={task_id} reason={rec.reason}")
            self._persist_unlocked()
            return rec

    def reconcile(
        self,
        reservation_id: str,
        actual: BudgetCost | None = None,
    ) -> dict[str, Any]:
        """Commit actual child cost; release unused reservation. New ids do not reset."""
        actual = actual or BudgetCost()
        with self._mu:
            rec = self.reservations.get(reservation_id)
            if rec is None or rec.status != STATUS_HELD:
                # still charge actual against the parent (roll-up must not be lost)
                self.consumed = self.consumed.add(actual)
                self.ledger.append(
                    {
                        "op": "reconcile_unheld",
                        "reservation_id": reservation_id,
                        "actual": actual.to_dict(),
                        "at": _utc_now(),
                    }
                )
                over = self.over_budget()
                self._persist_unlocked()
                return {
                    "ok": not over,
                    "over_budget": over,
                    "held": False,
                    "consumed": self.consumed.to_dict(),
                }
            self.reserved = self.reserved.sub(rec.cost)
            self.consumed = self.consumed.add(actual)
            rec.status = STATUS_RECONCILED
            rec.granted = True
            over = bool(_used_exceeds(self.consumed, self.limits))
            self.ledger.append(
                {
                    "op": "reconcile",
                    "reservation_id": reservation_id,
                    "task_id": rec.task_id,
                    "run_id": rec.run_id,
                    "reserved": rec.cost.to_dict(),
                    "actual": actual.to_dict(),
                    "released": rec.cost.sub(actual).to_dict(),
                    "over_budget": over,
                    "at": _utc_now(),
                }
            )
            self.notes.append(
                f"reconcile {reservation_id} actual attempts={actual.attempts} "
                f"reworks={actual.reworks} approvals={actual.approvals} "
                f"wall={actual.wall_sec:.3f} over_budget={over}"
            )
            self._persist_unlocked()
            return {
                "ok": not over,
                "over_budget": over,
                "held": True,
                "consumed": self.consumed.to_dict(),
                "reserved": self.reserved.to_dict(),
                "dims": _used_exceeds(self.consumed, self.limits),
            }

    def release(self, reservation_id: str) -> bool:
        """Drop a held reservation without consuming (did not start / aborted)."""
        with self._mu:
            rec = self.reservations.get(reservation_id)
            if rec is None or rec.status != STATUS_HELD:
                return False
            self.reserved = self.reserved.sub(rec.cost)
            rec.status = STATUS_RELEASED
            rec.granted = False
            self.ledger.append(
                {
                    "op": "release",
                    "reservation_id": reservation_id,
                    "task_id": rec.task_id,
                    "at": _utc_now(),
                }
            )
            self._persist_unlocked()
            return True

    def to_dict(self) -> dict[str, Any]:
        with self._mu:
            return {
                "goal_id": self.goal_id,
                "contract_version": "contract.v0.1-draft",
                "limits": self.limits.to_dict(),
                "consumed": self.consumed.to_dict(),
                "reserved": self.reserved.to_dict(),
                "committed": self.committed().to_dict(),
                "remaining": self.remaining().to_dict(),
                "remaining_wall_sec": self.remaining_wall_sec(),
                "started_at": self.started_at,
                "wall_deadline": self.wall_deadline,
                "over_budget": self.over_budget(),
                "over_budget_dims": self.over_budget_dims(),
                "reservations": {k: v.to_dict() for k, v in self.reservations.items()},
                "ledger": list(self.ledger),
                "notes": list(self.notes),
                "persist_path": str(self.path),
            }

    def _persist_unlocked(self) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "goal_id": self.goal_id,
                "limits": self.limits.to_dict(),
                "consumed": self.consumed.to_dict(),
                "reserved": self.reserved.to_dict(),
                "started_at": self.started_at,
                "wall_deadline": self.wall_deadline,
                "reservations": {k: v.to_dict() for k, v in self.reservations.items()},
                "ledger": list(self.ledger),
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
        """Restore consumed/reserved/deadline. Never resets because a new id was minted."""
        with self._mu:
            if payload.get("started_at") is not None:
                self.started_at = float(payload["started_at"])
            if payload.get("wall_deadline") is not None:
                self.wall_deadline = float(payload["wall_deadline"])
            self.consumed = BudgetCost.from_dict(payload.get("consumed") if isinstance(payload.get("consumed"), Mapping) else {})
            self.reserved = BudgetCost.from_dict(payload.get("reserved") if isinstance(payload.get("reserved"), Mapping) else {})
            self.reservations = {}
            raw_rs = payload.get("reservations") if isinstance(payload.get("reservations"), Mapping) else {}
            for k, v in raw_rs.items():
                rec = Reservation.from_dict(v if isinstance(v, Mapping) else {})
                if rec is not None:
                    self.reservations[str(k)] = rec
            self.ledger = [dict(x) for x in (payload.get("ledger") or []) if isinstance(x, Mapping)]
            self.notes = [str(x) for x in (payload.get("notes") or [])]
            # limits stay as constructed unless payload has them AND we haven't been given explicit ones
            if isinstance(payload.get("limits"), Mapping):
                # Keep original wall_deadline; do not extend limits.wall_sec on reload
                loaded = BudgetLimits.from_dict(payload["limits"])
                self.limits = loaded

    @classmethod
    def open(
        cls,
        goal_id: str,
        persist_dir: str | Path,
        *,
        limits: BudgetLimits | None = None,
        use_cache: bool = True,
    ) -> "GoalBudget":
        """Load existing Goal account or create. New task/run ids are not a new account."""
        gid = str(goal_id or "").strip() or "goal"
        root = Path(persist_dir)
        key = (str(root.resolve()) if root.exists() else str(root), gid)
        if use_cache:
            with _CACHE_MU:
                hit = _CACHE.get(key)
                if hit is not None:
                    return hit
        acc = cls(goal_id=gid, persist_dir=root, limits=limits)
        path = persist_path(root, gid)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                acc.load_into(payload)
                acc.notes.append("loaded existing Goal budget; minting a new id does not reset")
        else:
            if limits is not None:
                acc.limits = limits
            acc.notes.append("opened new Goal budget account")
            acc._persist_unlocked()
        if use_cache:
            with _CACHE_MU:
                _CACHE[key] = acc
        return acc


def open_goal_budget(
    goal_id: str,
    persist_dir: str | Path,
    *,
    limits: BudgetLimits | None = None,
    charter: Mapping[str, Any] | None = None,
    goal: Mapping[str, Any] | None = None,
) -> GoalBudget:
    lim = limits or limits_from_charter(charter, goal)
    return GoalBudget.open(goal_id, persist_dir, limits=lim)


def stable_budget_goal_id(charter: Mapping[str, Any] | None, name: str = "") -> str:
    """Stable Goal key so a freshly minted task/run id cannot reset the account."""
    from framework.id_projection import stable_goal_task_ids

    ch = charter if isinstance(charter, Mapping) else {}
    explicit = str(ch.get("goal_id") or "").strip()
    if explicit:
        return explicit
    job = name or str(ch.get("name") or "job")
    gid, _ = stable_goal_task_ids(job, dict(ch) if ch else None)
    return gid


def queued_for_deps_result(
    *,
    name: str = "",
    unsatisfied: list[str],
    charter: dict | None = None,
) -> dict[str, Any]:
    job = name or (str((charter or {}).get("name") or "") if isinstance(charter, dict) else "")
    return {
        "ok": False,
        "state": "queued",
        "error": "unsatisfied deps: " + ", ".join(unsatisfied),
        "error_class": None,
        "unsatisfied_deps": list(unsatisfied),
        "used_public_api_only": True,
        "backend": "inprocess.local_v1",
        "artifacts": [],
        "name": job,
        "notes": [f"deps unsatisfied; stay queued: {unsatisfied}"],
        "dry_run": False,
        "attempt": 0,
        "rework_used_new_run": False,
        "wall_deadline_unchanged": True,
        "force_lead_review": False,
        "session_id": "",
        "run_id": "",
        "pending_seen": False,
        "pending_summaries": [],
        "path": "inprocess.local_v1",
        "entered_running": False,
        "gate_reason": REASON_UNSATISFIED_DEPS,
    }


def budget_exhausted_result(
    *,
    name: str = "",
    charter: dict | None = None,
    budget: GoalBudget | None = None,
    detail: str = "",
) -> dict[str, Any]:
    job = name or (str((charter or {}).get("name") or "") if isinstance(charter, dict) else "")
    snap = budget.to_dict() if budget is not None else None
    err = map_error_class(kind="budget_exhausted")
    msg = detail or "goal budget exhausted; dispatch refused"
    return {
        "ok": False,
        "state": "fail",
        "error": msg,
        "error_class": err,
        "goal_budget": snap,
        "used_public_api_only": True,
        "backend": "inprocess.local_v1",
        "artifacts": [],
        "name": job,
        "notes": [msg, "over-budget must not report success"],
        "dry_run": False,
        "attempt": 0,
        "rework_used_new_run": False,
        "wall_deadline_unchanged": True,
        "force_lead_review": False,
        "session_id": "",
        "run_id": "",
        "pending_seen": False,
        "pending_summaries": [],
        "path": "inprocess.local_v1",
        "entered_running": False,
        "gate_reason": REASON_BUDGET_EXHAUSTED,
    }


def can_enter_running(
    *,
    charter: Mapping[str, Any] | None,
    name: str = "",
    task: Mapping[str, Any] | None = None,
    depends_on: Any = None,
    completed: Mapping[str, Any] | None = None,
    workdir: str | Path | None = None,
    extra_artifact_roots: Iterable[str | Path] | None = None,
    budget: GoalBudget | None = None,
    reserve_cost: BudgetCost | None = None,
    claim_registry: Any | None = None,
    holder_id: str = "",
    enforce_named_deps: bool | None = None,
) -> dict[str, Any]:
    """Gate queued→running: deps, then budget, then workdir claim.

    Does not mutate budget or claims. Caller reserves/claims after a ready result.
    """
    ch = charter if isinstance(charter, Mapping) else {}
    deps_raw = depends_on
    if deps_raw is None:
        if isinstance(task, Mapping) and task.get("depends_on") is not None:
            deps_raw = task.get("depends_on")
        else:
            deps_raw = ch.get("depends_on")
    missing = unsatisfied_deps(
        deps_raw,
        completed=completed,
        workdir=workdir,
        extra_artifact_roots=extra_artifact_roots,
        enforce_named_deps=enforce_named_deps,
    )
    if missing:
        return {
            "ready": False,
            "task_state": "queued",
            "reason": REASON_UNSATISFIED_DEPS,
            "unsatisfied_deps": missing,
            "name": name or str(ch.get("name") or ""),
        }
    if workdir is not None and claim_registry is not None:
        occ = claim_registry.occupancy(workdir)
        hid = (holder_id or "").strip()
        if occ is not None and occ.holder_id and occ.holder_id != hid:
            return {
                "ready": False,
                "task_state": "queued",
                "reason": REASON_WORKDIR_OCCUPIED,
                "occupancy": occ.to_dict() if hasattr(occ, "to_dict") else occ,
                "name": name or str(ch.get("name") or ""),
            }
    if budget is not None:
        cost = reserve_cost or estimate_dispatch_cost(
            ch,
            remaining_wall=budget.remaining_wall_sec(),
            attempt_n=1,
            need_review=bool(ch.get("force_lead_review")),
        )
        ok, reason = budget.can_reserve(cost)
        if not ok:
            return {
                "ready": False,
                "task_state": "failed",
                "reason": REASON_BUDGET_EXHAUSTED,
                "error_class": map_error_class(kind="budget_exhausted"),
                "reserve_cost": cost.to_dict(),
                "goal_budget": budget.to_dict(),
                "name": name or str(ch.get("name") or ""),
            }
    return {
        "ready": True,
        "task_state": "running",
        "reason": REASON_READY,
        "name": name or str(ch.get("name") or ""),
        "unsatisfied_deps": [],
    }


def mark_enter_running() -> None:
    """Lifecycle: queued → running is legal when the gate passed."""
    assert_transition("task", "queued", "running")


def mark_budget_block() -> None:
    """Lifecycle: queued → failed when dispatch cannot reserve."""
    assert_transition("task", "queued", "failed")


def attach_goal_budget(result: dict[str, Any], budget: GoalBudget | None) -> dict[str, Any]:
    if budget is None:
        return result
    result = dict(result)
    snap = budget.to_dict()
    result["goal_budget"] = snap
    result["goal_id"] = result.get("goal_id") or budget.goal_id
    if budget.over_budget() and result.get("ok"):
        result["ok"] = False
        result["state"] = "fail"
        result["error_class"] = map_error_class(kind="budget_exhausted")
        result["error"] = result.get("error") or "goal budget over after reconcile; success refused"
        result.setdefault("notes", []).append("over-budget must not report success")
    return result


def simulate_scheduler_goal_deps(
    jobs: list[dict[str, Any]],
    *,
    goal_budget: GoalBudget | None = None,
    persist_dir: str | Path | None = None,
    registry: Any | None = None,
    max_parallel: int = 2,
    max_ticks: int = 6,
    execute: bool = True,
    on_conflict: str = "block",
    claim_workdirs: bool = True,
) -> dict[str, Any]:
    """Scheduler ticks: unready deps stay queued; budget+claim checked before running.

    ``jobs`` items: ``{job_id, name, charter, workdir, depends_on?}``.
    Shared GoalBudget: child costs roll up. Over-budget jobs do not succeed.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from charter import job_name as charter_job_name
    from execution_backend.run_job_wire import run_inprocess_charter
    from execution_backend.workdir_claim import WorkdirClaimRegistry, resolve_workdir

    reg = registry if registry is not None else (WorkdirClaimRegistry() if claim_workdirs else None)
    slots: list[dict[str, Any]] = []
    shared_persist: Path | None = Path(persist_dir) if persist_dir is not None else None
    first_wd: Path | None = None

    for raw in jobs:
        charter = raw.get("charter") or {}
        name = str(raw.get("name") or (charter_job_name(charter) if charter else "job"))
        job_id = str(raw.get("job_id") or name)
        wd = resolve_workdir(raw.get("workdir") or ".")
        wd.mkdir(parents=True, exist_ok=True)
        if first_wd is None:
            first_wd = wd
        deps = raw.get("depends_on")
        if deps is None:
            deps = depends_on_strings(charter.get("depends_on"))
        gid = str(raw.get("goal_id") or (charter.get("goal_id") if isinstance(charter, dict) else "") or "")
        slots.append(
            {
                "job_id": job_id,
                "name": name,
                "charter": charter,
                "workdir": str(wd),
                "depends_on": list(deps) if isinstance(deps, list) else depends_on_strings(deps),
                "goal_id": gid,
                "state": "queued",
                "ok": None,
                "reason": "",
                "result": None,
                "entered_running": False,
            }
        )

    if shared_persist is None:
        shared_persist = first_wd or Path(".")

    if goal_budget is None:
        gid = next((s["goal_id"] for s in slots if s["goal_id"]), "") or stable_budget_goal_id(
            slots[0]["charter"] if slots else {}, slots[0]["name"] if slots else "job"
        )
        goal_budget = open_goal_budget(
            gid,
            shared_persist,
            charter=slots[0]["charter"] if slots else None,
        )
        for s in slots:
            if not s["goal_id"]:
                s["goal_id"] = gid

    completed: dict[str, Any] = {}
    ticks: list[dict[str, Any]] = []
    wall_deadline_at_start = goal_budget.wall_deadline

    for tick_n in range(int(max_ticks)):
        still = [s for s in slots if s["state"] == "queued"]
        if not still:
            break
        started: list[dict[str, Any]] = []
        queued_this: list[dict[str, Any]] = []
        failed_this: list[dict[str, Any]] = []

        for slot in still:
            if len(started) >= int(max_parallel):
                slot["reason"] = REASON_MAX_PARALLEL
                queued_this.append(slot)
                continue
            gate = can_enter_running(
                charter=slot["charter"],
                name=slot["name"],
                depends_on=slot["depends_on"],
                completed=completed,
                workdir=slot["workdir"],
                budget=goal_budget,
                claim_registry=reg,
                holder_id=slot["job_id"],
                enforce_named_deps=True,
            )
            if not gate.get("ready"):
                reason = str(gate.get("reason") or "")
                slot["reason"] = reason
                slot["gate"] = {k: gate[k] for k in gate if k != "goal_budget"}
                if reason == REASON_BUDGET_EXHAUSTED:
                    mark_budget_block()
                    slot["state"] = "failed"
                    slot["ok"] = False
                    slot["error_class"] = map_error_class(kind="budget_exhausted")
                    slot["result"] = budget_exhausted_result(
                        name=slot["name"],
                        charter=slot["charter"],
                        budget=goal_budget,
                    )
                    failed_this.append(slot)
                else:
                    # unsatisfied deps / workdir: stay queued (blocked when claim refuses)
                    wait_state = "queued"
                    if reason == REASON_WORKDIR_OCCUPIED and str(on_conflict).lower() in (
                        "block",
                        "blocked",
                        "fail",
                        "reject",
                    ):
                        wait_state = "blocked"
                    _sim_outbox_task(
                        shared_persist,
                        slot,
                        wait_state,
                        reason,
                        extra={"unsatisfied_deps": (gate.get("unsatisfied_deps") or [])},
                    )
                    queued_this.append(slot)
                continue
            mark_enter_running()
            slot["entered_running"] = True
            slot["state"] = "running"
            slot["reason"] = REASON_READY
            _sim_outbox_task(shared_persist, slot, "running", REASON_READY)
            started.append(slot)

        executed: list[dict[str, Any]] = []
        if execute and started:

            def _run(slot: dict[str, Any]) -> dict[str, Any]:
                ch = slot["charter"]
                try:
                    result = run_inprocess_charter(
                        charter=ch,
                        workdir=slot["workdir"],
                        name=slot["name"],
                        goal_budget=goal_budget,
                        completed=completed,
                        skip_dep_check=True,
                        skip_budget=False,
                        claim_registry=reg,
                        holder_id=slot["job_id"],
                        on_conflict=on_conflict,
                    )
                except Exception as e:  # noqa: BLE001
                    return {
                        "ok": False,
                        "state": "fail",
                        "error": str(e),
                        "error_class": "implementation_failed",
                        "name": slot["name"],
                    }
                return attach_goal_budget(result, goal_budget)

            if len(started) == 1:
                started[0]["result"] = _run(started[0])
                executed.append(started[0])
            else:
                with ThreadPoolExecutor(max_workers=len(started)) as pool:
                    futs = {pool.submit(_run, s): s for s in started}
                    for fut in as_completed(futs):
                        slot = futs[fut]
                        slot["result"] = fut.result()
                        executed.append(slot)

            for slot in executed:
                res = slot.get("result") or {}
                if res.get("ok"):
                    slot["state"] = "succeeded"
                    slot["ok"] = True
                    completed[slot["name"]] = res
                    completed[slot["job_id"]] = res
                    if res.get("task_id"):
                        completed[str(res.get("task_id"))] = res
                else:
                    st = str(res.get("state") or "")
                    if st == "queued":
                        slot["state"] = "queued"
                        slot["ok"] = False
                        slot["reason"] = res.get("gate_reason") or slot.get("reason") or REASON_UNSATISFIED_DEPS
                    else:
                        slot["state"] = "failed"
                        slot["ok"] = False
                        slot["error_class"] = res.get("error_class")
        elif started:
            for slot in started:
                slot["state"] = "running"

        ticks.append(
            {
                "tick": tick_n,
                "started": [s["name"] for s in started],
                "queued": [s["name"] for s in queued_this],
                "failed": [s["name"] for s in failed_this],
                "queued_reasons": {s["name"]: s.get("reason") for s in queued_this},
                "failed_reasons": {s["name"]: s.get("reason") for s in failed_this},
            }
        )
        # progress: if nobody started and nobody newly failed, remaining queued are stuck
        if not started and not failed_this:
            break

    wall_unchanged = goal_budget.wall_deadline == wall_deadline_at_start
    producer_like = slots[0] if slots else None
    consumer_like = slots[1] if len(slots) > 1 else None
    queued_unready = [
        s
        for s in slots
        if s["state"] == "queued" and s.get("reason") == REASON_UNSATISFIED_DEPS
    ]
    any_over = any(
        (s.get("result") or {}).get("error_class") == "budget_exhausted" or s.get("error_class") == "budget_exhausted"
        for s in slots
    )
    any_false_success = any(bool(s.get("ok")) and (s.get("error_class") == "budget_exhausted") for s in slots)
    all_ok = all(s.get("ok") is True for s in slots) if slots else False
    consumer_waited = False
    if consumer_like is not None and ticks:
        first = ticks[0]
        consumer_waited = consumer_like["name"] in (first.get("queued") or []) and (
            producer_like is not None and producer_like["name"] in (first.get("started") or [])
        )
        if not consumer_waited:
            # also true if consumer never entered running on tick 0
            consumer_waited = not (
                consumer_like["name"] in (first.get("started") or []) and not first.get("queued")
            ) and (producer_like is not None)

    if all_ok and not any_false_success and goal_budget is not None:
        _sim_outbox_goal(shared_persist, str(goal_budget.goal_id), "completed", "all_tasks_succeeded")

    return {
        "ok": all_ok and not any_false_success,
        "slots": slots,
        "ticks": ticks,
        "tick_count": len(ticks),
        "goal_budget": goal_budget.to_dict(),
        "goal_id": goal_budget.goal_id,
        "wall_deadline_unchanged": wall_unchanged,
        "deps_blocked_unready": bool(consumer_waited) or bool(queued_unready) or (
            len(ticks) >= 1
            and consumer_like is not None
            and consumer_like["name"] in (ticks[0].get("queued") or [])
        ),
        "consumer_queued_until_producer": bool(
            consumer_like is not None
            and ticks
            and consumer_like["name"] in (ticks[0].get("queued") or [])
        ),
        "over_budget": bool(goal_budget.over_budget() or any_over),
        "over_budget_reported_success": bool(any_false_success),
        "completed_names": [k for k, v in completed.items() if isinstance(v, dict) and v.get("ok")],
        "execute": execute,
        "max_parallel": max_parallel,
    }


def run_dependent_inprocess_jobs(
    *,
    producer_charter: dict,
    consumer_charter: dict,
    workdir: str | Path,
    workdir_consumer: str | Path | None = None,
    goal_budget: GoalBudget | None = None,
    limits: BudgetLimits | None = None,
    persist_dir: str | Path | None = None,
    max_parallel: int = 2,
) -> dict[str, Any]:
    """Two inprocess jobs: consumer depends on producer. Shared Goal budget."""
    from execution_backend.workdir_claim import resolve_workdir

    wd = resolve_workdir(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    wdc = resolve_workdir(workdir_consumer) if workdir_consumer is not None else wd
    wdc.mkdir(parents=True, exist_ok=True)
    pdir = Path(persist_dir) if persist_dir is not None else wd
    gid = stable_budget_goal_id(producer_charter, str(producer_charter.get("name") or "producer"))
    if goal_budget is None:
        goal_budget = open_goal_budget(
            gid,
            pdir,
            limits=limits,
            charter=producer_charter,
        )
    jobs = [
        {
            "job_id": "job-producer",
            "name": str(producer_charter.get("name") or "producer"),
            "charter": producer_charter,
            "workdir": wd,
            "goal_id": goal_budget.goal_id,
        },
        {
            "job_id": "job-consumer",
            "name": str(consumer_charter.get("name") or "consumer"),
            "charter": consumer_charter,
            "workdir": wdc,
            "goal_id": goal_budget.goal_id,
        },
    ]
    report = simulate_scheduler_goal_deps(
        jobs,
        goal_budget=goal_budget,
        persist_dir=pdir,
        max_parallel=max_parallel,
        max_ticks=6,
        execute=True,
    )
    slots = report.get("slots") or []
    prod = next((s for s in slots if s.get("name") == jobs[0]["name"]), slots[0] if slots else {})
    cons = next((s for s in slots if s.get("name") == jobs[1]["name"]), slots[1] if len(slots) > 1 else {})
    report["producer"] = prod.get("result") if isinstance(prod, dict) else None
    report["consumer"] = cons.get("result") if isinstance(cons, dict) else None
    report["producer_slot"] = prod
    report["consumer_slot"] = cons
    return report


def _sim_outbox_ids(slot: Mapping[str, Any]) -> tuple[str, str]:
    try:
        from framework.outbox import ids_for_outbox

        gid = str(slot.get("goal_id") or "")
        charter = slot.get("charter") if isinstance(slot.get("charter"), Mapping) else {}
        g2, tid = ids_for_outbox(charter, str(slot.get("name") or slot.get("job_id") or "job"))
        return gid or g2, tid
    except Exception:  # noqa: BLE001
        return str(slot.get("goal_id") or ""), str(slot.get("job_id") or "")


def _sim_outbox_task(
    persist_dir: str | Path | None,
    slot: Mapping[str, Any],
    new_state: str,
    reason: str,
    *,
    from_state: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    if persist_dir is None:
        return
    try:
        from framework.outbox import record_transition

        gid, tid = _sim_outbox_ids(slot)
        record_transition(
            persist_dir,
            {"kind": "task", "task_id": tid, "goal_id": gid},
            new_state,
            from_state=from_state,
            reason=str(reason or ""),
            extra=extra,
            goal_id=gid,
            task_id=tid,
        )
    except Exception:  # noqa: BLE001
        return


def _sim_outbox_goal(
    persist_dir: str | Path | None,
    goal_id: str,
    new_state: str,
    reason: str,
) -> None:
    if persist_dir is None or not goal_id:
        return
    try:
        from framework.outbox import record_transition

        record_transition(
            persist_dir,
            {"kind": "goal", "goal_id": goal_id},
            new_state,
            reason=reason,
            goal_id=goal_id,
        )
    except Exception:  # noqa: BLE001
        return


__all__ = [
    "BUDGET_DIRNAME",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_READY",
    "REASON_UNSATISFIED_DEPS",
    "REASON_WORKDIR_OCCUPIED",
    "BudgetCost",
    "BudgetLimits",
    "GoalBudget",
    "GoalBudgetError",
    "Reservation",
    "attach_goal_budget",
    "budget_exhausted_result",
    "can_enter_running",
    "estimate_dispatch_cost",
    "limits_from_charter",
    "mark_budget_block",
    "mark_enter_running",
    "open_goal_budget",
    "persist_path",
    "queued_for_deps_result",
    "reset_goal_budget_cache",
    "run_dependent_inprocess_jobs",
    "simulate_scheduler_goal_deps",
    "stable_budget_goal_id",
]
