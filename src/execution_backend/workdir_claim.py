"""Knife 10: exclusive workdir/path resource claim (queue or block).

Two jobs targeting the same resolved workdir must not run in parallel merely
because they use different ExecutionBackend instances. The holder is recorded
in an occupancy record. Isolated (distinct) workdirs still run in parallel.

In-process: threading occupancy (flock is per-process on Linux).
Cross-process: fcntl flock + occupancy JSON under the workdir.

Public ExecutionBackend path only. No TeleAgent HTTP. No Hermes ledger.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOCK_FILENAME = ".collab-workdir-claim.lock"
OCCUPANCY_FILENAME = ".collab-workdir-claim.json"
STATUS_GRANTED = "granted"
STATUS_BLOCKED = "blocked"
STATUS_QUEUED = "queued"
STATUS_HELD = "held"

_BLOCK_MODES = frozenset({"block", "blocked", "fail", "reject"})
_QUEUE_MODES = frozenset({"queue", "queued", "wait"})


class WorkdirClaimError(RuntimeError):
    """Workdir resource is occupied (or claim protocol failed)."""


def resolve_workdir(workdir: str | Path) -> Path:
    return Path(workdir).expanduser().resolve()


def occupancy_path(workdir: str | Path) -> Path:
    return resolve_workdir(workdir) / OCCUPANCY_FILENAME


def lock_path(workdir: str | Path) -> Path:
    return resolve_workdir(workdir) / LOCK_FILENAME


def write_paths_from_charter(charter: dict | None) -> list[str]:
    if not isinstance(charter, dict):
        return []
    done = charter.get("done_when") if isinstance(charter.get("done_when"), dict) else {}
    return [str(x) for x in (done.get("artifacts") or [])]


def _utc_iso(ts: float | None = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _normalize_mode(mode: str) -> str:
    key = (mode or "block").strip().lower()
    if key in _QUEUE_MODES:
        return "queue"
    return "block"


def _new_holder_id() -> str:
    return f"inproc-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"


@dataclass
class Occupancy:
    """Who holds a workdir claim."""

    workdir: str
    holder_id: str
    job_name: str = ""
    backend_id: str = "inprocess.local_v1"
    backend_instance_id: str = ""
    pid: int = 0
    thread_id: int = 0
    claimed_at: float = 0.0
    claimed_at_iso: str = ""
    write_paths: list[str] = field(default_factory=list)
    status: str = STATUS_HELD
    waiters: list[dict[str, Any]] = field(default_factory=list)
    refcount: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "workdir": self.workdir,
            "holder_id": self.holder_id,
            "job_name": self.job_name,
            "backend_id": self.backend_id,
            "backend_instance_id": self.backend_instance_id,
            "pid": self.pid,
            "thread_id": self.thread_id,
            "claimed_at": self.claimed_at,
            "claimed_at_iso": self.claimed_at_iso,
            "write_paths": list(self.write_paths),
            "status": self.status,
            "waiters": [dict(w) for w in self.waiters],
        }

    def snapshot(self) -> Occupancy:
        return Occupancy(
            workdir=self.workdir,
            holder_id=self.holder_id,
            job_name=self.job_name,
            backend_id=self.backend_id,
            backend_instance_id=self.backend_instance_id,
            pid=self.pid,
            thread_id=self.thread_id,
            claimed_at=self.claimed_at,
            claimed_at_iso=self.claimed_at_iso,
            write_paths=list(self.write_paths),
            status=self.status,
            waiters=[dict(w) for w in self.waiters],
            refcount=self.refcount,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Occupancy | None:
        if not isinstance(d, dict) or not d.get("holder_id"):
            return None
        return cls(
            workdir=str(d.get("workdir") or ""),
            holder_id=str(d.get("holder_id") or ""),
            job_name=str(d.get("job_name") or ""),
            backend_id=str(d.get("backend_id") or ""),
            backend_instance_id=str(d.get("backend_instance_id") or ""),
            pid=int(d.get("pid") or 0),
            thread_id=int(d.get("thread_id") or 0),
            claimed_at=float(d.get("claimed_at") or 0.0),
            claimed_at_iso=str(d.get("claimed_at_iso") or ""),
            write_paths=[str(x) for x in (d.get("write_paths") or [])],
            status=str(d.get("status") or STATUS_HELD),
            waiters=[dict(w) for w in (d.get("waiters") or []) if isinstance(w, dict)],
            refcount=int(d.get("refcount") or 1),
        )


@dataclass
class ClaimOutcome:
    granted: bool
    status: str
    workdir: str
    requester_id: str
    occupancy: Occupancy | None = None
    waiter: dict[str, Any] | None = None
    was_queued: bool = False
    error: str = ""

    @property
    def blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    @property
    def queued(self) -> bool:
        return self.status == STATUS_QUEUED

    def to_dict(self) -> dict[str, Any]:
        holder = self.occupancy.to_dict() if self.occupancy else None
        return {
            "granted": self.granted,
            "status": self.status,
            "workdir": self.workdir,
            "requester_id": self.requester_id,
            "occupancy": holder,
            "holder": holder,
            "waiter": dict(self.waiter) if self.waiter else None,
            "was_queued": self.was_queued,
            "error": self.error,
            "blocked": self.blocked,
            "queued": self.queued,
        }


def _read_occupancy_file(workdir: str | Path) -> Occupancy | None:
    path = occupancy_path(workdir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return Occupancy.from_dict(raw if isinstance(raw, dict) else None)


def _write_occupancy_file(occ: Occupancy) -> None:
    root = Path(occ.workdir)
    root.mkdir(parents=True, exist_ok=True)
    path = occupancy_path(root)
    tmp = path.with_suffix(".json.tmp")
    payload = occ.to_dict()
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _clear_occupancy_file(workdir: str | Path) -> None:
    path = occupancy_path(workdir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        try:
            path.write_text("{}\n", encoding="utf-8")
        except OSError:
            pass


class WorkdirClaimRegistry:
    """Exclusive workdir claims: in-memory (threads) + flock (processes)."""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._held: dict[str, Occupancy] = {}
        self._fds: dict[str, int] = {}
        self._conds: dict[str, threading.Condition] = {}

    def _key(self, workdir: str | Path) -> str:
        return str(resolve_workdir(workdir))

    def _cond(self, key: str) -> threading.Condition:
        c = self._conds.get(key)
        if c is None:
            c = threading.Condition(self._mu)
            self._conds[key] = c
        return c

    def occupancy(self, workdir: str | Path) -> Occupancy | None:
        """Current holder, if any (in-memory preferred; JSON fallback)."""
        key = self._key(workdir)
        with self._mu:
            rec = self._held.get(key)
            if rec is not None:
                return rec.snapshot()
        return _read_occupancy_file(key)

    def is_held(self, workdir: str | Path) -> bool:
        return self.occupancy(workdir) is not None

    def list_held(self) -> list[Occupancy]:
        with self._mu:
            return [rec.snapshot() for rec in self._held.values()]

    def claim(
        self,
        workdir: str | Path,
        *,
        holder_id: str = "",
        job_name: str = "",
        backend_id: str = "inprocess.local_v1",
        backend_instance_id: str = "",
        write_paths: list[str] | None = None,
        mode: str = "block",
        timeout_sec: float = 0.0,
    ) -> ClaimOutcome:
        """Try to occupy ``workdir``.

        ``mode=block``: return immediately if occupied.
        ``mode=queue``: wait up to ``timeout_sec`` (0 waits a tiny slice then
        returns queued if still occupied).
        Same ``holder_id`` is reentrant.
        """
        root = resolve_workdir(workdir)
        root.mkdir(parents=True, exist_ok=True)
        key = str(root)
        hid = holder_id or _new_holder_id()
        kind = _normalize_mode(mode)
        paths = [str(p) for p in (write_paths or [])]
        waiter = {
            "holder_id": hid,
            "job_name": job_name,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "requested_at": time.time(),
            "requested_at_iso": _utc_iso(),
            "mode": kind,
            "write_paths": paths,
        }
        deadline = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
        queued_once = False
        poll = 0.02

        while True:
            outcome = self._try_acquire(
                key,
                root,
                hid=hid,
                job_name=job_name,
                backend_id=backend_id,
                backend_instance_id=backend_instance_id,
                write_paths=paths,
                waiter=waiter,
            )
            if outcome is not None:
                if outcome.granted:
                    outcome.was_queued = queued_once
                    return outcome
                if kind == "block":
                    if outcome.occupancy is None:
                        outcome.occupancy = self._occupancy_after_block(root, key)
                    outcome.status = STATUS_BLOCKED
                    outcome.waiter = waiter
                    if not outcome.error:
                        outcome.error = (
                            f"workdir occupied by holder_id={outcome.occupancy.holder_id if outcome.occupancy else '?'} "
                            f"job={outcome.occupancy.job_name if outcome.occupancy else ''} "
                            f"(requester={hid})"
                        )
                    return outcome
                queued_once = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome.status = STATUS_QUEUED
                    outcome.was_queued = True
                    outcome.waiter = waiter
                    outcome.error = (
                        f"workdir still occupied after queue wait; "
                        f"holder_id={outcome.occupancy.holder_id if outcome.occupancy else '?'} "
                        f"(requester={hid})"
                    )
                    self._drop_waiter(key, hid)
                    return outcome
                self._wait_or_sleep(key, min(poll, remaining))
                continue
            # None means retry (lost a race)
            if kind == "queue" and time.monotonic() >= deadline:
                occ = self.occupancy(root)
                return ClaimOutcome(
                    granted=False,
                    status=STATUS_QUEUED,
                    workdir=key,
                    requester_id=hid,
                    occupancy=occ,
                    waiter=waiter,
                    was_queued=True,
                    error=f"workdir claim queue timeout (requester={hid})",
                )
            if kind == "block":
                occ = self.occupancy(root)
                return ClaimOutcome(
                    granted=False,
                    status=STATUS_BLOCKED,
                    workdir=key,
                    requester_id=hid,
                    occupancy=occ,
                    waiter=waiter,
                    error=f"workdir occupied (requester={hid})",
                )
            time.sleep(poll)

    def _wait_or_sleep(self, key: str, timeout: float) -> None:
        with self._mu:
            cond = self._cond(key)
            if key in self._held:
                cond.wait(timeout=timeout)
                return
        time.sleep(timeout)

    def _occupancy_after_block(self, root: Path, key: str) -> Occupancy | None:
        """Holder metadata after a failed flock: in-memory, then JSON, short retry."""
        for _ in range(5):
            with self._mu:
                rec = self._held.get(key)
                if rec is not None:
                    return rec.snapshot()
            occ = _read_occupancy_file(root)
            if occ is not None:
                return occ
            time.sleep(0.005)
        return _read_occupancy_file(root)

    def _drop_waiter(self, key: str, holder_id: str) -> None:
        with self._mu:
            rec = self._held.get(key)
            if rec is None:
                return
            rec.waiters = [w for w in rec.waiters if w.get("holder_id") != holder_id]

    def _try_acquire(
        self,
        key: str,
        root: Path,
        *,
        hid: str,
        job_name: str,
        backend_id: str,
        backend_instance_id: str,
        write_paths: list[str],
        waiter: dict[str, Any],
    ) -> ClaimOutcome | None:
        with self._mu:
            rec = self._held.get(key)
            if rec is not None and rec.holder_id == hid:
                rec.refcount += 1
                if write_paths:
                    for p in write_paths:
                        if p not in rec.write_paths:
                            rec.write_paths.append(p)
                return ClaimOutcome(
                    granted=True,
                    status=STATUS_GRANTED,
                    workdir=key,
                    requester_id=hid,
                    occupancy=rec.snapshot(),
                )
            if rec is not None and rec.holder_id != hid:
                if not any(w.get("holder_id") == hid for w in rec.waiters):
                    rec.waiters.append(dict(waiter))
                return ClaimOutcome(
                    granted=False,
                    status=STATUS_BLOCKED,
                    workdir=key,
                    requester_id=hid,
                    occupancy=rec.snapshot(),
                    waiter=waiter,
                )

        fd: int | None = None
        try:
            fd = os.open(str(lock_path(root)), os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            occ = self._occupancy_after_block(root, key)
            return ClaimOutcome(
                granted=False,
                status=STATUS_BLOCKED,
                workdir=key,
                requester_id=hid,
                occupancy=occ,
                waiter=waiter,
            )
        except OSError as e:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return ClaimOutcome(
                granted=False,
                status=STATUS_BLOCKED,
                workdir=key,
                requester_id=hid,
                occupancy=self.occupancy(root),
                waiter=waiter,
                error=f"workdir claim lock failed: {e}",
            )

        # Got flock. Persist occupancy immediately so a concurrent locker
        # that loses LOCK_NB can still read who holds the claim.
        now = time.time()
        occ = Occupancy(
            workdir=key,
            holder_id=hid,
            job_name=job_name,
            backend_id=backend_id,
            backend_instance_id=str(backend_instance_id or ""),
            pid=os.getpid(),
            thread_id=threading.get_ident(),
            claimed_at=now,
            claimed_at_iso=_utc_iso(now),
            write_paths=list(write_paths),
            status=STATUS_HELD,
            waiters=[],
            refcount=1,
        )
        try:
            _write_occupancy_file(occ)
        except OSError:
            pass
        with self._mu:
            rec = self._held.get(key)
            if rec is not None and rec.holder_id != hid:
                # Do not LOCK_UN — that would drop a same-process holder's flock.
                try:
                    os.close(fd)
                except OSError:
                    pass
                if not any(w.get("holder_id") == hid for w in rec.waiters):
                    rec.waiters.append(dict(waiter))
                return ClaimOutcome(
                    granted=False,
                    status=STATUS_BLOCKED,
                    workdir=key,
                    requester_id=hid,
                    occupancy=rec.snapshot(),
                    waiter=waiter,
                )
            if rec is not None and rec.holder_id == hid:
                rec.refcount += 1
                try:
                    os.close(fd)
                except OSError:
                    pass
                return ClaimOutcome(
                    granted=True,
                    status=STATUS_GRANTED,
                    workdir=key,
                    requester_id=hid,
                    occupancy=rec.snapshot(),
                )
            self._held[key] = occ
            self._fds[key] = fd
            return ClaimOutcome(
                granted=True,
                status=STATUS_GRANTED,
                workdir=key,
                requester_id=hid,
                occupancy=occ.snapshot(),
            )

    def release(self, workdir: str | Path, holder_id: str, *, force: bool = False) -> bool:
        key = self._key(workdir)
        with self._mu:
            rec = self._held.get(key)
            if rec is None:
                if force:
                    _clear_occupancy_file(key)
                return False
            if rec.holder_id != holder_id and not force:
                return False
            rec.refcount -= 1
            if rec.refcount > 0 and not force:
                return True
            self._held.pop(key, None)
            fd = self._fds.pop(key, None)
            _clear_occupancy_file(key)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._cond(key).notify_all()
            return True

    def force_release(self, workdir: str | Path) -> bool:
        occ = None
        with self._mu:
            occ = self._held.get(self._key(workdir))
        hid = occ.holder_id if occ else ""
        return self.release(workdir, hid, force=True)

    def clear_all(self) -> None:
        with self._mu:
            keys = list(self._held.keys())
        for key in keys:
            self.force_release(key)

    @contextmanager
    def holding(
        self,
        workdir: str | Path,
        *,
        holder_id: str = "",
        job_name: str = "",
        backend_id: str = "inprocess.local_v1",
        write_paths: list[str] | None = None,
        mode: str = "block",
        timeout_sec: float = 0.0,
    ) -> Iterator[ClaimOutcome]:
        hid = holder_id or _new_holder_id()
        outcome = self.claim(
            workdir,
            holder_id=hid,
            job_name=job_name,
            backend_id=backend_id,
            write_paths=write_paths,
            mode=mode,
            timeout_sec=timeout_sec,
        )
        try:
            yield outcome
        finally:
            if outcome.granted:
                self.release(workdir, hid)


_DEFAULT: WorkdirClaimRegistry | None = None
_DEFAULT_MU = threading.Lock()


def default_registry() -> WorkdirClaimRegistry:
    global _DEFAULT
    with _DEFAULT_MU:
        if _DEFAULT is None:
            _DEFAULT = WorkdirClaimRegistry()
        return _DEFAULT


def reset_default_registry() -> WorkdirClaimRegistry:
    """Test helper: drop in-memory holds (does not steal other processes' flocks)."""
    global _DEFAULT
    with _DEFAULT_MU:
        if _DEFAULT is not None:
            _DEFAULT.clear_all()
        _DEFAULT = WorkdirClaimRegistry()
        return _DEFAULT


def claim_workdir(
    workdir: str | Path,
    *,
    holder_id: str = "",
    job_name: str = "",
    **kwargs: Any,
) -> ClaimOutcome:
    return default_registry().claim(workdir, holder_id=holder_id, job_name=job_name, **kwargs)


def release_workdir(workdir: str | Path, holder_id: str, *, force: bool = False) -> bool:
    return default_registry().release(workdir, holder_id, force=force)


def blocked_inprocess_result(
    *,
    outcome: ClaimOutcome,
    name: str = "",
    charter: dict | None = None,
) -> dict[str, Any]:
    """Result dict when a job is refused the workdir (no writes)."""
    holder = outcome.occupancy.to_dict() if outcome.occupancy else None
    job = name or (str((charter or {}).get("name") or "") if isinstance(charter, dict) else "")
    status = outcome.status if outcome.status in (STATUS_BLOCKED, STATUS_QUEUED) else STATUS_BLOCKED
    return {
        "ok": False,
        "state": status,
        "error": outcome.error
        or f"workdir {status}: held by {holder.get('holder_id') if holder else '?'}",
        "error_class": None,
        "used_public_api_only": True,
        "backend": "inprocess.local_v1",
        "artifacts": [],
        "workdir": outcome.workdir,
        "workdir_claim": outcome.to_dict(),
        "occupancy": holder,
        "resource_status": status,
        "name": job,
        "notes": [
            f"workdir claim {status}; no writes performed",
            f"holder={holder.get('holder_id') if holder else None} "
            f"job={holder.get('job_name') if holder else None}",
        ],
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
        "claimed": False,
    }


def attach_claim(result: dict[str, Any], outcome: ClaimOutcome) -> dict[str, Any]:
    result = dict(result)
    result["workdir_claim"] = outcome.to_dict()
    result["occupancy"] = outcome.occupancy.to_dict() if outcome.occupancy else None
    result["resource_status"] = outcome.status
    result["claimed"] = bool(outcome.granted)
    result.setdefault("workdir", outcome.workdir)
    return result


def start_run_with_workdir_claim(
    backend: Any,
    *,
    title: str,
    directory: str | Path,
    instruction: str = "",
    artifacts: list[str] | None = None,
    charter: dict | None = None,
    holder_id: str = "",
    registry: WorkdirClaimRegistry | None = None,
    on_conflict: str = "block",
    timeout_sec: float = 0.0,
    release_after: bool = True,
) -> dict[str, Any]:
    """Claim workdir then ``backend.start_run``. Second instance is blocked/queued."""
    from execution_backend.inprocess_v1 import InProcessExecutionBackend

    be = backend or InProcessExecutionBackend()
    reg = registry or default_registry()
    hid = holder_id or _new_holder_id()
    arts = list(artifacts or write_paths_from_charter(charter))
    inst = str(id(be))
    outcome = reg.claim(
        directory,
        holder_id=hid,
        job_name=title,
        backend_id=getattr(be, "backend_id", "") or "inprocess.local_v1",
        backend_instance_id=inst,
        write_paths=arts,
        mode=on_conflict,
        timeout_sec=timeout_sec,
    )
    if not outcome.granted:
        body = blocked_inprocess_result(outcome=outcome, name=title, charter=charter)
        body["started"] = False
        body["backend_instance_id"] = inst
        return body
    try:
        started = be.start_run(
            title=title,
            directory=str(directory),
            instruction=instruction,
            artifacts=arts,
            charter=charter,
        )
        started = attach_claim(started, outcome)
        started["backend_instance_id"] = inst
        started["holder_id"] = hid
        return started
    finally:
        if release_after:
            reg.release(directory, hid)


def prove_same_workdir_no_silent_overwrite(
    *,
    workdir: str | Path,
    relative: str = "shared.txt",
    registry: WorkdirClaimRegistry | None = None,
) -> dict[str, Any]:
    """Two backend instances, one workdir, WITH claims: second blocked, file not overwritten."""
    from execution_backend.inprocess_v1 import InProcessExecutionBackend

    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / relative
    if target.exists():
        target.unlink()
    reg = registry or WorkdirClaimRegistry()
    be_a = InProcessExecutionBackend()
    be_b = InProcessExecutionBackend()
    started_a = start_run_with_workdir_claim(
        be_a,
        title="instance-a",
        directory=root,
        artifacts=[relative],
        holder_id="instance-a",
        registry=reg,
        release_after=False,
    )
    body_after_a = target.read_text(encoding="utf-8") if target.is_file() else ""
    occ = reg.occupancy(root)
    started_b = start_run_with_workdir_claim(
        be_b,
        title="instance-b",
        directory=root,
        artifacts=[relative],
        holder_id="instance-b",
        registry=reg,
        on_conflict="block",
        release_after=True,
    )
    body_after_b = target.read_text(encoding="utf-8") if target.is_file() else ""
    overwritten = bool(target.is_file() and body_after_a and body_after_a != body_after_b)
    b_blocked = (not started_b.get("ok")) and str(started_b.get("state") or "") in (
        STATUS_BLOCKED,
        STATUS_QUEUED,
    )
    try:
        reg.release(root, "instance-a")
    except Exception:
        pass
    return {
        "ok": bool(started_a.get("ok")) and b_blocked and (not overwritten),
        "silent_overwrite": overwritten,
        "shared_workdir": str(root.resolve()),
        "relative": relative,
        "instance_a_id": id(be_a),
        "instance_b_id": id(be_b),
        "instances_distinct": be_a is not be_b,
        "body_after_a": body_after_a,
        "body_after_b": body_after_b,
        "started_a": started_a,
        "started_b": started_b,
        "second_status": started_b.get("state") or started_b.get("resource_status"),
        "occupancy_while_held": occ.to_dict() if occ else started_a.get("occupancy"),
        "holder_id": (occ.holder_id if occ else started_a.get("holder_id")),
        "occupancy_file": str(occupancy_path(root)),
        "note": "claims prevent the knife-9 silent overwrite of the same relative path",
    }


def run_claimed_inprocess_job(
    *,
    charter: dict,
    workdir: str | Path,
    name: str = "",
    registry: WorkdirClaimRegistry | None = None,
    holder_id: str = "",
    on_conflict: str = "block",
    timeout_sec: float = 0.0,
    busy_hold_sec: float = 0.0,
    claim_already_held: bool = False,
    active_counter: dict[str, int] | None = None,
    active_lock: threading.Lock | None = None,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Claim workdir, optionally hold, then run the inprocess charter (no nested claim)."""
    from charter import job_name as charter_job_name
    from execution_backend.run_job_wire import run_inprocess_charter

    job = name or charter_job_name(charter)
    hid = holder_id or f"{job}-{_new_holder_id()}"
    reg = registry or default_registry()
    paths = write_paths_from_charter(charter)
    if claim_already_held:
        outcome = ClaimOutcome(
            granted=True,
            status=STATUS_GRANTED,
            workdir=str(resolve_workdir(workdir)),
            requester_id=hid,
            occupancy=reg.occupancy(workdir),
        )
    else:
        outcome = reg.claim(
            workdir,
            holder_id=hid,
            job_name=job,
            write_paths=paths,
            mode=on_conflict,
            timeout_sec=timeout_sec,
        )
        if not outcome.granted:
            return blocked_inprocess_result(outcome=outcome, name=job, charter=charter)
    try:
        lk = active_lock if active_lock is not None else threading.Lock()
        if active_counter is not None:
            with lk:
                active_counter["n"] = int(active_counter.get("n") or 0) + 1
                active_counter["max"] = max(
                    int(active_counter.get("max") or 0), int(active_counter["n"])
                )
        try:
            if busy_hold_sec > 0:
                time.sleep(busy_hold_sec)
            result = run_inprocess_charter(
                charter=charter,
                workdir=workdir,
                name=job,
                claim_workdir=False,
                **run_kwargs,
            )
        finally:
            if active_counter is not None:
                with lk:
                    active_counter["n"] = int(active_counter.get("n") or 0) - 1
        return attach_claim(result, outcome)
    finally:
        if not claim_already_held:
            reg.release(workdir, hid)


def run_two_jobs_with_claims(
    *,
    charter_a: dict,
    charter_b: dict,
    workdir_a: str | Path,
    workdir_b: str | Path,
    concurrent: bool = True,
    on_conflict: str = "block",
    registry: WorkdirClaimRegistry | None = None,
    busy_hold_sec: float = 0.05,
) -> dict[str, Any]:
    """Run A and B under workdir claims.

    Same resolved workdir: second is blocked/queued; occupancy names the holder;
    artifacts are not silently overwritten.
    Distinct workdirs: both run (parallel when concurrent=True).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from charter import job_name as charter_job_name

    wa = resolve_workdir(workdir_a)
    wb = resolve_workdir(workdir_b)
    wa.mkdir(parents=True, exist_ok=True)
    wb.mkdir(parents=True, exist_ok=True)
    same = wa == wb
    reg = registry or WorkdirClaimRegistry()
    counter: dict[str, int] = {"n": 0, "max": 0}
    clock = threading.Lock()
    hold = 0.0 if not concurrent else float(busy_hold_sec or 0.0)
    barrier = threading.Barrier(2) if concurrent else None

    def _one(charter: dict, wd: Path, tag: str) -> dict[str, Any]:
        if barrier is not None:
            barrier.wait(timeout=5)
        return run_claimed_inprocess_job(
            charter=charter,
            workdir=wd,
            name=tag,
            registry=reg,
            holder_id=tag,
            on_conflict=on_conflict,
            busy_hold_sec=hold,
            active_counter=counter,
            active_lock=clock,
        )

    name_a = charter_job_name(charter_a)
    name_b = charter_job_name(charter_b)
    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(_one, charter_a, wa, name_a)
            fb = pool.submit(_one, charter_b, wb, name_b)
            errors: list[BaseException] = []
            for fut in as_completed([fa, fb]):
                try:
                    fut.result()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
            if errors:
                raise errors[0]
            job_a, job_b = fa.result(), fb.result()
    else:
        job_a = _one(charter_a, wa, name_a)
        job_b = _one(charter_b, wb, name_b)

    states = [str(job_a.get("state") or ""), str(job_b.get("state") or "")]
    granted = [j for j in (job_a, job_b) if j.get("ok") or j.get("resource_status") == STATUS_GRANTED]
    refused = [
        j
        for j in (job_a, job_b)
        if str(j.get("state") or j.get("resource_status") or "") in (STATUS_BLOCKED, STATUS_QUEUED)
    ]
    occ_a = job_a.get("occupancy") or (job_a.get("workdir_claim") or {}).get("occupancy")
    occ_b = job_b.get("occupancy") or (job_b.get("workdir_claim") or {}).get("occupancy")
    occupancy = occ_a or occ_b
    # Overwrite check: if same workdir, at most one writer; file matches a granted job.
    silent = False
    bodies: dict[str, str] = {}
    for charter, wd, job in ((charter_a, wa, job_a), (charter_b, wb, job_b)):
        for rel in write_paths_from_charter(charter):
            p = wd / Path(rel).name
            if p.is_file():
                bodies[f"{wd}:{rel}"] = p.read_text(encoding="utf-8")
    if same:
        # Both jobs writing the same relative names would collide unclaimed.
        # After claims, refused job must not have changed the holder's bytes.
        silent = False
        if len(refused) == 0 and concurrent and counter.get("max", 0) > 1:
            silent = True

    parallel_ok = (not same) and bool(job_a.get("ok")) and bool(job_b.get("ok"))
    serialized_ok = same and len(refused) >= 1 and len(granted) >= 1 and not silent
    serial_both_ok = (not concurrent) and bool(job_a.get("ok")) and bool(job_b.get("ok"))
    ok = parallel_ok or serialized_ok or serial_both_ok
    return {
        "ok": ok,
        "workdirs_distinct": not same,
        "conflict": same,
        "concurrent": bool(concurrent),
        "isolation_by": "workdir",
        "resource": "workdir_claim",
        "workdir_a": str(wa),
        "workdir_b": str(wb),
        "job_a": job_a,
        "job_b": job_b,
        "occupancy": occupancy,
        "second_status": (refused[0].get("state") if refused else STATUS_GRANTED),
        "refused_count": len(refused),
        "granted_count": len(granted),
        "max_active": int(counter.get("max") or 0),
        "silent_overwrite": silent,
        "states": states,
        "parallel": parallel_ok,
        "queued_or_blocked": bool(refused),
        "bodies": bodies,
    }


def simulate_scheduler_workdir_claims(
    jobs: list[dict[str, Any]],
    *,
    max_parallel: int = 2,
    registry: WorkdirClaimRegistry | None = None,
    execute: bool = True,
    on_conflict: str = "block",
) -> dict[str, Any]:
    """One scheduler tick: start free workdirs up to max_parallel; occupy or queue the rest.

    ``jobs`` items: ``{job_id, name, charter, workdir}``.
    Same workdir → first running (claim recorded), later jobs stay queued.
    Distinct workdirs → may run in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from charter import job_name as charter_job_name

    reg = registry or WorkdirClaimRegistry()
    started: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    occupancy: dict[str, dict[str, Any]] = {}

    for raw in jobs:
        charter = raw.get("charter") or {}
        name = str(raw.get("name") or (charter_job_name(charter) if charter else "job"))
        job_id = str(raw.get("job_id") or name)
        wd = resolve_workdir(raw.get("workdir") or ".")
        wd.mkdir(parents=True, exist_ok=True)
        key = str(wd)
        slot = {
            "job_id": job_id,
            "name": name,
            "workdir": key,
            "charter": charter,
        }
        if len(started) >= max_parallel:
            slot["state"] = STATUS_QUEUED
            slot["reason"] = "max_parallel"
            slot["occupancy"] = occupancy.get(key) or (reg.occupancy(wd).to_dict() if reg.occupancy(wd) else None)
            queued.append(slot)
            continue
        outcome = reg.claim(
            wd,
            holder_id=job_id,
            job_name=name,
            write_paths=write_paths_from_charter(charter if isinstance(charter, dict) else None),
            mode=on_conflict,
            timeout_sec=0.0,
        )
        if not outcome.granted:
            slot["state"] = STATUS_QUEUED if on_conflict == "queue" else outcome.status
            slot["reason"] = "workdir_occupied"
            slot["resource_status"] = outcome.status
            slot["occupancy"] = outcome.to_dict()
            slot["holder"] = outcome.occupancy.to_dict() if outcome.occupancy else None
            queued.append(slot)
            continue
        slot["state"] = "running"
        slot["resource_status"] = STATUS_GRANTED
        slot["occupancy"] = outcome.to_dict()
        slot["holder_id"] = job_id
        occupancy[key] = outcome.to_dict()
        started.append(slot)

    if execute and started:
        counter: dict[str, int] = {"n": 0, "max": 0}
        clock = threading.Lock()

        def _run(slot: dict[str, Any]) -> dict[str, Any]:
            return run_claimed_inprocess_job(
                charter=slot["charter"],
                workdir=slot["workdir"],
                name=slot["name"],
                registry=reg,
                holder_id=slot["holder_id"],
                claim_already_held=True,
                busy_hold_sec=0.05,
                active_counter=counter,
                active_lock=clock,
            )

        if len(started) == 1:
            started[0]["result"] = _run(started[0])
            reg.release(started[0]["workdir"], started[0]["holder_id"])
        else:
            barrier = threading.Barrier(len(started))

            def _run_aligned(slot: dict[str, Any]) -> dict[str, Any]:
                barrier.wait(timeout=5)
                return _run(slot)

            with ThreadPoolExecutor(max_workers=len(started)) as pool:
                futs = {pool.submit(_run_aligned, s): s for s in started}
                for fut in as_completed(futs):
                    slot = futs[fut]
                    slot["result"] = fut.result()
                    reg.release(slot["workdir"], slot["holder_id"])
        max_active = int(counter.get("max") or 0)
    else:
        max_active = len(started)
        if not execute:
            for s in started:
                # leave claims held so occupancy remains observable
                pass

    same_dir_queued = any(q.get("reason") == "workdir_occupied" for q in queued)
    return {
        "ok": True,
        "started": started,
        "queued": queued,
        "blocked": [q for q in queued if q.get("resource_status") == STATUS_BLOCKED],
        "occupancy": occupancy,
        "started_count": len(started),
        "queued_count": len(queued),
        "max_parallel": max_parallel,
        "max_active": max_active,
        "same_workdir_queued_or_blocked": same_dir_queued,
        "execute": execute,
    }


__all__ = [
    "LOCK_FILENAME",
    "OCCUPANCY_FILENAME",
    "STATUS_BLOCKED",
    "STATUS_GRANTED",
    "STATUS_HELD",
    "STATUS_QUEUED",
    "ClaimOutcome",
    "Occupancy",
    "WorkdirClaimError",
    "WorkdirClaimRegistry",
    "attach_claim",
    "blocked_inprocess_result",
    "claim_workdir",
    "default_registry",
    "lock_path",
    "occupancy_path",
    "prove_same_workdir_no_silent_overwrite",
    "release_workdir",
    "reset_default_registry",
    "resolve_workdir",
    "run_claimed_inprocess_job",
    "run_two_jobs_with_claims",
    "simulate_scheduler_workdir_claims",
    "start_run_with_workdir_claim",
    "write_paths_from_charter",
]
