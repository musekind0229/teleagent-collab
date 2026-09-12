"""Knife 14: durable/perpetual Goal layer — file-backed, no transport.

Public ops:
  ``submit_goal``, ``get_goal``, ``resolve_decision``, ``cancel_goal``,
  ``get_report``.

``submit_goal`` is idempotent by submit key: a duplicate must not open a
second Goal. ``resolve_decision`` targets exactly one pending decision;
a fuzzy batch of unrelated actions is refused. ``cancel_goal`` stops
accepting new child tasks and requests terminate of in-flight work —
``cancel_requested`` is not ``cancelled``.

The kernel does **not** promote identity/memory (no 晋升身份记忆).

Persisted under ``<persist_dir>/.collab-durable/``.
Public kernel path only. No TeleAgent HTTP. No Hermes ledger. No glue rewrite.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from framework.delegation import (
    ESCALATE_KINDS,
    EVENT_CLASS_DECISION,
    EVENT_CLASS_STATUS,
    KIND_ESCALATE_INSUFFICIENT_AUTH,
    KIND_ESCALATE_OUT_OF_SCOPE,
    KIND_ESCALATE_OVER_BUDGET,
    REASON_AUTONOMY_DENIED,
    REASON_UNKNOWN_AUTONOMY,
    REASON_UNKNOWN_ESCALATE,
    autonomy_is_known,
    event_class_for,
    is_escalate_kind,
    normalize_autonomy,
    normalize_escalate_kind,
    undeclared_autonomy,
)
from framework.lifecycle import (
    GOAL_STATES,
    TASK_STATES,
    LifecycleError,
    assert_transition,
)
from framework.models import CONTRACT_VERSION, contract_fingerprint, new_goal_id, new_task_id

DURABLE_DIRNAME = ".collab-durable"
STORE_FILENAME = "store.json"

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"

REASON_READY = "ready"
REASON_DUPLICATE_SUBMIT = "duplicate_submit"
REASON_UNKNOWN_GOAL = "unknown_goal"
REASON_EMPTY_SUBMIT_KEY = "empty_submit_key"
REASON_UNRELATED_BATCH = "unrelated_batch"
REASON_FUZZY_BATCH = "fuzzy_batch"
REASON_NO_PENDING = "no_pending_decision"
REASON_AMBIGUOUS_PENDING = "ambiguous_pending"
REASON_UNKNOWN_DECISION = "unknown_decision"
REASON_ALREADY_RESOLVED = "already_resolved"
REASON_ADMISSION_CLOSED = "admission_closed"
REASON_ALREADY_TERMINAL = "already_terminal"
REASON_NOT_CANCEL_REQUESTED = "not_cancel_requested"
REASON_IDENTITY_MEMORY_FORBIDDEN = "identity_memory_forbidden"
REASON_ILLEGAL_STATE = "illegal_state"
REASON_EMPTY_VERDICT = "empty_verdict"
REASON_SUBMIT_CONTENT_CONFLICT = "submit_content_conflict"
REASON_ACTOR_REQUIRED = "actor_required"
REASON_NOT_COORDINATOR = "not_coordinator"
REASON_STALE_OWNERSHIP = "stale_ownership"
REASON_ACTOR_NOT_AUTHORIZED = "actor_not_authorized"
REASON_NO_OWNER = "no_owner"

DECISION_KINDS = frozenset(
    {
        "action_approval",
        "question",
        "artifact_review",
        "plan_review",
        KIND_ESCALATE_OVER_BUDGET,
        KIND_ESCALATE_OUT_OF_SCOPE,
        KIND_ESCALATE_INSUFFICIENT_AUTH,
        "return_to_upper",
    }
) | set(ESCALATE_KINDS)

_IN_FLIGHT_TASK = frozenset({"running", "awaiting_decision", "review"})
_TERMINAL_GOAL = frozenset({"completed", "failed", "cancelled"})
_CANCEL_CLOSED = frozenset({"cancel_requested", "cancelled"})

# Kernel must never treat these as first-class durable records.
_IDENTITY_MEMORY_KEYS = frozenset(
    {
        "identity",
        "memory",
        "identity_memory",
        "promoted_identity",
        "promoted_memory",
        "promote_identity",
        "promote_memory",
        "identity_promotion",
        "memory_promotion",
        "晋升身份记忆",
    }
)

_BATCH_KEYS = frozenset(
    {"decisions", "batch", "items", "ops", "operations", "payloads"}
)
_MIXED_OP_KEYS = frozenset(
    {
        "cancel_goal",
        "submit_goal",
        "add_child_task",
        "add_task",
        "effect_cancel",
        "promote_identity",
        "promote_memory",
        "identity",
        "memory",
    }
)


class DurableError(RuntimeError):
    """Durable-layer protocol failure (not a success)."""


def kernel_promotes_identity_memory() -> bool:
    """Hard no: the kernel does not promote identity/memory."""
    return False


def _utc_now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    t = ts if ts is not None else _utc_now()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def persist_dir(root: str | Path) -> Path:
    return Path(root) / DURABLE_DIRNAME


def store_path(root: str | Path) -> Path:
    return persist_dir(root) / STORE_FILENAME


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    tmp.write_text(data, encoding="utf-8")
    try:
        fd = os.open(str(tmp), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def new_decision_id() -> str:
    return f"dec_{uuid.uuid4().hex[:12]}"


def _norm_key(value: Any) -> str:
    return str(value or "").strip()


def _identity_memory_hits(payload: Any, *, _depth: int = 0) -> list[str]:
    if _depth > 4 or not isinstance(payload, Mapping):
        return []
    hits: list[str] = []
    for k, v in payload.items():
        key = _norm_key(k)
        if key.lower() in {x.lower() for x in _IDENTITY_MEMORY_KEYS} or key in _IDENTITY_MEMORY_KEYS:
            hits.append(key)
        if isinstance(v, Mapping):
            hits.extend(_identity_memory_hits(v, _depth=_depth + 1))
    return hits


def _forbid_identity_memory(*payloads: Any) -> str | None:
    for p in payloads:
        hits = _identity_memory_hits(p)
        if hits:
            return (
                "kernel does not promote identity/memory "
                f"(forbidden keys: {', '.join(sorted(set(hits)))})"
            )
    return None


def _as_mapping_list(value: Any) -> list[dict[str, Any]] | None:
    """Return a list of mappings, or None if value is absent/empty-non-list."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (str, bytes)):
        return None
    if isinstance(value, Sequence):
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                out.append(dict(item))
            else:
                return None
        return out
    return None


def _target_id(item: Mapping[str, Any]) -> str:
    return _norm_key(
        item.get("decision_id")
        or item.get("request_id")
        or item.get("id")
        or ""
    )


def _unrelated_batch_reason(
    *,
    decision_id: str = "",
    request_id: str = "",
    actions: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> str | None:
    """Refuse a fuzzy batch of unrelated actions / mixed ops.

    One pending decision (optionally with related action rows that share
    that decision's id) is allowed. Multiple distinct targets, a
    ``decisions``/``batch`` list of length > 1, or mixed ops
    (cancel/submit/promote) in the same call are refused.
    """
    extra = extra if isinstance(extra, Mapping) else {}
    mixed = [k for k in extra if _norm_key(k) in _MIXED_OP_KEYS or _norm_key(k).lower() in _MIXED_OP_KEYS]
    if mixed:
        return REASON_UNRELATED_BATCH

    collected: list[dict[str, Any]] = []
    for key in _BATCH_KEYS:
        raw = extra.get(key) if key in extra else None
        if raw is None:
            continue
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) > 1:
            return REASON_UNRELATED_BATCH
        parsed = _as_mapping_list(raw)
        if parsed is None:
            return REASON_FUZZY_BATCH
        collected.extend(parsed)

    action_list = _as_mapping_list(actions)
    if actions is not None and action_list is None:
        if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)) and len(actions) > 1:
            return REASON_UNRELATED_BATCH
        return REASON_FUZZY_BATCH
    if action_list:
        collected.extend(action_list)

    if isinstance(decision_id, (list, tuple)) and len(decision_id) > 1:
        return REASON_UNRELATED_BATCH
    if isinstance(request_id, (list, tuple)) and len(request_id) > 1:
        return REASON_UNRELATED_BATCH

    hinted = {_norm_key(decision_id), _norm_key(request_id)} - {""}
    ids: set[str] = set(hinted)
    kinds: set[str] = set()
    for item in collected:
        tid = _target_id(item)
        if tid:
            ids.add(tid)
        kind = _norm_key(item.get("kind") or item.get("op") or item.get("action") or "")
        if kind:
            kinds.add(kind)
        if any(_norm_key(k) in _MIXED_OP_KEYS for k in item):
            return REASON_UNRELATED_BATCH
    if len(ids) > 1:
        return REASON_UNRELATED_BATCH
    # Distinct action kinds across a multi-item bag without a shared id → fuzzy.
    if len(collected) > 1 and len(ids) == 0 and len(kinds) > 1:
        return REASON_UNRELATED_BATCH
    return None


def _submit_content_fingerprint(
    *,
    title: str,
    desired_outcome: str,
    goal: Mapping[str, Any] | None,
    tasks: Sequence[Mapping[str, Any]] | None,
    submitter_id: str = "",
    external_goal_ref: str = "",
    autonomy: Any = None,
    coordinator_id: str = "",
) -> str:
    """Stable fingerprint of submit payload (ids / submit_key excluded)."""
    body = _default_goal_body(
        goal_id="_fp",
        title=title,
        desired_outcome=desired_outcome,
        goal=goal,
    )
    body.pop("goal_id", None)
    body.pop("idempotency_key", None)
    body.pop("contract_version", None)
    hints = dict(body.get("role_hints") or {}) if isinstance(body.get("role_hints"), Mapping) else {}
    cid = _norm_key(coordinator_id)
    if cid:
        hints["coordinator"] = cid
    spec = normalize_autonomy(autonomy)
    if autonomy_is_known(spec):
        hints["autonomy"] = spec["mode"]
        body["role_hints"] = hints
    elif hints:
        body["role_hints"] = hints
    task_rows: list[dict[str, Any]] = []
    for item in tasks or []:
        if not isinstance(item, Mapping):
            continue
        rec = _task_record(item, goal_id="_fp")
        rec.pop("goal_id", None)
        rec.pop("contract_version", None)
        if not _norm_key(item.get("task_id")):
            rec.pop("task_id", None)
        task_rows.append(rec)
    payload: dict[str, Any] = {"goal": body, "tasks": task_rows}
    sid = _norm_key(submitter_id)
    if sid:
        payload["submitter_id"] = sid
    xref = _norm_key(external_goal_ref)
    if xref:
        payload["external_goal_ref"] = xref
    if autonomy_is_known(spec):
        payload["autonomy"] = {
            "mode": spec["mode"],
            "allow_local_plan": bool(spec.get("allow_local_plan")),
            "allow_rework": bool(spec.get("allow_rework")),
            "allow_reassign": bool(spec.get("allow_reassign")),
        }
    return contract_fingerprint(payload)


def _fingerprint_of_snap(snap: Mapping[str, Any]) -> str:
    stored = _norm_key(snap.get("submit_fingerprint"))
    if stored:
        return stored
    g = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
    return _submit_content_fingerprint(
        title=str(g.get("title") or ""),
        desired_outcome=str(g.get("desired_outcome") or ""),
        goal=g if isinstance(g, Mapping) else None,
        tasks=[t for t in (snap.get("tasks") or []) if isinstance(t, Mapping)],
    )


def _ownership_store_if_present(persist_root: str | Path, goal_id: str):
    """Open Goal ownership only when a store file already exists (do not mint)."""
    from framework.goal_ownership import GoalOwnershipStore, persist_path

    path = persist_path(persist_root, goal_id)
    if not path.is_file():
        return None
    return GoalOwnershipStore.open(goal_id, persist_root)


def _ownership_projection(persist_root: str | Path, goal_id: str) -> dict[str, Any] | None:
    store = _ownership_store_if_present(persist_root, goal_id)
    if store is None:
        return None
    own = store.current()
    if not own.coordinator_id and int(own.version) <= 0 and int(store.plan_revision) <= 0:
        return None
    cid = own.coordinator_id
    return {
        "ownership": {
            "coordinator_id": cid,
            "version": int(own.version),
            "readonly": True,
        },
        "plan_revision": {
            "coordinator_id": cid,
            "version": int(store.plan_revision),
            "readonly": True,
        },
    }


def _authorize_resolve_actor(
    persist_root: str | Path,
    goal_id: str,
    *,
    actor_id: str,
    ownership_version: Any = None,
    decision_kind: str = "",
    return_to_upper: bool = False,
    submitter_id: str = "",
) -> tuple[bool, str]:
    """Bind resolve to a caller identity.

    P0: if the Goal has a coordinator, actor must be that live instance.
    P1 return-to-upper / escalate: the recorded submitter (upper) may resolve,
    as may the live coordinator. Silent retry is not an authorization path.
    """
    store = _ownership_store_if_present(persist_root, goal_id)
    own = store.current() if store is not None else None
    coordinator = ""
    if own is not None and own.coordinator_id and int(own.version) > 0:
        coordinator = own.coordinator_id

    escalate = is_escalate_kind(decision_kind, return_to_upper=return_to_upper)
    if escalate:
        allowed = {x for x in (_norm_key(submitter_id), _norm_key(coordinator)) if x}
        if not allowed:
            return True, REASON_READY
        if actor_id not in allowed:
            return False, REASON_ACTOR_NOT_AUTHORIZED
        if coordinator and actor_id == coordinator and ownership_version not in (None, ""):
            try:
                ver = int(ownership_version)
            except (TypeError, ValueError):
                return False, REASON_STALE_OWNERSHIP
            if own is not None and ver != int(own.version):
                return False, REASON_STALE_OWNERSHIP
        return True, REASON_READY

    if store is None or not coordinator:
        return True, REASON_READY
    if actor_id != coordinator:
        return False, REASON_NOT_COORDINATOR
    if ownership_version is None or ownership_version == "":
        return True, REASON_READY
    try:
        ver = int(ownership_version)
    except (TypeError, ValueError):
        return False, REASON_STALE_OWNERSHIP
    if own is not None and ver != int(own.version):
        return False, REASON_STALE_OWNERSHIP
    return True, REASON_READY


def _default_goal_body(
    *,
    goal_id: str,
    title: str,
    desired_outcome: str,
    goal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    src = dict(goal) if isinstance(goal, Mapping) else {}
    title_v = _norm_key(src.get("title") or title) or "goal"
    outcome = _norm_key(src.get("desired_outcome") or desired_outcome) or title_v
    boundaries = src.get("boundaries") if isinstance(src.get("boundaries"), Mapping) else {}
    must = list(boundaries.get("must") or src.get("must") or [])
    must_not = list(boundaries.get("must_not") or src.get("must_not") or [])
    body: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "goal_id": goal_id,
        "title": title_v,
        "desired_outcome": outcome,
        "boundaries": {
            "must": [str(x) for x in must],
            "must_not": [str(x) for x in must_not],
        },
        "acceptance": dict(src.get("acceptance") or {}) if isinstance(src.get("acceptance"), Mapping) else {},
        "budget": dict(src.get("budget") or {"wall_sec": 360})
        if isinstance(src.get("budget"), Mapping)
        else {"wall_sec": 360},
    }
    if "idempotency_key" in src:
        body["idempotency_key"] = str(src.get("idempotency_key") or "")
    if isinstance(src.get("role_hints"), Mapping):
        body["role_hints"] = dict(src["role_hints"])
    for extra_key in ("capability_requirements", "platform_allowlist", "context_refs", "source_charter_path"):
        if extra_key in src:
            body[extra_key] = src[extra_key]
    return body


def _apply_delegation_hints(
    body: dict[str, Any],
    *,
    coordinator_id: str = "",
    autonomy: Mapping[str, Any] | None = None,
    submitter_id: str = "",
) -> dict[str, Any]:
    hints = dict(body.get("role_hints") or {}) if isinstance(body.get("role_hints"), Mapping) else {}
    cid = _norm_key(coordinator_id)
    if cid:
        hints["coordinator"] = cid
    if autonomy_is_known(autonomy):
        hints["autonomy"] = str(autonomy.get("mode") or "")
    sid = _norm_key(submitter_id)
    if sid:
        hints["submitter"] = sid
    if hints:
        body["role_hints"] = hints
    return body


def _task_record(task: Mapping[str, Any] | None, *, goal_id: str, default_status: str = "queued") -> dict[str, Any]:
    src = dict(task) if isinstance(task, Mapping) else {}
    tid = _norm_key(src.get("task_id")) or new_task_id()
    status = _norm_key(src.get("status") or default_status) or "queued"
    if status not in TASK_STATES:
        raise DurableError(f"illegal task status {status!r}")
    rec: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "task_id": tid,
        "goal_id": goal_id,
        "title": _norm_key(src.get("title")) or tid,
        "status": status,
        "depends_on": list(src.get("depends_on") or []) if isinstance(src.get("depends_on"), list) else [],
        "inputs": dict(src.get("inputs") or {}) if isinstance(src.get("inputs"), Mapping) else {},
        "expected_artifacts": list(src.get("expected_artifacts") or [])
        if isinstance(src.get("expected_artifacts"), list)
        else [],
        "done_when": dict(src.get("done_when") or {}) if isinstance(src.get("done_when"), Mapping) else {},
        "terminate_requested": bool(src.get("terminate_requested")),
    }
    return rec


def _empty_snapshot(*, goal_id: str, submit_key: str, goal: Mapping[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    return {
        "contract_version": CONTRACT_VERSION,
        "goal_id": goal_id,
        "submit_key": submit_key,
        "state": "queued",
        "cancel_requested": False,
        "cancelled": False,
        "accepting_child_tasks": True,
        "goal": dict(goal),
        "tasks": [],
        "pending_decisions": [],
        "resolved_decisions": [],
        "history": [],
        "notes": [],
        "submitter_id": "",
        "external_goal_ref": "",
        "autonomy": undeclared_autonomy(),
        "created_at": now,
        "created_at_iso": _iso(now),
        "updated_at": now,
        "updated_at_iso": _iso(now),
        "cancel_requested_at": None,
        "cancel_requested_at_iso": "",
        "cancelled_at": None,
        "cancelled_at_iso": "",
    }


def _delegation_fields_from(
    *,
    extra: Mapping[str, Any] | None,
    goal: Mapping[str, Any] | None,
    submitter_id: str = "",
    external_goal_ref: str = "",
    autonomy: Any = None,
    coordinator_id: str = "",
) -> dict[str, Any]:
    extra = extra if isinstance(extra, Mapping) else {}
    src = goal if isinstance(goal, Mapping) else {}
    hints = src.get("role_hints") if isinstance(src.get("role_hints"), Mapping) else {}
    sid = _norm_key(
        submitter_id
        or extra.get("submitter_id")
        or extra.get("submitter")
        or src.get("submitter_id")
        or src.get("submitter")
        or hints.get("submitter")
    )
    xref = _norm_key(
        external_goal_ref
        or extra.get("external_goal_ref")
        or extra.get("external_ref")
        or src.get("external_goal_ref")
    )
    raw_auto = (
        autonomy
        if autonomy is not None
        else extra.get("autonomy")
        if extra.get("autonomy") is not None
        else src.get("autonomy")
        if src.get("autonomy") is not None
        else hints.get("autonomy")
    )
    cid = _norm_key(
        coordinator_id
        or extra.get("coordinator_id")
        or extra.get("coordinator")
        or src.get("coordinator_id")
        or hints.get("coordinator")
    )
    return {
        "submitter_id": sid,
        "external_goal_ref": xref,
        "autonomy": raw_auto,
        "coordinator_id": cid,
    }


_CACHE: dict[str, "DurableLayer"] = {}
_CACHE_MU = threading.Lock()


def reset_durable_cache() -> None:
    with _CACHE_MU:
        _CACHE.clear()


class DurableLayer:
    """File-backed perpetual Goal store. Not a transport."""

    def __init__(self, persist_dir_root: str | Path) -> None:
        self.persist_root = Path(persist_dir_root)
        self.submit_keys: dict[str, str] = {}
        self.goals: dict[str, dict[str, Any]] = {}
        self.notes: list[str] = []
        self._mu = threading.RLock()

    @property
    def path(self) -> Path:
        return persist_dir(self.persist_root)

    def store_file(self) -> Path:
        return store_path(self.persist_root)

    def goal_count(self) -> int:
        with self._mu:
            return len(self.goals)

    def _persist_unlocked(self) -> None:
        payload = {
            "contract_version": CONTRACT_VERSION,
            "submit_keys": dict(self.submit_keys),
            "goals": {gid: dict(snap) for gid, snap in self.goals.items()},
            "notes": list(self.notes)[-50:],
        }
        _atomic_write(self.store_file(), payload)
        goals_dir = self.path / "goals"
        goals_dir.mkdir(parents=True, exist_ok=True)
        for gid, snap in self.goals.items():
            slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in gid)[:80] or "goal"
            _atomic_write(goals_dir / f"{slug}.json", snap)

    def persist(self) -> Path:
        with self._mu:
            self._persist_unlocked()
            return self.store_file()

    def load_into(self, payload: Mapping[str, Any]) -> None:
        with self._mu:
            keys = payload.get("submit_keys") if isinstance(payload.get("submit_keys"), Mapping) else {}
            self.submit_keys = {str(k): str(v) for k, v in keys.items() if str(k) and str(v)}
            raw_goals = payload.get("goals") if isinstance(payload.get("goals"), Mapping) else {}
            self.goals = {}
            for gid, snap in raw_goals.items():
                if isinstance(snap, Mapping) and str(gid):
                    self.goals[str(gid)] = dict(snap)
            self.notes = [str(x) for x in (payload.get("notes") or [])]

    @classmethod
    def open(cls, persist_dir_root: str | Path, *, use_cache: bool = True) -> "DurableLayer":
        root = Path(persist_dir_root)
        key = str(root.resolve()) if root.exists() else str(root)
        if use_cache:
            with _CACHE_MU:
                hit = _CACHE.get(key)
                if hit is not None:
                    return hit
        layer = cls(root)
        raw = _read_json(store_path(root))
        if isinstance(raw, dict):
            layer.load_into(raw)
            layer.notes.append("loaded existing durable store")
        else:
            layer.notes.append("opened empty durable store")
            layer._persist_unlocked()
        if use_cache:
            with _CACHE_MU:
                _CACHE[key] = layer
        return layer

    def _copy_goal(self, snap: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(snap)
        out["goal"] = dict(snap.get("goal") or {})
        out["tasks"] = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        out["pending_decisions"] = [
            dict(d) for d in (snap.get("pending_decisions") or []) if isinstance(d, Mapping)
        ]
        out["resolved_decisions"] = [
            dict(d) for d in (snap.get("resolved_decisions") or []) if isinstance(d, Mapping)
        ]
        out["history"] = [dict(h) for h in (snap.get("history") or []) if isinstance(h, Mapping)]
        out["notes"] = list(snap.get("notes") or [])
        if isinstance(snap.get("autonomy"), Mapping):
            out["autonomy"] = dict(snap.get("autonomy") or {})
        return out

    def _touch(self, snap: dict[str, Any], *, now: float | None = None) -> None:
        ts = now if now is not None else _utc_now()
        snap["updated_at"] = ts
        snap["updated_at_iso"] = _iso(ts)

    def _append_history(self, snap: dict[str, Any], op: str, **fields: Any) -> None:
        now = _utc_now()
        rec = {"op": op, "at": now, "at_iso": _iso(now)}
        rec.update(fields)
        hist = list(snap.get("history") or [])
        hist.append(rec)
        snap["history"] = hist[-200:]

    def _set_state(self, snap: dict[str, Any], new_state: str) -> None:
        src = _norm_key(snap.get("state") or "queued") or "queued"
        dst = _norm_key(new_state)
        if dst not in GOAL_STATES:
            raise DurableError(f"unknown goal state {dst!r}")
        if src != dst:
            try:
                assert_transition("goal", src, dst)
            except LifecycleError as e:
                raise DurableError(str(e)) from e
        snap["state"] = dst
        snap["cancelled"] = dst == "cancelled"
        if dst == "cancel_requested":
            snap["cancel_requested"] = True
        self._touch(snap)

    def submit_goal(
        self,
        *,
        submit_key: str,
        title: str = "",
        desired_outcome: str = "",
        goal: Mapping[str, Any] | None = None,
        tasks: Sequence[Mapping[str, Any]] | None = None,
        extra: Mapping[str, Any] | None = None,
        submitter_id: str = "",
        external_goal_ref: str = "",
        autonomy: Any = None,
        coordinator_id: str = "",
    ) -> dict[str, Any]:
        """Open a Goal, or return the existing one for this submit key.

        Duplicate submit with the same key does **not** open a second Goal.
        Same key + different payload fingerprint is ``submit_content_conflict``
        (the first Goal is kept; the new payload is not applied).
        P1: records submitter / external ref / autonomy; claims coordinator
        when ``coordinator_id`` is provided (existing claim/handoff).
        """
        key = _norm_key(submit_key)
        if not key:
            return {
                "ok": False,
                "reason": REASON_EMPTY_SUBMIT_KEY,
                "error": "submit_key required",
                "created": False,
            }
        forbidden = _forbid_identity_memory(goal, extra, {"title": title, "desired_outcome": desired_outcome})
        if forbidden:
            return {
                "ok": False,
                "reason": REASON_IDENTITY_MEMORY_FORBIDDEN,
                "error": forbidden,
                "created": False,
            }
        extra = extra if isinstance(extra, Mapping) else {}
        if extra:
            hit = _forbid_identity_memory(extra)
            if hit:
                return {
                    "ok": False,
                    "reason": REASON_IDENTITY_MEMORY_FORBIDDEN,
                    "error": hit,
                    "created": False,
                }
        fields = _delegation_fields_from(
            extra=extra,
            goal=goal,
            submitter_id=submitter_id,
            external_goal_ref=external_goal_ref,
            autonomy=autonomy,
            coordinator_id=coordinator_id,
        )
        spec = normalize_autonomy(fields["autonomy"])
        if spec.get("unknown"):
            return {
                "ok": False,
                "reason": REASON_UNKNOWN_AUTONOMY,
                "error": f"unknown autonomy mode {spec.get('mode')!r}",
                "created": False,
            }
        incoming_fp = _submit_content_fingerprint(
            title=title or key,
            desired_outcome=desired_outcome,
            goal=goal,
            tasks=list(tasks) if tasks else None,
            submitter_id=fields["submitter_id"],
            external_goal_ref=fields["external_goal_ref"],
            autonomy=spec if autonomy_is_known(spec) else None,
            coordinator_id=fields["coordinator_id"],
        )
        with self._mu:
            existing_id = self.submit_keys.get(key)
            if existing_id and existing_id in self.goals:
                stored = self.goals[existing_id]
                stored_fp = _fingerprint_of_snap(stored)
                if stored_fp != incoming_fp:
                    snap = self._copy_goal(stored)
                    return {
                        "ok": False,
                        "reason": REASON_SUBMIT_CONTENT_CONFLICT,
                        "error": (
                            "submit_key bound to a different payload; "
                            "content conflict is not a silent replay"
                        ),
                        "created": False,
                        "duplicate": False,
                        "opened": False,
                        "goal_id": existing_id,
                        "goal": snap,
                        "goal_count": len(self.goals),
                    }
                snap = self._copy_goal(stored)
                return {
                    "ok": True,
                    "reason": REASON_DUPLICATE_SUBMIT,
                    "created": False,
                    "duplicate": True,
                    "opened": False,
                    "goal_id": existing_id,
                    "goal": snap,
                    "goal_count": len(self.goals),
                }
            gid = _norm_key((goal or {}).get("goal_id") if isinstance(goal, Mapping) else "") or new_goal_id(
                title or key
            )
            # Never collide with an already-open Goal id.
            if gid in self.goals:
                gid = new_goal_id(title or key)
            body = _default_goal_body(
                goal_id=gid,
                title=title or key,
                desired_outcome=desired_outcome,
                goal=goal,
            )
            body["idempotency_key"] = key
            _apply_delegation_hints(
                body,
                coordinator_id=fields["coordinator_id"],
                autonomy=spec,
                submitter_id=fields["submitter_id"],
            )
            snap = _empty_snapshot(goal_id=gid, submit_key=key, goal=body)
            snap["submit_fingerprint"] = incoming_fp
            snap["submitter_id"] = fields["submitter_id"]
            snap["external_goal_ref"] = fields["external_goal_ref"]
            snap["autonomy"] = spec
            child_tasks: list[dict[str, Any]] = []
            for item in tasks or []:
                if isinstance(item, Mapping):
                    child_tasks.append(_task_record(item, goal_id=gid))
            snap["tasks"] = child_tasks
            if child_tasks:
                self._set_state(snap, "running")
            claimed = None
            if fields["coordinator_id"]:
                from framework.goal_ownership import GoalOwnershipStore, extract_goal_contract

                store = GoalOwnershipStore.open(gid, self.persist_root)
                initial_plan = None
                if child_tasks:
                    initial_plan = {
                        "goal_id": gid,
                        "tasks": [dict(t) for t in child_tasks],
                        "title": body.get("title"),
                        "desired_outcome": body.get("desired_outcome"),
                        "acceptance": body.get("acceptance"),
                    }
                claimed = store.claim(
                    fields["coordinator_id"],
                    initial_plan=initial_plan,
                    autonomy=spec,
                    goal_contract=extract_goal_contract(body) or body,
                )
                if not claimed.get("ok"):
                    return {
                        "ok": False,
                        "reason": claimed.get("reason") or "claim_failed",
                        "error": claimed.get("error") or "coordinator claim refused",
                        "created": False,
                        "ownership": (claimed.get("ownership") or {}),
                    }
            self._append_history(
                snap,
                "submit_goal",
                submit_key=key,
                task_count=len(child_tasks),
                submitter_id=fields["submitter_id"],
                autonomy_mode=spec.get("mode"),
                coordinator_id=fields["coordinator_id"],
            )
            snap["notes"] = list(snap.get("notes") or []) + [f"opened via submit_key={key}"]
            self.goals[gid] = snap
            self.submit_keys[key] = gid
            self._persist_unlocked()
            copied = self._copy_goal(snap)
            out = {
                "ok": True,
                "reason": REASON_READY,
                "created": True,
                "duplicate": False,
                "opened": True,
                "goal_id": gid,
                "goal": copied,
                "goal_count": len(self.goals),
                "submitter_id": fields["submitter_id"],
                "external_goal_ref": fields["external_goal_ref"],
                "autonomy": dict(spec),
            }
            proj = _ownership_projection(self.persist_root, gid)
            if proj:
                out["ownership"] = proj["ownership"]
                out["plan_revision"] = proj["plan_revision"]
                copied["ownership"] = proj["ownership"]
                copied["plan_revision"] = proj["plan_revision"]
                out["goal"] = copied
            return out

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        """Read the current Goal snapshot."""
        gid = _norm_key(goal_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {
                    "ok": False,
                    "reason": REASON_UNKNOWN_GOAL,
                    "error": f"unknown goal_id {gid!r}",
                    "goal_id": gid,
                }
            copied = self._copy_goal(snap)
            auto = copied.get("autonomy") if isinstance(copied.get("autonomy"), Mapping) else undeclared_autonomy()
            out = {
                "ok": True,
                "reason": REASON_READY,
                "goal_id": gid,
                "state": copied.get("state"),
                "cancel_requested": bool(copied.get("cancel_requested")),
                "cancelled": bool(copied.get("cancelled")) or copied.get("state") == "cancelled",
                "goal": copied,
                "submitter_id": copied.get("submitter_id") or "",
                "external_goal_ref": copied.get("external_goal_ref") or "",
                "autonomy": dict(auto),
            }
            proj = _ownership_projection(self.persist_root, gid)
            if proj:
                out["ownership"] = proj["ownership"]
                out["plan_revision"] = proj["plan_revision"]
                copied["ownership"] = proj["ownership"]
                copied["plan_revision"] = proj["plan_revision"]
                out["goal"] = copied
            return out

    def add_child_task(
        self,
        goal_id: str,
        *,
        title: str = "",
        task: Mapping[str, Any] | None = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        """Admit a new child Task. Refused after cancel_goal (admission closed)."""
        gid = _norm_key(goal_id)
        forbidden = _forbid_identity_memory(task)
        if forbidden:
            return {
                "ok": False,
                "reason": REASON_IDENTITY_MEMORY_FORBIDDEN,
                "error": forbidden,
            }
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {
                    "ok": False,
                    "reason": REASON_UNKNOWN_GOAL,
                    "error": f"unknown goal_id {gid!r}",
                }
            if not snap.get("accepting_child_tasks") or snap.get("state") in _CANCEL_CLOSED:
                return {
                    "ok": False,
                    "reason": REASON_ADMISSION_CLOSED,
                    "error": "goal is not accepting new child tasks",
                    "goal_id": gid,
                    "state": snap.get("state"),
                    "cancel_requested": bool(snap.get("cancel_requested")),
                    "cancelled": bool(snap.get("cancelled")),
                }
            body = dict(task) if isinstance(task, Mapping) else {}
            if title and not body.get("title"):
                body["title"] = title
            if status and not body.get("status"):
                body["status"] = status
            rec = _task_record(body, goal_id=gid)
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            tasks.append(rec)
            snap["tasks"] = tasks
            if snap.get("state") == "queued":
                self._set_state(snap, "running")
            self._append_history(snap, "add_child_task", task_id=rec["task_id"])
            self._touch(snap)
            self._persist_unlocked()
            return {
                "ok": True,
                "reason": REASON_READY,
                "goal_id": gid,
                "task": rec,
                "state": snap.get("state"),
            }

    def start_task(self, goal_id: str, task_id: str) -> dict[str, Any]:
        """Move a queued child Task to running (in-flight)."""
        gid = _norm_key(goal_id)
        tid = _norm_key(task_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {"ok": False, "reason": REASON_UNKNOWN_GOAL, "error": f"unknown goal_id {gid!r}"}
            if snap.get("state") in _CANCEL_CLOSED:
                return {
                    "ok": False,
                    "reason": REASON_ADMISSION_CLOSED,
                    "error": "goal cancel_requested; not starting new work",
                    "state": snap.get("state"),
                }
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            found = None
            for t in tasks:
                if t.get("task_id") == tid:
                    found = t
                    break
            if found is None:
                return {"ok": False, "reason": "unknown_task", "error": f"unknown task_id {tid!r}"}
            src = _norm_key(found.get("status") or "queued") or "queued"
            if src != "running":
                try:
                    assert_transition("task", src, "running")
                except LifecycleError as e:
                    return {"ok": False, "reason": REASON_ILLEGAL_STATE, "error": str(e)}
            found["status"] = "running"
            snap["tasks"] = tasks
            if snap.get("state") == "queued":
                self._set_state(snap, "running")
            self._append_history(snap, "start_task", task_id=tid)
            self._touch(snap)
            self._persist_unlocked()
            return {"ok": True, "reason": REASON_READY, "task": found, "state": snap.get("state")}

    def open_decision(
        self,
        goal_id: str,
        *,
        kind: str = "action_approval",
        task_id: str = "",
        run_id: str = "",
        decision_id: str = "",
        request_id: str = "",
        actions: Sequence[Mapping[str, Any]] | None = None,
        title: str = "",
        return_to_upper: bool = False,
        details: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Record one pending decision (test/kernel helper; not a transport)."""
        gid = _norm_key(goal_id)
        escalated = normalize_escalate_kind(kind)
        knd = escalated or _norm_key(kind) or "action_approval"
        if knd not in DECISION_KINDS:
            return {"ok": False, "reason": "unknown_kind", "error": f"unknown decision kind {knd!r}"}
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {"ok": False, "reason": REASON_UNKNOWN_GOAL, "error": f"unknown goal_id {gid!r}"}
            did = _norm_key(decision_id) or new_decision_id()
            rid = _norm_key(request_id) or did
            related: list[dict[str, Any]] = []
            for item in actions or []:
                if isinstance(item, Mapping):
                    row = dict(item)
                    row.setdefault("decision_id", did)
                    row.setdefault("request_id", rid)
                    related.append(row)
            to_upper = bool(return_to_upper) or is_escalate_kind(knd)
            rec = {
                "contract_version": CONTRACT_VERSION,
                "decision_id": did,
                "request_id": rid,
                "kind": knd,
                "goal_id": gid,
                "task_id": _norm_key(task_id),
                "run_id": _norm_key(run_id),
                "status": STATUS_PENDING,
                "title": _norm_key(title),
                "actions": related,
                "verdict": "",
                "reason": _norm_key(reason),
                "created_at": _utc_now(),
                "created_at_iso": _iso(),
                "return_to_upper": to_upper,
                "silent_retry": False if to_upper else None,
                "event_class": EVENT_CLASS_DECISION if to_upper else EVENT_CLASS_STATUS,
                "details": dict(details) if isinstance(details, Mapping) else {},
            }
            pending = [dict(d) for d in (snap.get("pending_decisions") or []) if isinstance(d, Mapping)]
            pending.append(rec)
            snap["pending_decisions"] = pending
            self._append_history(
                snap,
                "open_decision",
                decision_id=did,
                kind=knd,
                return_to_upper=to_upper,
                event_class=rec["event_class"],
            )
            self._touch(snap)
            self._persist_unlocked()
            return {"ok": True, "reason": REASON_READY, "decision": rec, "pending_count": len(pending)}

    def resolve_decision(
        self,
        goal_id: str,
        *,
        decision_id: str = "",
        request_id: str = "",
        verdict: str = "",
        reason: str = "",
        actions: Any = None,
        extra: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Resolve exactly one pending decision. Unrelated batches are refused.

        ``actor_id`` is required. If the Goal has a coordinator, the actor
        must be that coordinator (optional ``ownership_version`` must match
        when supplied). The actor is recorded on the resolution; this is not
        identity/memory promotion.
        """
        gid = _norm_key(goal_id)
        extra_map: dict[str, Any] = dict(extra) if isinstance(extra, Mapping) else {}
        extra_map.update(kwargs)
        forbidden = _forbid_identity_memory(
            extra_map,
            {"verdict": verdict, "reason": reason, "actions": actions, "decision_id": decision_id},
        )
        if forbidden:
            return {
                "ok": False,
                "reason": REASON_IDENTITY_MEMORY_FORBIDDEN,
                "error": forbidden,
            }
        actor = _norm_key(extra_map.get("actor_id") or extra_map.get("actor"))
        own_ver = extra_map.get("ownership_version")
        if own_ver is None:
            own_ver = extra_map.get("ownership_ver")
        extra_map.pop("actor_id", None)
        extra_map.pop("actor", None)
        extra_map.pop("ownership_version", None)
        extra_map.pop("ownership_ver", None)
        if not actor:
            return {
                "ok": False,
                "reason": REASON_ACTOR_REQUIRED,
                "error": "actor_id required",
                "goal_id": gid,
            }
        batch_why = _unrelated_batch_reason(
            decision_id=decision_id,
            request_id=request_id,
            actions=actions,
            extra=extra_map,
        )
        if batch_why:
            return {
                "ok": False,
                "reason": batch_why,
                "error": "resolve_decision accepts exactly one pending decision; unrelated batch refused",
                "goal_id": gid,
            }
        vdict = _norm_key(verdict) or _norm_key(extra_map.get("verdict"))
        rsn = _norm_key(reason) or _norm_key(extra_map.get("reason"))
        if not vdict:
            return {
                "ok": False,
                "reason": REASON_EMPTY_VERDICT,
                "error": "verdict required",
                "goal_id": gid,
            }
        did = _norm_key(decision_id) or _norm_key(extra_map.get("decision_id"))
        rid = _norm_key(request_id) or _norm_key(extra_map.get("request_id"))
        action_list = _as_mapping_list(actions) or []
        if not did and action_list:
            did = _target_id(action_list[0])
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {"ok": False, "reason": REASON_UNKNOWN_GOAL, "error": f"unknown goal_id {gid!r}"}
            pending = [dict(d) for d in (snap.get("pending_decisions") or []) if isinstance(d, Mapping)]
            resolved = [dict(d) for d in (snap.get("resolved_decisions") or []) if isinstance(d, Mapping)]
            target = None
            if did or rid:
                for d in pending:
                    if did and d.get("decision_id") == did:
                        target = d
                        break
                    if rid and (d.get("request_id") == rid or d.get("decision_id") == rid):
                        target = d
                        break
                if target is None:
                    for d in resolved:
                        if (did and d.get("decision_id") == did) or (
                            rid and (d.get("request_id") == rid or d.get("decision_id") == rid)
                        ):
                            return {
                                "ok": True,
                                "reason": REASON_ALREADY_RESOLVED,
                                "idempotent": True,
                                "goal_id": gid,
                                "decision": dict(d),
                            }
                    return {
                        "ok": False,
                        "reason": REASON_UNKNOWN_DECISION,
                        "error": f"unknown pending decision {did or rid!r}",
                        "goal_id": gid,
                    }
            else:
                if not pending:
                    return {
                        "ok": False,
                        "reason": REASON_NO_PENDING,
                        "error": "no pending decision",
                        "goal_id": gid,
                    }
                if len(pending) > 1:
                    return {
                        "ok": False,
                        "reason": REASON_AMBIGUOUS_PENDING,
                        "error": "exactly one pending decision must be named when several are open",
                        "goal_id": gid,
                        "pending_count": len(pending),
                    }
                target = pending[0]
            authed, auth_why = _authorize_resolve_actor(
                self.persist_root,
                gid,
                actor_id=actor,
                ownership_version=own_ver,
                decision_kind=str(target.get("kind") or ""),
                return_to_upper=bool(target.get("return_to_upper")),
                submitter_id=_norm_key(snap.get("submitter_id")),
            )
            if not authed:
                return {
                    "ok": False,
                    "reason": auth_why,
                    "error": (
                        "stale ownership instance; resolve refused"
                        if auth_why == REASON_STALE_OWNERSHIP
                        else f"actor {actor!r} is not authorized to resolve"
                    ),
                    "goal_id": gid,
                    "actor_id": actor,
                }
            # Related actions must share this decision's id.
            target_ids = {_norm_key(target.get("decision_id")), _norm_key(target.get("request_id"))} - {""}
            for item in action_list:
                tid = _target_id(item)
                if tid and tid not in target_ids:
                    return {
                        "ok": False,
                        "reason": REASON_UNRELATED_BATCH,
                        "error": "resolve_decision accepts exactly one pending decision; unrelated batch refused",
                        "goal_id": gid,
                    }
            target["status"] = STATUS_RESOLVED
            target["verdict"] = vdict
            target["reason"] = rsn or "resolved"
            target["actor_id"] = actor
            if own_ver is not None and own_ver != "":
                try:
                    target["ownership_version"] = int(own_ver)
                except (TypeError, ValueError):
                    target["ownership_version"] = own_ver
            target["resolved_at"] = _utc_now()
            target["resolved_at_iso"] = _iso()
            if action_list:
                target["resolved_actions"] = action_list
            remaining = [d for d in pending if d.get("decision_id") != target.get("decision_id")]
            snap["pending_decisions"] = remaining
            resolved.append(target)
            snap["resolved_decisions"] = resolved
            self._append_history(
                snap,
                "resolve_decision",
                decision_id=target.get("decision_id"),
                verdict=vdict,
                actor_id=actor,
            )
            self._touch(snap)
            self._persist_unlocked()
            return {
                "ok": True,
                "reason": REASON_READY,
                "actor_id": actor,
                "goal_id": gid,
                "decision": dict(target),
                "pending_count": len(remaining),
            }

    def escalate_to_upper(
        self,
        goal_id: str,
        *,
        kind: str,
        reason: str,
        details: Mapping[str, Any] | None = None,
        task_id: str = "",
        decision_id: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """Open a return-to-upper pending decision. Does not retry local work."""
        gid = _norm_key(goal_id)
        knd = normalize_escalate_kind(kind)
        if not knd:
            return {
                "ok": False,
                "reason": REASON_UNKNOWN_ESCALATE,
                "error": f"unknown escalate kind {kind!r}",
                "goal_id": gid,
            }
        rsn = _norm_key(reason)
        if not rsn:
            return {
                "ok": False,
                "reason": REASON_EMPTY_VERDICT,
                "error": "escalate reason required",
                "goal_id": gid,
            }
        opened = self.open_decision(
            gid,
            kind=knd,
            task_id=task_id,
            decision_id=decision_id,
            title=title or knd,
            return_to_upper=True,
            details=details,
            reason=rsn,
        )
        if not opened.get("ok"):
            return opened
        with self._mu:
            snap = self.goals.get(gid)
            if snap is not None:
                self._append_history(
                    snap,
                    "escalate_to_upper",
                    decision_id=(opened.get("decision") or {}).get("decision_id"),
                    kind=knd,
                    reason=rsn,
                    return_to_upper=True,
                    silent_retry=False,
                    event_class=EVENT_CLASS_DECISION,
                )
                self._touch(snap)
                self._persist_unlocked()
        rec = dict(opened.get("decision") or {})
        rec["silent_retry"] = False
        rec["return_to_upper"] = True
        return {
            "ok": True,
            "reason": REASON_READY,
            "goal_id": gid,
            "decision": rec,
            "pending_count": opened.get("pending_count"),
            "event_class": EVENT_CLASS_DECISION,
            "silent_retry": False,
            "return_to_upper": True,
        }

    def list_events(self, goal_id: str) -> dict[str, Any]:
        """History + pending notifications. Escalations are decision_required."""
        gid = _norm_key(goal_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {
                    "ok": False,
                    "reason": REASON_UNKNOWN_GOAL,
                    "error": f"unknown goal_id {gid!r}",
                    "goal_id": gid,
                }
            events: list[dict[str, Any]] = []
            for h in snap.get("history") or []:
                if not isinstance(h, Mapping):
                    continue
                rec = dict(h)
                rec["event_class"] = event_class_for(
                    kind=str(rec.get("kind") or ""),
                    op=str(rec.get("op") or ""),
                    return_to_upper=bool(rec.get("return_to_upper")),
                )
                rec["source"] = "history"
                events.append(rec)
            pending: list[dict[str, Any]] = []
            for d in snap.get("pending_decisions") or []:
                if not isinstance(d, Mapping):
                    continue
                row = dict(d)
                to_upper = bool(row.get("return_to_upper")) or is_escalate_kind(row.get("kind"))
                row["event_class"] = EVENT_CLASS_DECISION if to_upper else EVENT_CLASS_STATUS
                row["silent_retry"] = False if to_upper else row.get("silent_retry")
                pending.append(row)
            return {
                "ok": True,
                "reason": REASON_READY,
                "goal_id": gid,
                "events": events,
                "pending": pending,
                "pending_count": len(pending),
            }

    def handoff_coordinator(
        self,
        goal_id: str,
        *,
        from_coordinator_id: str,
        to_coordinator_id: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Wire existing ownership handoff into the durable layer."""
        gid = _norm_key(goal_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {
                    "ok": False,
                    "reason": REASON_UNKNOWN_GOAL,
                    "error": f"unknown goal_id {gid!r}",
                    "goal_id": gid,
                }
            from framework.goal_ownership import GoalOwnershipStore, persist_path

            path = persist_path(self.persist_root, gid)
            if not path.is_file():
                return {
                    "ok": False,
                    "reason": REASON_NO_OWNER,
                    "error": "no coordinator to hand off",
                    "goal_id": gid,
                }
            store = GoalOwnershipStore.open(gid, self.persist_root)
            out = store.handoff(
                from_coordinator_id,
                to_coordinator_id,
                expected_version=expected_version,
            )
            if out.get("ok"):
                self._append_history(
                    snap,
                    "handoff_coordinator",
                    from_coordinator_id=_norm_key(from_coordinator_id),
                    to_coordinator_id=_norm_key(to_coordinator_id),
                    version=(out.get("ownership") or {}).get("version"),
                )
                self._touch(snap)
                self._persist_unlocked()
            copied = self._copy_goal(snap)
            result = dict(out)
            result["goal_id"] = gid
            proj = _ownership_projection(self.persist_root, gid)
            if proj:
                result["ownership"] = proj["ownership"]
                result["plan_revision"] = proj["plan_revision"]
            result["goal"] = copied
            return result

    def cancel_goal(self, goal_id: str, *, reason: str = "") -> dict[str, Any]:
        """Request cancel: close admission + terminate-request in-flight.

        Lands on ``cancel_requested``. This is **not** terminal ``cancelled``.
        """
        gid = _norm_key(goal_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {"ok": False, "reason": REASON_UNKNOWN_GOAL, "error": f"unknown goal_id {gid!r}"}
            state = _norm_key(snap.get("state") or "")
            if state == "cancelled":
                return {
                    "ok": True,
                    "reason": REASON_ALREADY_TERMINAL,
                    "goal_id": gid,
                    "state": "cancelled",
                    "cancel_requested": True,
                    "cancelled": True,
                    "note": "already_terminal",
                }
            now = _utc_now()
            snap["accepting_child_tasks"] = False
            snap["cancel_requested"] = True
            if not snap.get("cancel_requested_at"):
                snap["cancel_requested_at"] = now
                snap["cancel_requested_at_iso"] = _iso(now)
            if state != "cancel_requested":
                if state in _TERMINAL_GOAL and state != "cancelled":
                    # completed/failed stay terminal; still record the request flag.
                    snap["notes"] = list(snap.get("notes") or []) + [
                        "cancel_requested ignored; already terminal"
                    ]
                else:
                    self._set_state(snap, "cancel_requested")
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            in_flight: list[str] = []
            for t in tasks:
                st = _norm_key(t.get("status") or "")
                if st in _IN_FLIGHT_TASK:
                    src = st
                    try:
                        if src != "cancel_requested":
                            assert_transition("task", src, "cancel_requested")
                        t["status"] = "cancel_requested"
                    except LifecycleError:
                        t["status"] = "cancel_requested"
                    t["terminate_requested"] = True
                    in_flight.append(str(t.get("task_id") or ""))
            snap["tasks"] = tasks
            snap["cancelled"] = False
            note = reason or "cancel_requested received (execution may still be running)"
            snap["notes"] = list(snap.get("notes") or []) + [note]
            self._append_history(
                snap,
                "cancel_goal",
                in_flight=in_flight,
                accepting_child_tasks=False,
            )
            self._touch(snap, now=now)
            self._persist_unlocked()
            copied = self._copy_goal(snap)
            return {
                "ok": True,
                "reason": REASON_READY,
                "goal_id": gid,
                "state": copied.get("state"),
                "cancel_requested": True,
                "cancelled": False,
                "accepting_child_tasks": False,
                "terminate_requested_task_ids": in_flight,
                "goal": copied,
            }

    def effect_cancel(self, goal_id: str, *, reason: str = "") -> dict[str, Any]:
        """Terminal cancelled — only after cancel_requested. Distinct from the request."""
        gid = _norm_key(goal_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {"ok": False, "reason": REASON_UNKNOWN_GOAL, "error": f"unknown goal_id {gid!r}"}
            state = _norm_key(snap.get("state") or "")
            if state == "cancelled":
                return {
                    "ok": True,
                    "reason": REASON_ALREADY_TERMINAL,
                    "idempotent": True,
                    "goal_id": gid,
                    "state": "cancelled",
                    "cancel_requested": True,
                    "cancelled": True,
                }
            if state != "cancel_requested":
                return {
                    "ok": False,
                    "reason": REASON_NOT_CANCEL_REQUESTED,
                    "error": "effect_cancel requires cancel_requested first (request ≠ effected)",
                    "goal_id": gid,
                    "state": state,
                    "cancel_requested": bool(snap.get("cancel_requested")),
                    "cancelled": False,
                }
            now = _utc_now()
            self._set_state(snap, "cancelled")
            snap["cancelled"] = True
            snap["cancelled_at"] = now
            snap["cancelled_at_iso"] = _iso(now)
            snap["accepting_child_tasks"] = False
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
            for t in tasks:
                st = _norm_key(t.get("status") or "")
                if st in ("cancel_requested", *_IN_FLIGHT_TASK, "queued", "blocked"):
                    t["status"] = "cancelled"
                    t["terminate_requested"] = True
            snap["tasks"] = tasks
            snap["notes"] = list(snap.get("notes") or []) + [
                reason or "cancel effected — execution stopped for this goal_id"
            ]
            self._append_history(snap, "effect_cancel")
            self._touch(snap, now=now)
            self._persist_unlocked()
            copied = self._copy_goal(snap)
            return {
                "ok": True,
                "reason": REASON_READY,
                "goal_id": gid,
                "state": "cancelled",
                "cancel_requested": True,
                "cancelled": True,
                "goal": copied,
            }

    def get_report(self, goal_id: str) -> dict[str, Any]:
        """Read-only report snapshot for a Goal."""
        gid = _norm_key(goal_id)
        with self._mu:
            snap = self.goals.get(gid)
            if snap is None:
                return {
                    "ok": False,
                    "reason": REASON_UNKNOWN_GOAL,
                    "error": f"unknown goal_id {gid!r}",
                    "goal_id": gid,
                }
            copied = self._copy_goal(snap)
            counts: dict[str, int] = {}
            for t in copied.get("tasks") or []:
                st = str(t.get("status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
            state = str(copied.get("state") or "")
            auto = copied.get("autonomy") if isinstance(copied.get("autonomy"), Mapping) else undeclared_autonomy()
            report = {
                "contract_version": CONTRACT_VERSION,
                "goal_id": gid,
                "submit_key": copied.get("submit_key"),
                "title": (copied.get("goal") or {}).get("title"),
                "desired_outcome": (copied.get("goal") or {}).get("desired_outcome"),
                "state": state,
                "ok": state == "completed",
                "cancel_requested": bool(copied.get("cancel_requested")),
                "cancelled": bool(copied.get("cancelled")) or state == "cancelled",
                "accepting_child_tasks": bool(copied.get("accepting_child_tasks")),
                "task_counts": counts,
                "task_count": len(copied.get("tasks") or []),
                "pending_decision_count": len(copied.get("pending_decisions") or []),
                "resolved_decision_count": len(copied.get("resolved_decisions") or []),
                "tasks": copied.get("tasks") or [],
                "pending_decisions": copied.get("pending_decisions") or [],
                "resolved_decisions": copied.get("resolved_decisions") or [],
                "notes": copied.get("notes") or [],
                "created_at_iso": copied.get("created_at_iso"),
                "updated_at_iso": copied.get("updated_at_iso"),
                "cancel_requested_at_iso": copied.get("cancel_requested_at_iso") or "",
                "cancelled_at_iso": copied.get("cancelled_at_iso") or "",
                "submitter_id": copied.get("submitter_id") or "",
                "external_goal_ref": copied.get("external_goal_ref") or "",
                "autonomy": dict(auto),
                "readonly": True,
                "kernel_promotes_identity_memory": False,
            }
            proj = _ownership_projection(self.persist_root, gid)
            if proj:
                report["ownership"] = proj["ownership"]
                report["plan_revision"] = proj["plan_revision"]
            return {
                "ok": True,
                "reason": REASON_READY,
                "goal_id": gid,
                "report": report,
                "state": state,
                "cancel_requested": report["cancel_requested"],
                "cancelled": report["cancelled"],
                "submitter_id": report["submitter_id"],
                "autonomy": report["autonomy"],
            }


def open_durable(persist_dir_root: str | Path) -> DurableLayer:
    return DurableLayer.open(persist_dir_root)


def submit_goal(persist_dir_root: str | Path, **kwargs: Any) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).submit_goal(**kwargs)


def get_goal(persist_dir_root: str | Path, goal_id: str) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).get_goal(goal_id)


def resolve_decision(persist_dir_root: str | Path, goal_id: str, **kwargs: Any) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).resolve_decision(goal_id, **kwargs)


def cancel_goal(persist_dir_root: str | Path, goal_id: str, **kwargs: Any) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).cancel_goal(goal_id, **kwargs)


def get_report(persist_dir_root: str | Path, goal_id: str) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).get_report(goal_id)


def effect_cancel(persist_dir_root: str | Path, goal_id: str, **kwargs: Any) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).effect_cancel(goal_id, **kwargs)


def list_events(persist_dir_root: str | Path, goal_id: str) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).list_events(goal_id)


def escalate_to_upper(persist_dir_root: str | Path, goal_id: str, **kwargs: Any) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).escalate_to_upper(goal_id, **kwargs)


def handoff_coordinator(persist_dir_root: str | Path, goal_id: str, **kwargs: Any) -> dict[str, Any]:
    return DurableLayer.open(persist_dir_root).handoff_coordinator(goal_id, **kwargs)
