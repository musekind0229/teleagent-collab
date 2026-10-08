"""Async lead review runner (spec §6–§6.5).

Ticks submit or poll. The lead call runs on a daemon thread. Durable state is
``decision.details.lead_review`` via ``annotate_decision``. Events are appended
to ``details.lead_review.events`` because ``record_goal_event`` cannot store them.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable

from lead_adapter.cancel import CancelScope, use_scope as use_lead_scope

_LOG = logging.getLogger("framework.lead_review_runner")
_REQUEST_RE = re.compile(r"^agyrev:(agy_[0-9a-f]{12}):r([1-9][0-9]*)$")
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+\S+|sk-[a-z0-9]{8,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)"
)
_KEEP = 10
_EVENT_FIELD_CAP = 300
_EVENTS_MAX_BYTES = 4096
_LATE, _STALE = "lead_review_late_result", "lead_review_stale_result"
_SUPERSEDED, _STOPPED = "lead_review_superseded", "lead_review_stopped"


def _iso(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _sanitize(text: Any, limit: int = 200) -> str:
    cleaned = " ".join(_SECRET_RE.sub("[redacted]", str(text or "")).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _event_field(value: Any) -> Any:
    """Redact and cap strings. Leave scalars. Anything else becomes a capped string."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        value = str(value)
    return _sanitize(value, _EVENT_FIELD_CAP)


_CORE_EVENT_KEYS = ("event", "at", "at_iso", "job_id", "request_id")
_EVENT_MARKER_KEYS = ("truncated", "dropped_fields")
_DROP = object()


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value))
    except (TypeError, ValueError):
        return _EVENTS_MAX_BYTES + 1


def _shrink_event_value(value: Any) -> Any:
    """Return a strictly smaller JSON value, or ``_DROP`` to remove it."""
    if isinstance(value, str):
        if len(value) <= 16:
            return _DROP
        target = max(16, len(value) // 2)
        shortened = value[: target - 3].rstrip() + "..."
        if len(shortened) >= len(value):
            return _DROP
        return shortened
    if isinstance(value, list):
        if not value:
            return _DROP
        if len(value) == 1:
            inner = _shrink_event_value(value[0])
            if inner is _DROP:
                return _DROP
            return [inner]
        return value[:-1]
    if isinstance(value, dict):
        if not value:
            return _DROP
        keys = list(value)
        if len(keys) == 1:
            inner = _shrink_event_value(value[keys[0]])
            if inner is _DROP:
                return _DROP
            return {keys[0]: inner}
        shrunk = dict(value)
        shrunk.pop(keys[-1], None)
        return shrunk
    return _DROP


def _bound_event(row: dict[str, Any]) -> dict[str, Any]:
    """Drop or shorten non-core fields until ``json.dumps([row])`` fits in 4096.

    Field capping at 300 characters still leaves one wide event over the events
    budget. The last non-core field is shortened, then removed, until the row
    fits. A row that already fits is returned unchanged, with no truncation marker.
    """
    if _json_len([row]) <= _EVENTS_MAX_BYTES:
        return row
    core = set(_CORE_EVENT_KEYS)
    working = {key: value for key, value in row.items() if key not in _EVENT_MARKER_KEYS}
    extras = [key for key in working if key not in core]
    dropped = 0
    shortened = False

    def marked() -> dict[str, Any]:
        probe = dict(working)
        if dropped or shortened:
            probe["truncated"] = True
            if dropped:
                probe["dropped_fields"] = dropped
        return probe

    guard = 0
    while extras and _json_len([marked()]) > _EVENTS_MAX_BYTES:
        guard += 1
        key = extras[-1]
        current = working.get(key)
        shrunk = _shrink_event_value(current)
        if guard > 4000 or shrunk is _DROP or _json_len(shrunk) >= _json_len(current):
            working.pop(key, None)
            extras.pop()
            dropped += 1
            continue
        working[key] = shrunk
        shortened = True
    if _json_len([marked()]) > _EVENTS_MAX_BYTES:
        for key in list(extras):
            if key in working:
                working.pop(key, None)
                dropped += 1
        extras.clear()
    for key in ("request_id", "job_id", "at_iso"):
        if _json_len([marked()]) <= _EVENTS_MAX_BYTES:
            break
        value = working.get(key)
        if isinstance(value, str) and len(value) > 16:
            nxt = value[:13].rstrip() + "..."
            if len(nxt) < len(value):
                working[key] = nxt
                shortened = True
    fitted = marked()
    if _json_len([fitted]) > _EVENTS_MAX_BYTES:
        fitted = {key: working[key] for key in _CORE_EVENT_KEYS if key in working}
        fitted["truncated"] = True
    row.clear()
    row.update(fitted)
    return row


def _sanitize_process_value(value: Any, depth: int = 0) -> Any:
    """Redact and cap one process value. Small lists and dicts keep their shape."""
    if depth >= 6:
        return _event_field(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize(value, _EVENT_FIELD_CAP)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 32:
                break
            text = str(key)[:80]
            if not text or text in out:
                continue
            out[text] = _sanitize_process_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_process_value(item, depth + 1) for item in list(value)[:8]]
    return _event_field(value)


def _sanitize_processes(procs: list[Any]) -> list[Any]:
    return [_sanitize_process_value(item) for item in list(procs)[:8]]


def _round_of(request_id: str) -> int:
    match = _REQUEST_RE.match(str(request_id or ""))
    return int(match.group(2)) if match else 0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class LeadReviewRunner:
    """Submit, poll, and finish one async lead review per durable decision."""

    MAX_ATTEMPTS = 2
    WATCHDOG_GRACE_SEC = 30.0
    MAX_CONCURRENT_REVIEWS = 4
    STOPPER_JOIN_SEC = 5.0

    def __init__(
        self, *, layer: Any, planner: Any, backend: Any, actor_id: str = "app-coordinator",
        clock: Callable[[], float] = time.time, token: str | None = None,
        error_record: Callable[[BaseException], Mapping[str, Any]] | None = None,
    ) -> None:
        self.layer, self.planner, self.backend = layer, planner, backend
        self.actor_id = str(actor_id or "").strip() or "app-coordinator"
        self._clock = clock or time.time
        self.token = str(token or "").strip() or f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._error_record = error_record
        self._jobs: dict[str, dict[str, Any]] = {}
        self._abandoned: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._applying: str | None = None
        self._late_once: set[tuple[str, str]] = set()
        self._stoppers: list[threading.Thread] = []

    @classmethod
    def for_coordinator(cls, coord: Any) -> LeadReviewRunner:
        runner = cls(
            layer=coord.layer, planner=coord.planner, backend=coord.backend,
            clock=getattr(coord, "_clock", None) or time.time,
        )
        planner, backend = coord.planner, coord.backend
        if callable(getattr(planner, "decide_action", None)) and callable(getattr(backend, "enable_lead_review", None)):
            backend.enable_lead_review()
        listen = getattr(backend, "add_review_listener", None)
        if callable(listen):
            listen(runner._on_backend_resolved)
        return runner

    def owns(self, action: Any) -> bool:
        if not isinstance(action, Mapping) or str(action.get("kind") or "") != "review":
            return False
        return bool(
            getattr(self.backend, "lead_review_mode", "") == "async_v1"
            and callable(getattr(self.planner, "decide_action", None))
            and callable(getattr(self.backend, "resolve_decision", None))
        )

    def live_jobs(self) -> int:
        with self._lock:
            return len(self._jobs)

    def step(self, snap: Any, task: Any, *, decision: Any, action: Any) -> dict[str, Any]:
        snap = snap if isinstance(snap, Mapping) else {}
        decision = decision if isinstance(decision, Mapping) else {}
        action = action if isinstance(action, Mapping) else {}
        goal_id = str(snap.get("goal_id") or "")
        decision_id = str(decision.get("decision_id") or "")
        request_id = str(action.get("request_id") or decision.get("request_id") or "")
        self._sweep_abandoned()
        pending = self._pending(goal_id, decision_id)
        if pending is not None:
            decision = pending
        # Backend truth wins before any lead call. ValueError on resolve is only
        # the race where another writer lands between this read and the send.
        outcome = self._review_outcome(request_id)
        if outcome is not None:
            self._drop_job(decision_id, "recovered")
            return self._recover(snap, goal_id, decision_id, request_id, decision, outcome)
        lead = self._lead_state(decision)
        self._bind(lead, request_id)
        job = self._locked_job(decision_id)
        if job is None:
            return self._without_job(snap, task, action, goal_id, decision_id, request_id, lead)
        if not job["done"].is_set():
            if self._now() > float(job["deadline"]):
                return self._watchdog(goal_id, decision_id, lead, job)
            return self._reviewing(decision_id, _as_int(job.get("attempt"), 1))
        if not self._pop_if_current(decision_id, job) or str(job.get("job_id") or "") != str(lead.get("job_id") or ""):
            self._record_late(job)
            return self._reviewing(decision_id, _as_int(job.get("attempt") or lead.get("attempt"), 1))
        if job.get("error") is not None:
            return self._note_error(goal_id, decision_id, request_id, lead, job)
        return self._apply_verdict(snap, goal_id, decision_id, request_id, lead, job)

    def stop_goal(self, goal_id: str, reason: str) -> None:
        for job in self._take_jobs(str(goal_id or "")):
            self._mark_stopped(job, reason)
            self._kill_later(job, reason)

    def stop_all(self, reason: str) -> list[dict[str, Any]]:
        jobs = self._take_jobs(None)
        for job in jobs:
            self._mark_stopped(job, reason)
        rows = []
        for job in jobs:
            procs = self._cancel_wait(job, reason, 3.0)
            self._patch_processes(job, procs)
            rows.append({
                "decision_id": job.get("decision_id"), "goal_id": job.get("goal_id"),
                "request_id": job.get("request_id"), "job_id": job.get("job_id"),
                "reason": str(reason or "")[:200], "lead_processes": list(procs)[:8],
            })
        self._join_stoppers(float(self.STOPPER_JOIN_SEC))
        return rows

    def _on_backend_resolved(self, request_id: str, verdict: str, state: str) -> None:
        try:
            rid = str(request_id or "")
            if rid and rid == self._applying:
                return
            with self._lock:
                found = next((job for job in self._jobs.values() if str(job.get("request_id") or "") == rid), None)
                if found is not None:
                    self._jobs.pop(str(found.get("decision_id") or ""), None)
            if found is None:
                return
            self._record_event(
                str(found.get("goal_id") or ""), str(found.get("decision_id") or ""), _SUPERSEDED,
                request_id=rid, job_id=found.get("job_id"),
                verdict=str(verdict or ""), state=str(state or ""),
            )
            self._kill_later(found, "superseded")
        except Exception:
            _LOG.info("lead review supersede failed")

    def _start_thread(self, job: dict[str, Any]) -> None:
        threading.Thread(
            target=self._run, args=(job,), name=f"collab-lead-review-{job.get('job_id')}", daemon=True,
        ).start()

    def _run(self, job: dict[str, Any]) -> None:
        try:
            with use_lead_scope(job.get("scope")):
                job["result"] = self.planner.decide_action(job.get("snap"), job.get("task"), job.get("action"))
        except BaseException as exc:  # noqa: BLE001 — the next tick reads job.error
            job["error"] = exc
        finally:
            done = job.get("done")
            if isinstance(done, threading.Event):
                done.set()

    def _without_job(self, snap, task, action, goal_id, decision_id, request_id, lead) -> dict[str, Any]:
        status, owner = str(lead.get("status") or ""), str(lead.get("owner") or "")
        if status == "running" and owner != self.token:
            lead["status"] = "interrupted"
            lead["interrupted_at"] = self._now()
            self._append_history(lead, _as_int(lead.get("attempt")), "interrupted", "interrupted")
            self._write(goal_id, decision_id, lead)
        attempt = _as_int(lead.get("attempt"))
        last = lead.get("last_error") if isinstance(lead.get("last_error"), Mapping) else None
        if attempt >= int(self.MAX_ATTEMPTS) or (last is not None and last.get("retryable") is False):
            code = str((last or {}).get("code") or "interrupted")
            return self._finalize(snap, goal_id, decision_id, request_id, lead, code, attempt)
        if self.live_jobs() >= int(self.MAX_CONCURRENT_REVIEWS):
            lead.update({"status": "queued", "owner": self.token, "attempt": attempt})
            self._bind(lead, request_id)
            self._write(goal_id, decision_id, lead)
            return {"ok": True, "state": "running", "action": "lead_review_queued", "decision_id": decision_id, "attempt": attempt}
        return self._submit(snap, task, action, goal_id, decision_id, request_id, lead, attempt)

    def _submit(self, snap, task, action, goal_id, decision_id, request_id, lead, attempt_before: int) -> dict[str, Any]:
        attempt, now = attempt_before + 1, self._now()
        timeout_sec, grace = self._timeout_sec(), float(self.WATCHDOG_GRACE_SEC)
        job: dict[str, Any] = {
            "job_id": f"lrj_{uuid.uuid4().hex[:8]}", "attempt": attempt, "started_at": now,
            "deadline": now + timeout_sec + grace, "timeout_sec": timeout_sec, "scope": CancelScope(),
            "done": threading.Event(), "result": None, "error": None, "goal_id": goal_id,
            "decision_id": decision_id, "request_id": request_id, "snap": copy.deepcopy(snap),
            "task": copy.deepcopy(task), "action": copy.deepcopy(dict(action)),
        }
        lead.update({
            "status": "running", "attempt": attempt, "job_id": job["job_id"], "owner": self.token,
            "started_at": now, "started_at_iso": _iso(now), "deadline": job["deadline"], "timeout_sec": timeout_sec,
        })
        self._bind(lead, request_id)
        if not self._write(goal_id, decision_id, lead).get("ok"):
            return {"ok": False, "action": "lead_review_not_started", "decision_id": decision_id, "attempt": attempt}
        with self._lock:
            self._jobs[decision_id] = job
        try:
            self._start_thread(job)
        except Exception as exc:
            with self._lock:
                self._jobs.pop(decision_id, None)
            lead["status"] = "failed"
            lead["last_error"] = {"code": "thread_start_failed", "message": _sanitize(exc or "thread_start_failed"), "retryable": True}
            self._append_history(lead, attempt, "failed", "thread_start_failed")
            self._write(goal_id, decision_id, lead)
            return {"ok": False, "state": "running", "action": "lead_reviewing", "decision_id": decision_id, "attempt": attempt}
        return self._reviewing(decision_id, attempt)

    def _watchdog(self, goal_id, decision_id, lead, job) -> dict[str, Any]:
        if job["done"].is_set():
            return self._reviewing(decision_id, _as_int(job.get("attempt"), 1))
        self._abandon(job)
        try:
            shown = int(job.get("timeout_sec"))
        except (TypeError, ValueError):
            shown = 0
        lead["status"] = "timeout"
        lead["last_error"] = {"code": "timeout", "message": f"no lead answer within {shown}s (watchdog)", "retryable": True}
        self._append_history(lead, _as_int(job.get("attempt")), "timeout", "timeout")
        self._bind(lead, str(job.get("request_id") or ""))
        self._write(goal_id, decision_id, lead)
        self._kill_later(job, "lead review watchdog")
        return self._reviewing(decision_id, _as_int(job.get("attempt"), 1))

    def _note_error(self, goal_id, decision_id, request_id, lead, job) -> dict[str, Any]:
        rec = self._error_from(job.get("error"))
        status = "timeout" if rec.get("code") == "timeout" else "failed"
        lead.update({"status": status, "attempt": _as_int(job.get("attempt"), _as_int(lead.get("attempt"))), "last_error": rec})
        self._append_history(lead, _as_int(lead.get("attempt")), status, str(rec.get("code") or ""))
        self._bind(lead, request_id)
        self._write(goal_id, decision_id, lead)
        return self._reviewing(decision_id, _as_int(lead.get("attempt"), 1))

    def _apply_verdict(self, snap, goal_id, decision_id, request_id, lead, job) -> dict[str, Any]:
        result = job.get("result")
        verdict = str(result.get("verdict") or "").strip().lower() if isinstance(result, Mapping) else ""
        reason = str(result.get("reason") or "") if isinstance(result, Mapping) else ""
        if verdict not in {"pass", "fail"}:
            err = RuntimeError("lead review verdict was not pass or fail")
            setattr(err, "code", "illegal_verdict")
            job["error"] = err
            return self._note_error(goal_id, decision_id, request_id, lead, job)
        try:
            applied = self._call_resolve(request_id, verdict, reason, "lead")
            if not isinstance(applied, Mapping) or not applied.get("ok"):
                raise RuntimeError("backend resolve_decision was not applied")
        except ValueError as exc:
            return self._discard_stale(snap, goal_id, decision_id, request_id, lead, job, exc)
        except Exception as exc:
            return self._backend_failed(goal_id, decision_id, request_id, lead, job, exc)
        lead["status"] = "done"
        self._bind(lead, request_id)
        self._write(goal_id, decision_id, lead)
        return self._resolved(self._durable(snap, goal_id, decision_id, verdict, reason))

    def _finalize(self, snap, goal_id, decision_id, request_id, lead, code: str, attempts: int) -> dict[str, Any]:
        token, count = str(code or "interrupted"), _as_int(attempts)
        reason = f"lead_review_unavailable: {token} after {count} attempt(s)"
        stub = {"job_id": lead.get("job_id"), "attempt": count}
        try:
            applied = self._call_resolve(request_id, "unavailable", reason, "system")
            if not isinstance(applied, Mapping) or not applied.get("ok"):
                raise RuntimeError("backend resolve_decision was not applied")
        except ValueError as exc:
            return self._discard_stale(snap, goal_id, decision_id, request_id, lead, stub, exc)
        except Exception as exc:
            return self._backend_failed(goal_id, decision_id, request_id, lead, stub, exc)
        lead.update({"status": "unavailable", "attempt": count})
        self._bind(lead, request_id)
        self._write(goal_id, decision_id, lead)
        return self._resolved(self._durable(snap, goal_id, decision_id, "reject", reason))

    def _discard_stale(self, snap, goal_id, decision_id, request_id, lead, job, exc: BaseException) -> dict[str, Any]:
        # Discard the lead verdict. Do not send unavailable or touch the worker round.
        err = str(exc).strip()
        lead["status"] = "stale_discarded"
        self._append_event(lead, _STALE, decision_id=decision_id, request_id=request_id, job_id=job.get("job_id"), error=err[:500])
        self._bind(lead, request_id)
        self._write(goal_id, decision_id, lead)
        outcome = self._review_outcome(request_id)
        if outcome is not None:
            verdict, reason = self._durable_verdict(outcome.get("verdict")), str(outcome.get("reason") or "")
        else:
            verdict = "reject"
            reason = f"stale review request discarded; worker state unchanged: {err}"[:500]
        return self._resolved(self._durable(snap, goal_id, decision_id, verdict, reason))

    def _backend_failed(self, goal_id, decision_id, request_id, lead, job, exc: BaseException) -> dict[str, Any]:
        lead.update({
            "status": "failed",
            "attempt": _as_int(job.get("attempt"), _as_int(lead.get("attempt"))),
            "last_error": {"code": "backend_resolve_failed", "message": _sanitize(exc), "retryable": False},
        })
        self._append_history(lead, _as_int(lead.get("attempt")), "failed", "backend_resolve_failed")
        self._bind(lead, request_id)
        self._write(goal_id, decision_id, lead)
        return self._reviewing(decision_id, _as_int(lead.get("attempt"), 1))

    def _recover(self, snap, goal_id, decision_id, request_id, decision, outcome) -> dict[str, Any]:
        lead = self._lead_state(decision)
        lead["status"] = "done"
        self._bind(lead, request_id)
        self._write(goal_id, decision_id, lead)
        out = self._resolved(self._durable(
            snap, goal_id, decision_id, self._durable_verdict(outcome.get("verdict")), str(outcome.get("reason") or ""),
        ))
        out["recovered"] = True
        return out

    def _call_resolve(self, request_id: str, verdict: str, reason: str, by: str) -> Any:
        self._applying = request_id
        try:
            return self.backend.resolve_decision(request_id, verdict=verdict, reason=reason, answers=[{"by": by}])
        finally:
            self._applying = None

    def _review_outcome(self, request_id: str) -> dict[str, Any] | None:
        fn = getattr(self.backend, "review_outcome", None)
        if not callable(fn) or not request_id:
            return None
        try:
            out = fn(request_id)
        except Exception:
            return None
        if not isinstance(out, Mapping) or not str(out.get("verdict") or "").strip():
            return None
        return dict(out)

    def _durable(self, snap, goal_id, decision_id, verdict: str, reason: str) -> dict[str, Any]:
        try:
            out = self.layer.resolve_decision(
                goal_id, decision_id=decision_id, verdict=verdict, reason=reason,
                extra={"actor_id": self._actor(snap)},
            )
        except Exception as exc:
            _LOG.info("durable resolve failed: %s", type(exc).__name__)
            return {"ok": False, "error": type(exc).__name__}
        return out if isinstance(out, dict) else {"ok": False}

    def _resolved(self, resolved: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(resolved)
        out["action"] = "decision_resolved"
        out["lead"] = str(getattr(self.planner, "name", "") or "")
        return out

    def _reviewing(self, decision_id: str, attempt: int) -> dict[str, Any]:
        return {"ok": True, "state": "running", "action": "lead_reviewing", "decision_id": decision_id, "attempt": attempt}

    @staticmethod
    def _durable_verdict(verdict: Any) -> str:
        token = str(verdict or "").strip().lower()
        return token if token in {"pass", "fail"} else "reject"

    def _actor(self, snap: Mapping[str, Any]) -> str:
        # Same convention as AppCoordinator._lead_resolve_opened.
        ownership = snap.get("ownership") if isinstance(snap.get("ownership"), Mapping) else {}
        return str(ownership.get("coordinator_id") or "").strip() or self.actor_id

    def _timeout_sec(self) -> float:
        try:
            value = float(getattr(self.planner, "timeout_sec", 180.0))
        except (TypeError, ValueError):
            return 180.0
        return value if value > 0 else 180.0

    def _now(self) -> float:
        try:
            return float(self._clock())
        except (TypeError, ValueError):
            return time.time()

    def _bind(self, lead: dict[str, Any], request_id: str) -> None:
        if request_id:
            lead["request_id"] = request_id
            rnd = _round_of(request_id)
            if rnd:
                lead["round"] = rnd
        lead["max_attempts"] = int(self.MAX_ATTEMPTS)

    def _error_from(self, exc: BaseException) -> dict[str, Any]:
        from framework.app_service import _lead_error_record, lead_error_retryable

        try:
            raw = (self._error_record or _lead_error_record)(exc)
        except Exception:
            raw = {}
        rec = dict(raw) if isinstance(raw, Mapping) else {}
        retryable = bool(rec.get("retryable")) if "retryable" in rec else bool(lead_error_retryable(exc))
        return {
            "code": str(rec.get("code") or getattr(exc, "code", "") or type(exc).__name__)[:80],
            "message": _sanitize(rec.get("message") if rec.get("message") is not None else exc),
            "retryable": retryable,
        }

    def _append_history(self, lead: dict[str, Any], attempt: int, status: str, code: str) -> None:
        history = [dict(item) for item in (lead.get("history") or []) if isinstance(item, Mapping)]
        history.append({"attempt": int(attempt), "status": status, "code": code, "at": self._now()})
        lead["history"] = history[-_KEEP:]

    def _append_event(self, lead: dict[str, Any], name: str, **fields: Any) -> None:
        now = self._now()
        row = {
            "event": name, "at": now, "at_iso": _iso(now),
            "job_id": fields.get("job_id", lead.get("job_id")),
            "request_id": fields.get("request_id", lead.get("request_id")),
        }
        row.update({key: value for key, value in fields.items() if key not in row})
        row = {key: _event_field(value) for key, value in row.items()}
        _bound_event(row)
        events = [dict(item) for item in (lead.get("events") or []) if isinstance(item, Mapping)]
        events.append(row)
        events = events[-_KEEP:]
        while len(events) > 1:
            try:
                encoded = json.dumps(events)
            except (TypeError, ValueError):
                break
            if len(encoded) <= _EVENTS_MAX_BYTES:
                break
            del events[0]
        if len(events) == 1 and _json_len(events) > _EVENTS_MAX_BYTES:
            _bound_event(events[-1])
        lead["events"] = events

    def _lead_state(self, decision: Mapping[str, Any]) -> dict[str, Any]:
        details = decision.get("details") if isinstance(decision.get("details"), Mapping) else {}
        raw = details.get("lead_review") if isinstance(details, Mapping) else None
        if not isinstance(raw, Mapping):
            return {}
        lead = dict(raw)
        for key in ("history", "events"):
            if isinstance(lead.get(key), list):
                lead[key] = [dict(item) if isinstance(item, Mapping) else item for item in lead[key]]
        if isinstance(lead.get("last_error"), Mapping):
            lead["last_error"] = dict(lead["last_error"])
        return lead

    def _write(self, goal_id: str, decision_id: str, lead: Mapping[str, Any]) -> dict[str, Any]:
        try:
            out = self.layer.annotate_decision(str(goal_id), decision_id=str(decision_id), details={"lead_review": dict(lead)})
        except Exception as exc:
            _LOG.info("annotate lead_review failed: %s", type(exc).__name__)
            return {"ok": False}
        return out if isinstance(out, dict) else {"ok": False}

    def _pending(self, goal_id: str, decision_id: str) -> dict[str, Any] | None:
        if not goal_id or not decision_id:
            return None
        try:
            got = self.layer.get_goal(goal_id)
        except Exception:
            return None
        snap = got.get("goal") if isinstance(got, Mapping) else None
        if not isinstance(snap, Mapping):
            return None
        for row in snap.get("pending_decisions") or []:
            if isinstance(row, Mapping) and str(row.get("decision_id") or "") == decision_id:
                return dict(row)
        return None

    def _record_event(self, goal_id: str, decision_id: str, name: str, **fields: Any) -> None:
        pending = self._pending(goal_id, decision_id)
        if pending is None:
            _LOG.info("lead review event %s not stored; decision %s is not pending", name, decision_id)
            return
        lead = self._lead_state(pending)
        fields.setdefault("decision_id", decision_id)
        self._append_event(lead, name, **fields)
        if not self._write(goal_id, decision_id, lead).get("ok"):
            _LOG.info("lead review event %s not stored; decision %s is not pending", name, decision_id)

    def _record_late(self, job: Mapping[str, Any]) -> None:
        decision_id, job_id = str(job.get("decision_id") or ""), str(job.get("job_id") or "")
        key = (decision_id, job_id)
        if key in self._late_once:
            return
        self._late_once.add(key)
        self._record_event(
            str(job.get("goal_id") or ""), decision_id, _LATE,
            request_id=job.get("request_id"), job_id=job_id,
        )

    def _sweep_abandoned(self) -> None:
        with self._lock:
            finished = [job for job in self._abandoned.values() if job["done"].is_set()]
            for job in finished:
                self._abandoned.pop(str(job.get("job_id") or ""), None)
        for job in finished:
            self._record_late(job)

    def _locked_job(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(decision_id)

    def _pop_if_current(self, decision_id: str, job: Mapping[str, Any]) -> bool:
        with self._lock:
            if self._jobs.get(decision_id) is not job:
                return False
            self._jobs.pop(decision_id, None)
            return True

    def _drop_job(self, decision_id: str, reason: str) -> None:
        with self._lock:
            job = self._jobs.pop(decision_id, None)
        if job is not None:
            self._kill_later(job, reason)

    def _abandon(self, job: dict[str, Any]) -> None:
        decision_id, job_id = str(job.get("decision_id") or ""), str(job.get("job_id") or "")
        with self._lock:
            if self._jobs.get(decision_id) is job:
                self._jobs.pop(decision_id, None)
            if job_id:
                self._abandoned[job_id] = job

    def _take_jobs(self, goal_id: str | None) -> list[dict[str, Any]]:
        with self._lock:
            if goal_id is None:
                jobs = list(self._jobs.values())
                self._jobs.clear()
                return jobs
            taken = [job for job in self._jobs.values() if str(job.get("goal_id") or "") == goal_id]
            self._jobs = {key: job for key, job in self._jobs.items() if str(job.get("goal_id") or "") != goal_id}
            return taken

    def _mark_stopped(self, job: Mapping[str, Any], reason: str) -> None:
        self._record_event(
            str(job.get("goal_id") or ""), str(job.get("decision_id") or ""), _STOPPED,
            request_id=job.get("request_id"), job_id=job.get("job_id"), reason=str(reason or "")[:200],
        )

    def _patch_processes(self, job: Mapping[str, Any], procs: list[Any]) -> None:
        goal_id, decision_id = str(job.get("goal_id") or ""), str(job.get("decision_id") or "")
        job_id = str(job.get("job_id") or "")
        pending = self._pending(goal_id, decision_id)
        if pending is None:
            return
        lead = self._lead_state(pending)
        for item in reversed(lead.get("events") or []):
            if isinstance(item, dict) and item.get("event") == _STOPPED and str(item.get("job_id") or "") == job_id:
                item["lead_processes"] = _sanitize_processes(procs)
                _bound_event(item)
                self._write(goal_id, decision_id, lead)
                return

    def _cancel_wait(self, job: Mapping[str, Any], reason: str, timeout: float) -> list[Any]:
        scope, rows = job.get("scope"), []
        if isinstance(scope, CancelScope):
            try:
                rows = list(scope.cancel(str(reason or "cancelled")) or [])
            except Exception:
                rows = []
        done = job.get("done")
        if isinstance(done, threading.Event):
            done.wait(max(0.0, float(timeout)))
        return rows

    def _kill_later(self, job: Mapping[str, Any], reason: str) -> None:
        def _run() -> None:
            try:
                self._patch_processes(job, self._cancel_wait(job, reason, 3.0))
            except Exception:
                _LOG.info("lead_processes patch failed")

        thread = threading.Thread(target=_run, name=f"collab-review-stop-{job.get('job_id')}", daemon=True)
        with self._lock:
            self._prune_stoppers_locked()
            self._stoppers.append(thread)
            thread.start()

    def _prune_stoppers_locked(self) -> None:
        self._stoppers = [thread for thread in self._stoppers if thread.is_alive()]

    def _join_stoppers(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                self._prune_stoppers_locked()
                pending = list(self._stoppers)
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            pending[0].join(remaining)


__all__ = ["LeadReviewRunner"]
