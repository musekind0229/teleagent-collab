#!/usr/bin/env python3
"""Persistent job/run state for fault recovery (条6).

Persists:
  - job status (queued/running/pending_approval/cancel_requested/cancelled/done/fail/timeout)
  - pending items still awaiting decision
  - decision records (so restart does not re-send the same decision)
  - claimed session ids (so restart does not hijack an old TeleAgent session)

Cancel semantics:
  - cancel_requested = request received (orchestration acknowledged)
  - cancelled = execution stopped for this job_id only
Timeout/cancel never cascade to sibling jobs.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_STATE_ROOT = Path(__file__).resolve().parents[1] / "jobs" / "state"

TERMINAL = frozenset({"done", "fail", "timeout", "cancelled"})
ACTIVE = frozenset({"queued", "starting", "running", "pending_approval", "cancel_requested"})
# Persist/restore job contract; bump when JobRecord charter schema changes incompatibly.
CONTRACT_VERSION = 1


@dataclass
class DecisionRecord:
    decision_id: str
    job_id: str
    permission_id: str
    reply: str
    via: str
    lead_decision: str | None = None
    sent_at: float = field(default_factory=time.time)
    application_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionRecord":
        return cls(
            decision_id=str(d.get("decision_id") or ""),
            job_id=str(d.get("job_id") or ""),
            permission_id=str(d.get("permission_id") or ""),
            reply=str(d.get("reply") or ""),
            via=str(d.get("via") or ""),
            lead_decision=d.get("lead_decision"),
            sent_at=float(d.get("sent_at") or time.time()),
            application_id=d.get("application_id"),
        )


@dataclass
class PendingItem:
    """Open permission wait. permission_id == public request_id (stable key)."""
    permission_id: str
    job_id: str
    session_id: str = ""
    request_id: str = ""  # public id; defaults to permission_id for old rows
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "open"  # open | decided | abandoned

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = self.permission_id
        if not self.permission_id and self.request_id:
            self.permission_id = self.request_id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingItem":
        pid = str(d.get("permission_id") or d.get("request_id") or "")
        rid = str(d.get("request_id") or pid)
        return cls(
            permission_id=pid,
            job_id=str(d.get("job_id") or ""),
            session_id=str(d.get("session_id") or ""),
            request_id=rid,
            summary=str(d.get("summary") or ""),
            created_at=float(d.get("created_at") or time.time()),
            status=str(d.get("status") or "open"),
        )



@dataclass
class JobRecord:
    job_id: str
    name: str = ""
    state: str = "queued"
    session_id: str = ""
    workdir: str = ""
    charter_name: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested_at: float | None = None
    cancel_effected_at: float | None = None
    timeout_sec: int = 300
    wall_deadline: float | None = None
    claimed_session: bool = False
    dispatch_token: str = ""  # unique per start; restart must not reuse to re-prompt
    dispatch_user_message_id: str = ""  # user message id for this dispatch turn
    dispatch_query_id: str = ""  # queryID sent with this dispatch prompt
    notes: list[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    handled_perm_ids: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    # Full task contract (条6 / astra P1 restore)
    contract_version: int = CONTRACT_VERSION
    charter: dict = field(default_factory=dict)
    instruction: str = ""
    expected_artifacts: list[str] = field(default_factory=list)
    force_lead_review: bool = False
    rework_budget: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        return cls(
            job_id=str(d["job_id"]),
            name=str(d.get("name") or ""),
            state=str(d.get("state") or "queued"),
            session_id=str(d.get("session_id") or ""),
            workdir=str(d.get("workdir") or ""),
            charter_name=str(d.get("charter_name") or ""),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            cancel_requested_at=d.get("cancel_requested_at"),
            cancel_effected_at=d.get("cancel_effected_at"),
            timeout_sec=int(d.get("timeout_sec") or 300),
            wall_deadline=d.get("wall_deadline"),
            claimed_session=bool(d.get("claimed_session")),
            dispatch_token=str(d.get("dispatch_token") or ""),
            dispatch_user_message_id=str(d.get("dispatch_user_message_id") or ""),
            dispatch_query_id=str(d.get("dispatch_query_id") or ""),
            notes=list(d.get("notes") or []),
            result=dict(d.get("result") or {}),
            handled_perm_ids=list(d.get("handled_perm_ids") or []),
            updated_at=float(d.get("updated_at") or time.time()),
            contract_version=int(d.get("contract_version") or 0),
            charter=dict(d.get("charter") or {}),
            instruction=str(d.get("instruction") or ""),
            expected_artifacts=list(d.get("expected_artifacts") or []),
            force_lead_review=bool(d.get("force_lead_review")),
            rework_budget=dict(d.get("rework_budget") or {}),
        )

    def contract_ok(self, *, expect_version: int = CONTRACT_VERSION) -> tuple[bool, str]:
        """Whether persisted charter is complete enough to resume safely."""
        if int(self.contract_version or 0) != int(expect_version):
            return False, f"contract_version_mismatch got={self.contract_version} want={expect_version}"
        if not isinstance(self.charter, dict) or not self.charter:
            return False, "charter_missing"
        # Require core constraint keys so empty-constraint resume is impossible
        for key in ("goal", "must", "must_not"):
            if key not in self.charter:
                return False, f"charter_missing_key:{key}"
        if not isinstance(self.charter.get("must"), list) or not isinstance(self.charter.get("must_not"), list):
            return False, "charter_constraints_invalid"
        return True, "ok"


class StateStore:
    """JSON file-backed store under jobs/state/<run_id>/."""

    def __init__(self, root: str | Path | None = None, *, run_id: str = "default") -> None:
        self.root = Path(root or DEFAULT_STATE_ROOT) / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._pending: dict[str, PendingItem] = {}  # key = permission_id
        self._decisions: dict[str, DecisionRecord] = {}  # key = decision_id or perm id
        self._load()

    # --- paths -----------------------------------------------------------------

    def _jobs_path(self) -> Path:
        return self.root / "jobs.json"

    def _pending_path(self) -> Path:
        return self.root / "pending.json"

    def _decisions_path(self) -> Path:
        return self.root / "decisions.json"

    def _atomic_write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)

    def _load(self) -> None:
        with self._lock:
            if self._jobs_path().is_file():
                raw = json.loads(self._jobs_path().read_text(encoding="utf-8"))
                for jid, obj in (raw.get("jobs") or {}).items():
                    self._jobs[jid] = JobRecord.from_dict(obj)
            if self._pending_path().is_file():
                raw = json.loads(self._pending_path().read_text(encoding="utf-8"))
                for pid, obj in (raw.get("pending") or {}).items():
                    self._pending[pid] = PendingItem.from_dict(obj)
            if self._decisions_path().is_file():
                raw = json.loads(self._decisions_path().read_text(encoding="utf-8"))
                for did, obj in (raw.get("decisions") or {}).items():
                    self._decisions[did] = DecisionRecord.from_dict(obj)

    def flush(self) -> None:
        with self._lock:
            self._atomic_write(
                self._jobs_path(),
                {"jobs": {k: v.to_dict() for k, v in self._jobs.items()}, "saved_at": time.time()},
            )
            self._atomic_write(
                self._pending_path(),
                {
                    "pending": {k: v.to_dict() for k, v in self._pending.items()},
                    "saved_at": time.time(),
                },
            )
            self._atomic_write(
                self._decisions_path(),
                {
                    "decisions": {k: v.to_dict() for k, v in self._decisions.items()},
                    "saved_at": time.time(),
                },
            )

    # --- jobs ------------------------------------------------------------------

    def upsert_job(self, rec: JobRecord) -> JobRecord:
        with self._lock:
            rec.updated_at = time.time()
            self._jobs[rec.job_id] = rec
            self.flush()
            return rec

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return list(self._jobs.values())

    def set_job_state(self, job_id: str, state: str, **fields: Any) -> JobRecord:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                rec = JobRecord(job_id=job_id, state=state)
            else:
                rec.state = state
            for k, v in fields.items():
                if hasattr(rec, k):
                    setattr(rec, k, v)
            rec.updated_at = time.time()
            self._jobs[job_id] = rec
            self.flush()
            return rec

    def request_cancel(self, job_id: str) -> JobRecord:
        """Mark cancel requested — does not yet mean execution stopped."""
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                raise KeyError(f"unknown job_id {job_id}")
            if rec.state in TERMINAL:
                rec.notes.append(f"cancel ignored; already terminal={rec.state}")
                self.flush()
                return rec
            rec.state = "cancel_requested"
            rec.cancel_requested_at = time.time()
            rec.notes.append("cancel_requested received (execution may still be running)")
            rec.updated_at = time.time()
            self.flush()
            return rec

    def effect_cancel(self, job_id: str, *, error: str = "cancelled by request") -> JobRecord:
        """Mark execution stopped for this job only."""
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                raise KeyError(f"unknown job_id {job_id}")
            rec.state = "cancelled"
            rec.cancel_effected_at = time.time()
            rec.finished_at = time.time()
            rec.result = {
                **(rec.result or {}),
                "ok": False,
                "state": "cancelled",
                "error": error,
                "cancel_requested_at": rec.cancel_requested_at,
                "cancel_effected_at": rec.cancel_effected_at,
            }
            rec.notes.append("cancel effected — execution stopped for this job_id only")
            # Abandon open pending for this job only
            for p in self._pending.values():
                if p.job_id == job_id and p.status == "open":
                    p.status = "abandoned"
            rec.updated_at = time.time()
            self.flush()
            return rec

    def mark_timeout(self, job_id: str, *, error: str = "wall clock timeout") -> JobRecord:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                raise KeyError(f"unknown job_id {job_id}")
            rec.state = "timeout"
            rec.finished_at = time.time()
            rec.result = {
                **(rec.result or {}),
                "ok": False,
                "state": "timeout",
                "error": error,
            }
            rec.notes.append("timeout scoped to this job_id only")
            for p in self._pending.values():
                if p.job_id == job_id and p.status == "open":
                    p.status = "abandoned"
            rec.updated_at = time.time()
            self.flush()
            return rec

    # --- pending / decisions ---------------------------------------------------

    def add_pending(self, item: PendingItem) -> None:
        with self._lock:
            self._pending[item.permission_id] = item
            self.flush()

    def get_pending(self, permission_id: str) -> PendingItem | None:
        with self._lock:
            return self._pending.get(permission_id)

    def open_pending_for_job(self, job_id: str) -> list[PendingItem]:
        with self._lock:
            return [p for p in self._pending.values() if p.job_id == job_id and p.status == "open"]

    def already_decided(self, permission_id: str) -> DecisionRecord | None:
        with self._lock:
            for d in self._decisions.values():
                if d.permission_id == permission_id:
                    return d
            return None

    def record_decision(self, rec: DecisionRecord) -> DecisionRecord:
        with self._lock:
            key = rec.decision_id or f"dec-{rec.permission_id}"
            rec.decision_id = key
            self._decisions[key] = rec
            pend = self._pending.get(rec.permission_id)
            if pend:
                pend.status = "decided"
            job = self._jobs.get(rec.job_id)
            if job and rec.permission_id not in job.handled_perm_ids:
                job.handled_perm_ids.append(rec.permission_id)
            self.flush()
            return rec

    def claim_session(self, job_id: str, session_id: str, *, dispatch_token: str) -> bool:
        """Claim a session for a job. Returns False if session already claimed by another active job."""
        with self._lock:
            for other in self._jobs.values():
                if other.job_id == job_id:
                    continue
                if other.session_id == session_id and other.claimed_session and other.state in ACTIVE:
                    return False
            rec = self._jobs.get(job_id)
            if rec is None:
                rec = JobRecord(job_id=job_id)
            # Restart safety: if already dispatched with a different token, do not re-dispatch
            if rec.dispatch_token and rec.dispatch_token != dispatch_token and rec.session_id:
                return False
            rec.session_id = session_id
            rec.claimed_session = True
            rec.dispatch_token = dispatch_token
            rec.updated_at = time.time()
            self._jobs[job_id] = rec
            self.flush()
            return True

    def should_dispatch(self, job_id: str) -> tuple[bool, str]:
        """Whether restart should create a new TeleAgent session / prompt."""
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return True, "new_job"
            if rec.state in TERMINAL:
                return False, f"already_terminal:{rec.state}"
            if rec.dispatch_token and rec.session_id and rec.claimed_session:
                return False, "already_dispatched_resume_only"
            if rec.state == "cancel_requested":
                return False, "cancel_requested_pending_effect"
            return True, "ok_to_dispatch"

    def resume_plan(self) -> dict:
        """Guidance for scheduler restart: which jobs to resume vs leave alone."""
        with self._lock:
            resume_monitor: list[str] = []
            skip_dispatch: list[str] = []
            effect_cancel: list[str] = []
            terminal: list[str] = []
            for j in self._jobs.values():
                if j.state in TERMINAL:
                    terminal.append(j.job_id)
                elif j.state == "cancel_requested":
                    effect_cancel.append(j.job_id)
                    skip_dispatch.append(j.job_id)
                elif j.session_id and j.claimed_session:
                    resume_monitor.append(j.job_id)
                    skip_dispatch.append(j.job_id)
                else:
                    resume_monitor.append(j.job_id)
            return {
                "resume_monitor": resume_monitor,
                "skip_dispatch": skip_dispatch,
                "effect_cancel": effect_cancel,
                "terminal": terminal,
                "decisions_count": len(self._decisions),
                "open_pending": [p.permission_id for p in self._pending.values() if p.status == "open"],
            }


def new_dispatch_token() -> str:
    import uuid

    return uuid.uuid4().hex


__all__ = [
    "DEFAULT_STATE_ROOT",
    "TERMINAL",
    "ACTIVE",
    "CONTRACT_VERSION",
    "DecisionRecord",
    "PendingItem",
    "JobRecord",
    "StateStore",
    "new_dispatch_token",
]
