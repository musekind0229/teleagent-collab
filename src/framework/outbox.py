"""Knife 13: transactional entity state + durable outbox + receiver dedup.

State change and outbound event rows commit in the same file-journal
transaction. Outbox rows go pending → claimed → sent. Crash before
``mark_sent`` leaves the row reclaimable (at-least-once delivery).
The receiver deduplicates by event_id / delivery_key — at-least-once
delivery is not the same as the external side effect happening only once.

Persisted under ``<persist_dir>/.collab-outbox/``.

Public kernel path only. No TeleAgent HTTP. No Hermes ledger. No glue rewrite.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from framework.lifecycle import GOAL_STATES, TASK_STATES, LifecycleError, assert_transition
from framework.models import CONTRACT_VERSION

OUTBOX_DIRNAME = ".collab-outbox"
STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_SENT = "sent"

EVENT_TASK_STATE = "task.state_changed"
EVENT_TASK_BLOCKED = "task.blocked"
EVENT_TASK_WAITING = "task.waiting"
EVENT_GOAL_STATE = "goal.state_changed"
EVENT_GOAL_COMPLETED = "goal.completed"

KIND_TASK = "task"
KIND_GOAL = "goal"


class OutboxError(RuntimeError):
    """Outbox / transactional state protocol failure."""


class CrashSimulated(RuntimeError):
    """Test hook: process died after a durable journal write (or after deliver)."""


def _utc_now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    t = ts if ts is not None else _utc_now()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def persist_dir(root: str | Path) -> Path:
    return Path(root) / OUTBOX_DIRNAME


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


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:16]}"


def new_outbox_id() -> str:
    return f"obx_{uuid.uuid4().hex[:16]}"


def new_txn_id() -> str:
    return f"txn_{uuid.uuid4().hex[:16]}"


def entity_key(kind: str, entity_id: str) -> str:
    return f"{(kind or '').strip()}:{(entity_id or '').strip()}"


def infer_kind(goal_or_task: Mapping[str, Any] | None, *, kind: str = "") -> str:
    if kind:
        k = kind.strip().lower()
        if k in (KIND_TASK, KIND_GOAL):
            return k
    rec = goal_or_task if isinstance(goal_or_task, Mapping) else {}
    hinted = str(rec.get("kind") or rec.get("entity_kind") or "").strip().lower()
    if hinted in (KIND_TASK, KIND_GOAL):
        return hinted
    if rec.get("task_id") and not rec.get("desired_outcome"):
        return KIND_TASK
    if rec.get("goal_id") and not rec.get("task_id"):
        return KIND_GOAL
    if rec.get("task_id"):
        return KIND_TASK
    if rec.get("goal_id"):
        return KIND_GOAL
    raise OutboxError("cannot infer entity kind (need task or goal)")


def infer_entity_id(
    goal_or_task: Mapping[str, Any] | None,
    *,
    kind: str,
    entity_id: str = "",
) -> str:
    if entity_id:
        return str(entity_id).strip()
    rec = goal_or_task if isinstance(goal_or_task, Mapping) else {}
    if kind == KIND_TASK:
        return str(rec.get("task_id") or rec.get("entity_id") or rec.get("id") or "").strip()
    return str(rec.get("goal_id") or rec.get("entity_id") or rec.get("id") or "").strip()


def event_type_for(*, kind: str, new_state: str, reason: str = "") -> str:
    if kind == KIND_GOAL and new_state == "completed":
        return EVENT_GOAL_COMPLETED
    if kind == KIND_TASK and new_state == "blocked":
        return EVENT_TASK_BLOCKED
    if kind == KIND_TASK and new_state == "queued" and reason:
        return EVENT_TASK_WAITING
    if kind == KIND_GOAL:
        return EVENT_GOAL_STATE
    return EVENT_TASK_STATE


def make_event(
    *,
    type: str,
    goal_id: str,
    task_id: str | None = None,
    run_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    dedupe_key: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    eid = (event_id or "").strip() or new_event_id()
    key = (dedupe_key or "").strip() or eid
    ev: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "event_id": eid,
        "ts": ts or _iso(),
        "type": str(type),
        "goal_id": str(goal_id or ""),
        "task_id": str(task_id) if task_id else None,
        "run_id": str(run_id) if run_id else None,
        "payload": dict(payload) if isinstance(payload, Mapping) else {},
        "dedupe_key": key,
    }
    return ev


def delivery_key_of(event: Mapping[str, Any] | None, *, delivery_key: str = "") -> str:
    if delivery_key:
        return str(delivery_key).strip()
    ev = event if isinstance(event, Mapping) else {}
    return str(ev.get("dedupe_key") or ev.get("event_id") or "").strip()


@dataclass
class OutboxRow:
    """One durable outbound notification. pending → claimed → sent."""

    outbox_id: str
    event_id: str
    delivery_key: str
    status: str = STATUS_PENDING
    event: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    claimed_at: float = 0.0
    sent_at: float = 0.0
    attempts: int = 0
    txn_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "event_id": self.event_id,
            "delivery_key": self.delivery_key,
            "status": self.status,
            "event": dict(self.event),
            "created_at": float(self.created_at),
            "created_at_iso": _iso(self.created_at) if self.created_at else "",
            "claimed_at": float(self.claimed_at),
            "sent_at": float(self.sent_at),
            "attempts": int(self.attempts),
            "txn_id": self.txn_id,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "OutboxRow | None":
        if not isinstance(d, Mapping) or not d.get("outbox_id"):
            return None
        ev = d.get("event") if isinstance(d.get("event"), Mapping) else {}
        return cls(
            outbox_id=str(d.get("outbox_id") or ""),
            event_id=str(d.get("event_id") or ev.get("event_id") or ""),
            delivery_key=str(d.get("delivery_key") or ev.get("dedupe_key") or ev.get("event_id") or ""),
            status=str(d.get("status") or STATUS_PENDING),
            event=dict(ev),
            created_at=float(d.get("created_at") or 0.0),
            claimed_at=float(d.get("claimed_at") or 0.0),
            sent_at=float(d.get("sent_at") or 0.0),
            attempts=int(d.get("attempts") or 0),
            txn_id=str(d.get("txn_id") or ""),
        )


def _row_from_event(
    event: Mapping[str, Any],
    *,
    txn_id: str = "",
    now: float | None = None,
) -> OutboxRow:
    ev = make_event(
        type=str(event.get("type") or "unknown"),
        goal_id=str(event.get("goal_id") or ""),
        task_id=event.get("task_id"),
        run_id=event.get("run_id"),
        payload=event.get("payload") if isinstance(event.get("payload"), Mapping) else {},
        event_id=str(event.get("event_id") or "") or None,
        dedupe_key=str(event.get("dedupe_key") or "") or None,
        ts=str(event.get("ts") or "") or None,
    )
    ts = now if now is not None else _utc_now()
    return OutboxRow(
        outbox_id=str(event.get("outbox_id") or "") or new_outbox_id(),
        event_id=str(ev["event_id"]),
        delivery_key=delivery_key_of(ev),
        status=STATUS_PENDING,
        event=ev,
        created_at=ts,
        txn_id=txn_id,
    )


_CACHE: dict[str, "OutboxStore"] = {}
_CACHE_MU = threading.Lock()
_DEDUP_CACHE: dict[str, "DedupStore"] = {}


def reset_outbox_cache() -> None:
    with _CACHE_MU:
        _CACHE.clear()
        _DEDUP_CACHE.clear()


class OutboxStore:
    """File-journaled entity state + outbox. One journal is one transaction."""

    def __init__(self, persist_dir_root: str | Path) -> None:
        self.persist_root = Path(persist_dir_root)
        self.entities: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, OutboxRow] = {}
        self.history: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self._mu = threading.RLock()

    @property
    def path(self) -> Path:
        return persist_dir(self.persist_root)

    def journal_path(self) -> Path:
        return self.path / "journal.json"

    def entities_path(self) -> Path:
        return self.path / "entities.json"

    def outbox_path(self) -> Path:
        return self.path / "outbox.json"

    def get_entity(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        with self._mu:
            rec = self.entities.get(entity_key(kind, entity_id))
            return dict(rec) if rec else None

    def entity_state(self, kind: str, entity_id: str) -> str | None:
        rec = self.get_entity(kind, entity_id)
        if not rec:
            return None
        return str(rec.get("state") or "") or None

    def list_rows(self, *, status: str | None = None) -> list[OutboxRow]:
        with self._mu:
            rows = list(self.rows.values())
        if status:
            rows = [r for r in rows if r.status == status]
        rows.sort(key=lambda r: (r.created_at, r.outbox_id))
        return rows

    def pending(self) -> list[OutboxRow]:
        return [r for r in self.list_rows() if r.status in (STATUS_PENDING, STATUS_CLAIMED)]

    def pending_count(self) -> int:
        return len(self.pending())

    def sent_count(self) -> int:
        return len(self.list_rows(status=STATUS_SENT))

    def snapshot(self) -> dict[str, Any]:
        with self._mu:
            return {
                "persist_dir": str(self.persist_root),
                "path": str(self.path),
                "entities": {k: dict(v) for k, v in self.entities.items()},
                "outbox": [r.to_dict() for r in self.rows.values()],
                "pending": self.pending_count(),
                "sent": self.sent_count(),
                "history": list(self.history)[-50:],
                "notes": list(self.notes)[-20:],
            }

    def append_in_txn(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        entities: Mapping[str, Mapping[str, Any]] | None = None,
        crash: str | None = None,
    ) -> dict[str, Any]:
        """Append outbox rows (and optional entity snapshots) in one journal txn."""
        return self._commit(
            entity_updates=entities,
            events=events,
            crash=crash,
        )

    def claim_pending(
        self,
        *,
        limit: int = 100,
        reclaim: bool = True,
        now: float | None = None,
    ) -> list[OutboxRow]:
        """Claim pending (and reclaim claimed-but-not-sent) rows for delivery."""
        ts = now if now is not None else _utc_now()
        with self._mu:
            claimed: list[OutboxRow] = []
            for row in sorted(self.rows.values(), key=lambda r: (r.created_at, r.outbox_id)):
                if len(claimed) >= int(limit):
                    break
                if row.status == STATUS_PENDING or (reclaim and row.status == STATUS_CLAIMED):
                    row.status = STATUS_CLAIMED
                    row.claimed_at = ts
                    row.attempts = int(row.attempts) + 1
                    claimed.append(row)
            if claimed:
                self._commit_unlocked(crash=None)
            return [OutboxRow.from_dict(r.to_dict()) for r in claimed if OutboxRow.from_dict(r.to_dict())]

    def mark_sent(self, outbox_id: str | Iterable[str], *, now: float | None = None) -> list[str]:
        ts = now if now is not None else _utc_now()
        ids = [outbox_id] if isinstance(outbox_id, str) else list(outbox_id)
        marked: list[str] = []
        with self._mu:
            for oid in ids:
                row = self.rows.get(str(oid))
                if row is None:
                    continue
                row.status = STATUS_SENT
                row.sent_at = ts
                marked.append(row.outbox_id)
            if marked:
                self._commit_unlocked(crash=None)
        return marked

    def _commit(
        self,
        *,
        entity_updates: Mapping[str, Mapping[str, Any]] | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        crash: str | None = None,
        history_item: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._mu:
            return self._commit_unlocked(
                entity_updates=entity_updates,
                events=events,
                crash=crash,
                history_item=history_item,
            )

    def _commit_unlocked(
        self,
        *,
        entity_updates: Mapping[str, Mapping[str, Any]] | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        crash: str | None = None,
        history_item: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        txn_id = new_txn_id()
        now = _utc_now()
        if entity_updates:
            for key, rec in entity_updates.items():
                self.entities[str(key)] = dict(rec)
        new_rows: list[OutboxRow] = []
        for ev in events or []:
            if not isinstance(ev, Mapping):
                continue
            row = _row_from_event(ev, txn_id=txn_id, now=now)
            self.rows[row.outbox_id] = row
            new_rows.append(row)
        if history_item:
            item = dict(history_item)
            item.setdefault("txn_id", txn_id)
            item.setdefault("at", now)
            item.setdefault("at_iso", _iso(now))
            self.history.append(item)
        journal = {
            "txn_id": txn_id,
            "ts": now,
            "ts_iso": _iso(now),
            "entities": {k: dict(v) for k, v in self.entities.items()},
            "outbox": [r.to_dict() for r in self.rows.values()],
            "history": list(self.history)[-200:],
            "notes": list(self.notes)[-50:],
        }
        self.path.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.journal_path(), journal)
        if crash == "after_journal":
            raise CrashSimulated("crash after journal; state+outbox not yet split to live files")
        self._apply_snapshot_unlocked(journal)
        if crash == "after_apply":
            raise CrashSimulated("crash after apply; journal still present for replay")
        self._drop_journal_unlocked()
        return {
            "ok": True,
            "txn_id": txn_id,
            "appended": [r.to_dict() for r in new_rows],
            "entities": {k: dict(v) for k, v in (entity_updates or {}).items()},
        }

    def _apply_snapshot_unlocked(self, journal: Mapping[str, Any]) -> None:
        ents = journal.get("entities") if isinstance(journal.get("entities"), Mapping) else {}
        self.entities = {str(k): dict(v) for k, v in ents.items() if isinstance(v, Mapping)}
        self.rows = {}
        for raw in journal.get("outbox") or []:
            rec = OutboxRow.from_dict(raw if isinstance(raw, Mapping) else {})
            if rec is not None:
                self.rows[rec.outbox_id] = rec
        self.history = [dict(x) for x in (journal.get("history") or []) if isinstance(x, Mapping)]
        self.notes = [str(x) for x in (journal.get("notes") or [])]
        _atomic_write(
            self.entities_path(),
            {"entities": {k: dict(v) for k, v in self.entities.items()}, "saved_at": _utc_now()},
        )
        _atomic_write(
            self.outbox_path(),
            {"rows": [r.to_dict() for r in self.rows.values()], "saved_at": _utc_now()},
        )

    def _drop_journal_unlocked(self) -> None:
        path = self.journal_path()
        try:
            if path.is_file():
                path.unlink()
        except OSError as e:
            self.notes.append(f"journal unlink failed: {e}")

    def _replay_journal_unlocked(self) -> bool:
        raw = _read_json(self.journal_path())
        if not isinstance(raw, dict) or not raw.get("txn_id"):
            return False
        self._apply_snapshot_unlocked(raw)
        self.notes.append(f"replayed journal txn={raw.get('txn_id')}")
        self._drop_journal_unlocked()
        return True

    def _load_live_unlocked(self) -> None:
        ents = _read_json(self.entities_path())
        if isinstance(ents, dict):
            raw_ents = ents.get("entities") if isinstance(ents.get("entities"), Mapping) else ents
            if isinstance(raw_ents, Mapping):
                self.entities = {
                    str(k): dict(v) for k, v in raw_ents.items() if isinstance(v, Mapping)
                }
        box = _read_json(self.outbox_path())
        if isinstance(box, dict):
            self.rows = {}
            for raw in box.get("rows") or []:
                rec = OutboxRow.from_dict(raw if isinstance(raw, Mapping) else {})
                if rec is not None:
                    self.rows[rec.outbox_id] = rec

    @classmethod
    def open(
        cls,
        persist_dir_root: str | Path,
        *,
        use_cache: bool = True,
    ) -> "OutboxStore":
        root = Path(persist_dir_root)
        key = str(root.resolve()) if root.exists() else str(root)
        if use_cache:
            with _CACHE_MU:
                hit = _CACHE.get(key)
                if hit is not None:
                    return hit
        store = cls(root)
        store.path.mkdir(parents=True, exist_ok=True)
        with store._mu:
            replayed = store._replay_journal_unlocked()
            if not replayed:
                store._load_live_unlocked()
                store.notes.append("opened outbox store")
        if use_cache:
            with _CACHE_MU:
                _CACHE[key] = store
        return store


def open_outbox(persist_dir_root: str | Path, *, use_cache: bool = True) -> OutboxStore:
    return OutboxStore.open(persist_dir_root, use_cache=use_cache)


def ids_for_outbox(charter: Mapping[str, Any] | None, name: str = "") -> tuple[str, str]:
    """Stable-enough ids for wiring when the caller has not mapped a Task yet."""
    from framework.id_projection import stable_goal_task_ids

    ch = charter if isinstance(charter, Mapping) else {}
    gid = str(ch.get("goal_id") or "").strip()
    tid = str(ch.get("task_id") or "").strip()
    if gid and tid:
        return gid, tid
    sg, st = stable_goal_task_ids(name or str(ch.get("name") or "job"), ch)
    return gid or sg, tid or st


class DedupStore:
    """Consumer-side event log. Duplicate event_id / delivery_key is ignored."""

    def __init__(self, persist_dir_root: str | Path) -> None:
        self.persist_root = Path(persist_dir_root)
        self.keys: set[str] = set()
        self.event_ids: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self._mu = threading.RLock()

    @property
    def path(self) -> Path:
        return persist_dir(self.persist_root) / "inbox.json"

    def seen(self, key: str) -> bool:
        k = str(key or "").strip()
        with self._mu:
            return k in self.keys or k in self.event_ids

    def receive(
        self,
        event: Mapping[str, Any] | None,
        *,
        delivery_key: str = "",
    ) -> dict[str, Any]:
        ev = dict(event) if isinstance(event, Mapping) else {}
        eid = str(ev.get("event_id") or "").strip()
        key = delivery_key_of(ev, delivery_key=delivery_key)
        if not key and not eid:
            return {
                "accepted": False,
                "duplicate": False,
                "error": "missing event_id/delivery_key",
            }
        with self._mu:
            if (key and key in self.keys) or (eid and eid in self.event_ids):
                return {
                    "accepted": False,
                    "duplicate": True,
                    "delivery_key": key or eid,
                    "event_id": eid,
                }
            if key:
                self.keys.add(key)
            if eid:
                self.event_ids.add(eid)
                self.keys.add(eid)
            self.events.append(ev)
            self._persist_unlocked()
            return {
                "accepted": True,
                "duplicate": False,
                "delivery_key": key or eid,
                "event_id": eid,
            }

    def _persist_unlocked(self) -> None:
        _atomic_write(
            self.path,
            {
                "keys": sorted(self.keys),
                "event_ids": sorted(self.event_ids),
                "events": list(self.events),
                "saved_at": _utc_now(),
            },
        )

    def _load_unlocked(self) -> None:
        raw = _read_json(self.path)
        if not isinstance(raw, dict):
            return
        self.keys = {str(x) for x in (raw.get("keys") or [])}
        self.event_ids = {str(x) for x in (raw.get("event_ids") or [])}
        self.events = [dict(x) for x in (raw.get("events") or []) if isinstance(x, Mapping)]

    @classmethod
    def open(
        cls,
        persist_dir_root: str | Path,
        *,
        use_cache: bool = True,
    ) -> "DedupStore":
        root = Path(persist_dir_root)
        key = str(root.resolve()) if root.exists() else str(root)
        if use_cache:
            with _CACHE_MU:
                hit = _DEDUP_CACHE.get(key)
                if hit is not None:
                    return hit
        store = cls(root)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with store._mu:
            store._load_unlocked()
        if use_cache:
            with _CACHE_MU:
                _DEDUP_CACHE[key] = store
        return store


EventLog = DedupStore


def _normalize_event(
    raw: Mapping[str, Any] | None,
    *,
    kind: str,
    entity_id: str,
    new_state: str,
    from_state: str | None,
    goal_id: str,
    task_id: str,
    run_id: str,
    reason: str,
) -> dict[str, Any]:
    ev = dict(raw) if isinstance(raw, Mapping) else {}
    payload = dict(ev.get("payload")) if isinstance(ev.get("payload"), Mapping) else {}
    payload.setdefault("kind", kind)
    payload.setdefault("entity_id", entity_id)
    payload.setdefault("from_state", from_state)
    payload.setdefault("to_state", new_state)
    if reason:
        payload.setdefault("reason", reason)
    gid = str(ev.get("goal_id") or goal_id or "")
    tid = ev.get("task_id") if ev.get("task_id") is not None else (task_id or None)
    if kind == KIND_TASK and not tid:
        tid = entity_id
    return make_event(
        type=str(ev.get("type") or event_type_for(kind=kind, new_state=new_state, reason=reason)),
        goal_id=gid,
        task_id=str(tid) if tid else None,
        run_id=ev.get("run_id") if ev.get("run_id") is not None else (run_id or None),
        payload=payload,
        event_id=str(ev.get("event_id") or "") or None,
        dedupe_key=str(ev.get("dedupe_key") or "") or None,
        ts=str(ev.get("ts") or "") or None,
    )


def _birth_or_transition(kind: str, src: str | None, dst: str) -> None:
    states = TASK_STATES if kind == KIND_TASK else GOAL_STATES
    if dst not in states:
        raise LifecycleError(f"unknown {kind} state {dst!r}")
    if not src:
        return
    if src == dst:
        return
    assert_transition(kind, src, dst)


def commit_transition(
    store: OutboxStore | str | Path,
    goal_or_task: Mapping[str, Any] | None = None,
    new_state: str | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    *,
    kind: str = "",
    entity_id: str = "",
    from_state: str | None = None,
    goal_id: str = "",
    task_id: str = "",
    run_id: str = "",
    reason: str = "",
    extra: Mapping[str, Any] | None = None,
    crash: str | None = None,
) -> dict[str, Any]:
    """Persist new entity state + outbox rows in one journal transaction.

    ``events`` may be empty: a state-changed event is synthesized.
    Same-state writes (e.g. stay queued on unsatisfied deps) still append
    a notification row. Illegal transitions raise and write nothing.
    """
    if not isinstance(store, OutboxStore):
        store = OutboxStore.open(store)
    rec_in = goal_or_task if isinstance(goal_or_task, Mapping) else {}
    k = infer_kind(rec_in, kind=kind)
    eid = infer_entity_id(rec_in, kind=k, entity_id=entity_id)
    if not eid:
        raise OutboxError("entity_id required")
    dst = str(new_state if new_state is not None else rec_in.get("status") or rec_in.get("state") or "").strip()
    if not dst:
        raise OutboxError("new_state required")
    gid = str(goal_id or rec_in.get("goal_id") or (eid if k == KIND_GOAL else "") or "")
    tid = str(task_id or rec_in.get("task_id") or (eid if k == KIND_TASK else "") or "")
    key = entity_key(k, eid)
    current = store.get_entity(k, eid)
    prev = from_state
    if prev is None and current is not None:
        prev = str(current.get("state") or "") or None
    if prev is None and rec_in.get("status"):
        st = str(rec_in.get("status") or "")
        if st and st != dst:
            prev = st
    _birth_or_transition(k, prev, dst)
    now = _utc_now()
    entity = {
        "kind": k,
        "entity_id": eid,
        "state": dst,
        "prev_state": prev,
        "goal_id": gid,
        "task_id": tid if k == KIND_TASK else str(rec_in.get("task_id") or ""),
        "run_id": str(run_id or rec_in.get("run_id") or ""),
        "reason": str(reason or ""),
        "updated_at": now,
        "updated_at_iso": _iso(now),
    }
    if extra:
        entity["extra"] = dict(extra)
    for fld in ("title", "name", "depends_on"):
        if rec_in.get(fld) is not None:
            entity[fld] = rec_in.get(fld)
    ev_list: list[dict[str, Any]]
    if events:
        ev_list = [
            _normalize_event(
                ev,
                kind=k,
                entity_id=eid,
                new_state=dst,
                from_state=prev,
                goal_id=gid,
                task_id=tid,
                run_id=str(run_id or ""),
                reason=str(reason or ""),
            )
            for ev in events
            if isinstance(ev, Mapping)
        ]
        if not ev_list:
            raise OutboxError("events must contain objects")
    else:
        ev_list = [
            _normalize_event(
                None,
                kind=k,
                entity_id=eid,
                new_state=dst,
                from_state=prev,
                goal_id=gid,
                task_id=tid,
                run_id=str(run_id or ""),
                reason=str(reason or ""),
            )
        ]
    out = store.append_in_txn(
        ev_list,
        entities={key: entity},
        crash=crash,
    )
    out["kind"] = k
    out["entity_id"] = eid
    out["state"] = dst
    out["from_state"] = prev
    out["events"] = ev_list
    out["entity"] = entity
    return out


def commit_transitions(
    store: OutboxStore | str | Path,
    items: Sequence[Mapping[str, Any]],
    *,
    crash: str | None = None,
) -> dict[str, Any]:
    """Atomic multi-entity state + outbox commit (one journal)."""
    if not isinstance(store, OutboxStore):
        store = OutboxStore.open(store)
    prepared: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    entity_updates: dict[str, dict[str, Any]] = {}
    all_events: list[dict[str, Any]] = []
    now = _utc_now()
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        rec_in = raw.get("entity") if isinstance(raw.get("entity"), Mapping) else raw
        k = infer_kind(rec_in, kind=str(raw.get("kind") or ""))
        eid = infer_entity_id(rec_in, kind=k, entity_id=str(raw.get("entity_id") or ""))
        if not eid:
            raise OutboxError("entity_id required")
        dst = str(raw.get("new_state") or rec_in.get("status") or rec_in.get("state") or "").strip()
        if not dst:
            raise OutboxError("new_state required")
        gid = str(raw.get("goal_id") or rec_in.get("goal_id") or (eid if k == KIND_GOAL else "") or "")
        tid = str(raw.get("task_id") or rec_in.get("task_id") or (eid if k == KIND_TASK else "") or "")
        key = entity_key(k, eid)
        current = store.get_entity(k, eid)
        prev = raw.get("from_state")
        if prev is None and current is not None:
            prev = str(current.get("state") or "") or None
        _birth_or_transition(k, str(prev) if prev else None, dst)
        entity = {
            "kind": k,
            "entity_id": eid,
            "state": dst,
            "prev_state": prev,
            "goal_id": gid,
            "task_id": tid if k == KIND_TASK else "",
            "run_id": str(raw.get("run_id") or ""),
            "reason": str(raw.get("reason") or ""),
            "updated_at": now,
            "updated_at_iso": _iso(now),
        }
        extra = raw.get("extra") if isinstance(raw.get("extra"), Mapping) else None
        if extra:
            entity["extra"] = dict(extra)
        evs_raw = raw.get("events")
        if isinstance(evs_raw, Sequence) and not isinstance(evs_raw, (str, bytes)) and evs_raw:
            ev_list = [
                _normalize_event(
                    ev,
                    kind=k,
                    entity_id=eid,
                    new_state=dst,
                    from_state=str(prev) if prev else None,
                    goal_id=gid,
                    task_id=tid,
                    run_id=str(raw.get("run_id") or ""),
                    reason=str(raw.get("reason") or ""),
                )
                for ev in evs_raw
                if isinstance(ev, Mapping)
            ]
        else:
            ev_list = [
                _normalize_event(
                    None,
                    kind=k,
                    entity_id=eid,
                    new_state=dst,
                    from_state=str(prev) if prev else None,
                    goal_id=gid,
                    task_id=tid,
                    run_id=str(raw.get("run_id") or ""),
                    reason=str(raw.get("reason") or ""),
                )
            ]
        entity_updates[key] = entity
        all_events.extend(ev_list)
        prepared.append((key, entity, ev_list))
    if not prepared:
        raise OutboxError("no transitions to commit")
    out = store.append_in_txn(all_events, entities=entity_updates, crash=crash)
    out["items"] = [
        {"key": key, "entity": ent, "events": evs} for key, ent, evs in prepared
    ]
    return out


def record_transition(
    persist_dir_root: str | Path | None,
    goal_or_task: Mapping[str, Any] | None = None,
    new_state: str | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Best-effort commit_transition for inprocess wiring. Never raises."""
    if persist_dir_root is None:
        return None
    try:
        store = OutboxStore.open(persist_dir_root)
        return commit_transition(store, goal_or_task, new_state, events, **kwargs)
    except Exception:  # noqa: BLE001 — projection/outbox must not fail the job
        return None


def attach_outbox(result: dict[str, Any], store: OutboxStore | None) -> dict[str, Any]:
    """Readonly snapshot. Does not flip ok/fail."""
    if store is None or not isinstance(result, dict):
        return result
    prev_ok = result.get("ok")
    prev_state = result.get("state")
    prev_error = result.get("error")
    snap = store.snapshot()
    result["outbox"] = {
        "pending": snap["pending"],
        "sent": snap["sent"],
        "path": snap["path"],
        "entities": snap["entities"],
        "readonly": True,
    }
    if "ok" in result:
        result["ok"] = prev_ok
    if "state" in result:
        result["state"] = prev_state
    if "error" in result:
        result["error"] = prev_error
    return result


def pump_outbox(
    store: OutboxStore,
    receiver: DedupStore,
    *,
    sink: Callable[[Mapping[str, Any]], Any] | None = None,
    mark_sent: bool = True,
    crash_before_mark_sent: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Deliver claimed outbox rows at-least-once. Receiver dedups.

    ``sink`` is the external side effect (may run more than once on replay).
    ``receiver.receive`` is the idempotent consume step.
    """
    claimed = store.claim_pending(limit=limit)
    delivered: list[str] = []
    duplicates: list[str] = []
    sink_calls = 0
    for row in claimed:
        ev = dict(row.event)
        if sink is not None:
            sink(ev)
            sink_calls += 1
        rec = receiver.receive(ev, delivery_key=row.delivery_key)
        if rec.get("duplicate"):
            duplicates.append(row.outbox_id)
        else:
            delivered.append(row.outbox_id)
        if crash_before_mark_sent:
            raise CrashSimulated("crash after deliver, before mark_sent")
        if mark_sent:
            store.mark_sent(row.outbox_id)
    return {
        "claimed": len(claimed),
        "delivered": delivered,
        "duplicates": duplicates,
        "sink_calls": sink_calls,
        "accepted": len(delivered),
        "duplicate_count": len(duplicates),
    }


__all__ = [
    "CONTRACT_VERSION",
    "CrashSimulated",
    "DedupStore",
    "EVENT_GOAL_COMPLETED",
    "EVENT_GOAL_STATE",
    "EVENT_TASK_BLOCKED",
    "EVENT_TASK_STATE",
    "EVENT_TASK_WAITING",
    "EventLog",
    "KIND_GOAL",
    "KIND_TASK",
    "OUTBOX_DIRNAME",
    "OutboxError",
    "OutboxRow",
    "OutboxStore",
    "STATUS_CLAIMED",
    "STATUS_PENDING",
    "STATUS_SENT",
    "attach_outbox",
    "commit_transition",
    "commit_transitions",
    "delivery_key_of",
    "entity_key",
    "ids_for_outbox",
    "make_event",
    "open_outbox",
    "persist_dir",
    "pump_outbox",
    "record_transition",
    "reset_outbox_cache",
]
