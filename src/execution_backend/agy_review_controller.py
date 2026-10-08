"""Lead-review state for the agy CLI backend.

The backend keeps thin hooks. This controller owns the review lock, the
listener list, the store, and the run-record transitions. It calls back
into the backend only through methods that already exist there.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from execution_backend.agy_account_pool import (
    ENV_POOL,
    ENV_PROFILE,
    AccountPoolError,
    AccountSwitchLockTimeout,
    account_switch_lock,
)
from execution_backend.agy_review import (
    build_payload,
    build_rework_prompt,
    context_hash,
    max_redos_from_charter,
    normalize_verdict,
    parse_request_id,
    project_outcome,
    read_tool_evidence,
    remaining_sec,
    review_required,
    snapshot_artifacts,
)
from execution_backend.base import BackendError, BackendStatus, unsupported

_LOG = logging.getLogger(__name__)

_PARKED_REVIEW_STATES = frozenset({
    "awaiting_review",
    "accepted",
    "rejected_final",
    "review_unavailable",
    "interrupted_worker",
    "worker_failed",
})


class AgyReviewController:
    """Review state machine parked on an ``AntigravityCliExecutionBackend``."""

    def __init__(self, backend: Any, *, store: Any) -> None:
        self._backend = backend
        self._store = store
        self._listeners: list[Any] = []
        self._lock = threading.RLock()

    def enable(self) -> None:
        self._backend._lead_review = True

    @property
    def mode(self) -> str:
        return "async_v1" if self._backend._lead_review else ""

    def add_listener(self, cb: Any) -> None:
        with self._lock:
            self._listeners.append(cb)

    def restore(self) -> None:
        try:
            self._restore_review_runs()
        except Exception as exc:  # noqa: BLE001 — startup must not crash on a bad review store
            _LOG.warning("agy review restore failed (%s)", type(exc).__name__)

    def arm(self, rec: dict[str, Any], charter: dict | None, prompt: str) -> None:
        if review_required(charter, enabled=bool(self._backend._lead_review)):
            self._arm_lead_review(rec, charter, prompt)

    def on_exit(self, rec: dict[str, Any]) -> None:
        if isinstance(rec.get("review"), dict):
            self._review_on_exit(rec)

    def override_awaiting(self, out: dict[str, Any], rec: Mapping[str, Any]) -> None:
        """awaiting_review stays busy so the coordinator does not collect early."""
        review = rec.get("review") if isinstance(rec.get("review"), dict) else None
        if review and review.get("state") == "awaiting_review":
            out["activity"] = "awaiting_review"
            out["busy"] = True
            out["finish"] = None
            out["finish_successful"] = False
            out["errored"] = False

    def hold(self) -> Any:
        """The review lock. ``collect_result`` holds it across pool release and project."""
        return self._lock

    def pool_pending(self, rec: Mapping[str, Any]) -> bool:
        """True when this round has not yet released the account-pool lease."""
        review = rec.get("review") if isinstance(rec.get("review"), dict) else None
        if not isinstance(review, dict):
            return True
        return review.get("pool_done_round") != review.get("round")

    def project(self, out: dict[str, Any], rec: dict[str, Any]) -> None:
        """Project the review onto ``out`` and mark the store record collected.

        Caller holds ``hold()``. A round whose lease was already released keeps
        the stored ``agy_err_class`` instead of classifying again.
        """
        review = rec.get("review") if isinstance(rec.get("review"), dict) else None
        if not isinstance(review, dict):
            return
        if review.get("pool_done_round") == review.get("round") and review.get("agy_err_class") is not None:
            out["agy_err_class"] = review.get("agy_err_class")
        project_outcome(out, review)
        review["collected"] = True
        try:
            self._store.update(
                str(rec.get("run_id") or ""),
                collected=True,
                updated_at=time.time(),
            )
        except KeyError:
            pass

    def iter_records(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Distinct run records, optionally one session (run id, handle, or conversation)."""
        seen: set[int] = set()
        chosen: list[dict[str, Any]] = []
        for rec in self._backend._runs.values():
            if not isinstance(rec, dict) or id(rec) in seen:
                continue
            seen.add(id(rec))
            if session_id is not None:
                sid = str(session_id)
                if sid not in (
                    str(rec.get("run_id") or ""),
                    str(rec.get("native_handle") or ""),
                    str(rec.get("conversation_id") or ""),
                ):
                    continue
            chosen.append(rec)
        return chosen

    def pending_actions(self, session_id: str | None = None) -> tuple[int, list]:
        rows: list[dict[str, Any]] = []
        for rec in self.iter_records(session_id):
            self._backend._refresh(rec)
            self._review_on_exit(rec)
            row = self._review_pending_row(rec)
            if row is not None:
                rows.append(row)
        return 200, rows

    def cancel_run(self, rec: dict[str, Any]) -> tuple[int, Any] | None:
        """Cancel a reviewed run. None when ``rec`` has no review.

        The branch is chosen under ``self._lock``, re-reading state and ``proc``
        there. A live review proc is killed and reaped before the account lease
        is released, still holding the lock. ``rework_waiting`` does not release
        a lease. Parked states do, after the re-check.

        Lock order while this lock is held: review-store, session, run-registry,
        then the account-switch file lock inside ``_release_pool_lease_after_cancel``.
        None of those acquire this lock. ``start_run`` drops the account-switch
        lock before ``arm`` takes this one. ``collect_result`` and rework spawn
        already take this lock first, then the account-switch lock.
        """
        with self._lock:
            review = rec.get("review") if isinstance(rec.get("review"), dict) else None
            if review is None:
                return None
            if self._proc_live(rec):
                return self._cancel_live_review_locked(rec, review)
            state = review.get("state")
            if state == "rework_waiting":
                self._stamp_cancelled(rec, review)
                self._forget_review(rec)
                return self._cancelled_body(rec)
            if state in _PARKED_REVIEW_STATES:
                self._stamp_cancelled(rec, review)
                self._forget_review(rec)
                self._backend._harvest(rec)
                self._backend._release_pool_lease_after_cancel(rec)
                return self._cancelled_body(rec)
            return None

    def _cancel_live_review_locked(self, rec: dict[str, Any], review: dict[str, Any]) -> tuple[int, Any]:
        """Caller holds ``self._lock``. Kill and wait for exit, then release."""
        proc = rec.get("proc")
        killed = bool(proc is not None and self._backend._kill_proc(proc, rec))
        if not killed or proc is None or proc.poll() is None:
            pid = getattr(proc, "pid", None)
            return 501, unsupported("cancel", f"could not signal agy pid={pid}")
        self._stamp_cancelled(rec, review)
        self._backend._harvest(rec)
        self._backend._release_pool_lease_after_cancel(rec)
        self._forget_review(rec)
        return self._cancelled_body(rec)

    @staticmethod
    def _stamp_cancelled(rec: dict[str, Any], review: dict[str, Any]) -> None:
        review["state"] = "cancelled"
        rec["cancelled"] = True
        rec["finish"] = "cancelled"
        rec["activity"] = "idle"
        rec["state"] = "cancelled"

    @staticmethod
    def _cancelled_body(rec: Mapping[str, Any]) -> tuple[int, Any]:
        return 200, {"ok": True, "run_id": rec["run_id"], "state": "cancelled"}

    def cancel_waiting(self, rec: dict[str, Any]) -> bool:
        """Cancel a rework that holds no account lease. False if this is not that case.

        Caller must not release a pool lease: none was taken.
        """
        review = rec.get("review") if isinstance(rec.get("review"), dict) else None
        if review is None or review.get("state") != "rework_waiting" or self._proc_live(rec):
            return False
        with self._lock:
            review = rec.get("review") if isinstance(rec.get("review"), dict) else None
            if review is None or review.get("state") != "rework_waiting" or self._proc_live(rec):
                return False
            self._stamp_cancelled(rec, review)
            self._forget_review(rec)
        return True

    def retry_waiting(self, rec: dict[str, Any]) -> None:
        """Spawn a deferred rework once an account is free. No-op while a proc is live."""
        with self._lock:
            review = rec.get("review") if isinstance(rec.get("review"), dict) else None
            if review is None or review.get("state") != "rework_waiting" or self._proc_live(rec):
                return
            try:
                left = remaining_sec(review.get("deadline"), time.time())
            except (TypeError, ValueError):
                left = 0.0
            if left < 30:
                review["state"] = "rejected_final"
                review["skip_reason"] = "rework skipped: no free agy account before deadline"
                rec["activity"] = "idle"
                rec["finish"] = "stop"
                rec["state"] = "succeeded"
                rec["harvested"] = True
                self._persist_review(rec)
                return
            pending = review.get("pending_rework") if isinstance(review.get("pending_rework"), dict) else {}
            reason = str(pending.get("reason") or "")
            previous = pending.get("previous")
            if not isinstance(previous, list):
                previous = []
            review["state"] = "running"
            review["request_id"] = None
            self._persist_review(rec)
            self._spawn_or_wait(rec, review, reason, previous)

    def cancel_parked(self, rec: dict[str, Any]) -> bool:
        """Parked review with no live process becomes cancelled. False if not parked.

        Re-checks under the lock. A rework that started since the caller looked
        is left alone (``cancel_run`` kills that proc before releasing a lease).
        """
        with self._lock:
            proc = rec.get("proc")
            review = rec.get("review") if isinstance(rec.get("review"), dict) else None
            if not (
                review is not None
                and review.get("state") in _PARKED_REVIEW_STATES
                and (proc is None or proc.poll() is not None)
            ):
                return False
            self._stamp_cancelled(rec, review)
            self._forget_review(rec)
        return True

    def cancel_live(self, rec: dict[str, Any]) -> None:
        """A killed live worker drops its review record."""
        if isinstance(rec.get("review"), dict):
            with self._lock:
                rec["review"]["state"] = "cancelled"
                self._forget_review(rec)


    def resolve_decision(
        self,
        request_id: str,
        *,
        verdict: str,
        reason: str = "",
        answers: Any = None,
    ) -> dict[str, Any]:
        """Apply one lead verdict. ValueError leaves the run and the store untouched."""
        parsed = parse_request_id(request_id if isinstance(request_id, str) else "")
        if parsed is None:
            raise BackendError(
                BackendStatus.FAILED,
                f"not an agy review request: {request_id}",
                capability="resolve_decision",
            )
        run_id, req_round = parsed
        rec = self._backend._runs.get(run_id)
        if rec is None or not isinstance(rec.get("review"), dict):
            raise BackendError(
                BackendStatus.FAILED,
                f"unknown agy review run for {request_id}",
                capability="resolve_decision",
            )
        normalized = normalize_verdict(verdict)
        listeners: list[Any] | None = None
        result: dict[str, Any] | None = None
        with self._lock:
            review = rec.get("review")
            if not isinstance(review, dict):
                raise BackendError(
                    BackendStatus.FAILED,
                    f"unknown agy review run for {request_id}",
                    capability="resolve_decision",
                )
            resolutions = review.get("resolutions")
            if not isinstance(resolutions, dict):
                resolutions = {}
            if request_id in resolutions:
                prev = resolutions[request_id]
                previous = prev.get("verdict") if isinstance(prev, dict) else None
                if previous == normalized:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "request_id": request_id,
                        "kind": "review",
                        "controller_state": review.get("state"),
                    }
                raise ValueError(f"review request already resolved as {previous}")
            current_round = review.get("round")
            current_state = review.get("state")
            try:
                round_ok = int(current_round) == int(req_round)
            except (TypeError, ValueError):
                round_ok = False
            if not round_ok or current_state != "awaiting_review":
                raise ValueError(
                    f"stale review request {request_id}: current round r{current_round}, "
                    f"state {current_state}"
                )
            by = self._review_actor(answers)
            reason_text = "" if reason is None else str(reason)
            if len(reason_text) > 2000:
                reason_text = reason_text[:2000]
            now = time.time()
            history = review.get("history")
            if not isinstance(history, list):
                history = []
            prior_fails = [
                str(item.get("reason") or "")
                for item in history
                if isinstance(item, dict) and item.get("verdict") == "fail"
            ]
            resolutions[request_id] = {
                "verdict": normalized,
                "reason": reason_text,
                "at": now,
                "by": by,
            }
            history.append({
                "round": int(req_round),
                "verdict": normalized,
                "reason": reason_text,
                "by": by,
                "conversation_id": str(rec.get("conversation_id") or ""),
                "at": now,
            })
            review["resolutions"] = resolutions
            review["history"] = history
            if normalized == "pass":
                self._apply_pass_locked(rec, review)
            elif normalized == "fail":
                self._apply_fail_locked(rec, review, reason_text, prior_fails, now, req_round)
            else:
                review["state"] = "review_unavailable"
                review["unavailable_reason"] = reason_text
                review["error_source"] = None
                self._persist_review(rec)
            result = {
                "ok": True,
                "request_id": request_id,
                "kind": "review",
                "controller_state": review.get("state"),
                "round": int(review.get("round") or req_round),
            }
            listeners = list(self._listeners)
        self._notify_review_listeners(
            listeners or [], request_id, normalized, str((result or {}).get("controller_state") or "")
        )
        return result or {"ok": True, "request_id": request_id, "kind": "review"}

    def review_outcome(self, request_id: str) -> dict[str, Any] | None:
        """Recorded verdict for an agy review id, or None. Never raises."""
        try:
            parsed = parse_request_id(request_id if isinstance(request_id, str) else "")
        except Exception:  # noqa: BLE001
            return None
        if parsed is None:
            return None
        run_id, _round = parsed
        try:
            with self._lock:
                rec = self._backend._runs.get(run_id)
                if rec is None or not isinstance(rec.get("review"), dict):
                    return None
                review = rec["review"]
                resolutions = review.get("resolutions")
                row = resolutions.get(request_id) if isinstance(resolutions, dict) else None
                if not isinstance(row, dict):
                    return None
                return {
                    "verdict": row.get("verdict"),
                    "reason": row.get("reason"),
                    "by": row.get("by"),
                    "controller_state": review.get("state"),
                }
        except Exception:  # noqa: BLE001 — outcome lookup is observational
            return None

    def _apply_pass_locked(self, rec: dict[str, Any], review: dict[str, Any]) -> None:
        snap = snapshot_artifacts(Path(str(rec.get("directory") or "")), list(rec.get("artifacts") or []))
        changed = bool(snap.get("missing")) or snap.get("artifact_hash") != review.get("artifact_hash")
        if changed:
            review["state"] = "review_unavailable"
            review["unavailable_reason"] = "lead_review_unavailable: artifacts_changed_after_review"
            review["error_source"] = None
        else:
            review["state"] = "accepted"
        self._persist_review(rec)

    def _apply_fail_locked(
        self,
        rec: dict[str, Any],
        review: dict[str, Any],
        reason_text: str,
        prior_fails: list[str],
        now: float,
        req_round: int,
    ) -> None:
        redos = int(review.get("redos") or 0)
        max_redos = int(review.get("max_redos") or 0)
        try:
            left = remaining_sec(review.get("deadline"), now)
        except (TypeError, ValueError):
            left = 0.0
        if redos < max_redos and left >= 30:
            review["redos"] = redos + 1
            review["round"] = int(req_round) + 1
            review["state"] = "running"
            review["request_id"] = None
            self._persist_review(rec)
            self._spawn_or_wait(rec, review, reason_text, prior_fails, now=now)
            return
        if redos >= max_redos:
            review["skip_reason"] = f"rework budget exhausted ({redos}/{max_redos})"
        else:
            review["skip_reason"] = f"rework skipped: {int(left)}s left (<30s)"
        review["state"] = "rejected_final"
        self._persist_review(rec)

    def _spawn_rework(
        self,
        rec: dict[str, Any],
        review: dict[str, Any],
        reason_text: str,
        prior_fails: list[str],
    ) -> str | None:
        """Next round under the account-switch lock. None on success, else a short error.

        Caller holds ``_review_lock`` and has already persisted ``state=running``.
        """
        from execution_backend.antigravity_cli_v1 import build_agy_argv

        base_env = self._backend._base_env()
        lock_pool = self._backend._account_pool_path or str(base_env.get(ENV_POOL) or "").strip()
        prompt = build_rework_prompt(
            str(review.get("base_prompt") or ""),
            k=int(review.get("redos") or 0),
            n=int(review.get("max_redos") or 0),
            reason=reason_text,
            previous=prior_fails,
        )
        spawn_env: dict[str, str] | None = None
        try:
            with (account_switch_lock(lock_pool, environ=base_env) if lock_pool else nullcontext()):
                spawn_env = self._backend._bind_pool_environ_for_dispatch()
                cmd = build_agy_argv(
                    bin_path=self._backend.bin_path,
                    model=str(rec.get("model") or ""),
                    prompt=prompt,
                    skip_permissions=bool(rec.get("skip_permissions")),
                )
                self._reset_rec_for_round(rec, review, spawn_env, cmd)
                failed = self._backend._spawn_locked(rec, cmd, spawn_env, list(rec.get("path_errors") or []))
                if failed is not None:
                    return self._one_line(str(rec.get("assistant_error") or "spawn failed"))
                proc = rec.get("proc")
                if proc is None:
                    return "spawn failed"
                rec["pid"] = getattr(proc, "pid", None)
                if self._backend._registry is not None:
                    try:
                        self._backend._registry.record(
                            str(rec.get("run_id") or ""),
                            pid=int(proc.pid),
                            directory=str(rec.get("directory") or ""),
                        )
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("agy run registry record failed (%s)", type(exc).__name__)
                self._backend._start_pipe_drainers(rec, proc)
                return None
        except Exception as exc:  # noqa: BLE001 — a failed rework is review_unavailable, not a raise
            if spawn_env is not None:
                self._backend._release_pool_after_failed_spawn(spawn_env)
            # No free account, before Popen: caller waits. A lock timeout is still a spawn failure.
            # Any lease already taken was released above.
            if isinstance(exc, AccountPoolError) and not isinstance(exc, AccountSwitchLockTimeout):
                raise
            return self._one_line(str(exc) or type(exc).__name__)

    def _spawn_or_wait(
        self,
        rec: dict[str, Any],
        review: dict[str, Any],
        reason_text: str,
        prior_fails: list[str],
        *,
        now: float | None = None,
    ) -> None:
        """Spawn the counted rework. A busy pool waits; any other error fails the review."""
        try:
            err = self._spawn_rework(rec, review, reason_text, prior_fails)
        except AccountPoolError:
            review["state"] = "rework_waiting"
            if not isinstance(review.get("waiting_since"), (int, float)) or isinstance(review.get("waiting_since"), bool):
                review["waiting_since"] = time.time() if now is None else now
            review["pending_rework"] = {"reason": reason_text, "previous": [str(item) for item in prior_fails]}
            rec["state"] = "running"
            rec["activity"] = "waiting_account"
            rec["harvested"] = True
            self._persist_review(rec)
            return
        if not err:
            return
        review["state"] = "review_unavailable"
        review["unavailable_reason"] = f"lead_review_unavailable: rework_spawn_failed: {err}"
        review["error_source"] = "spawn"
        rec["state"] = "failed"
        rec["activity"] = "idle"
        rec["finish"] = "error"
        rec["harvested"] = True
        rec["assistant_error"] = review["unavailable_reason"]
        self._persist_review(rec)

    def _reset_rec_for_round(
        self,
        rec: dict[str, Any],
        review: dict[str, Any],
        spawn_env: Mapping[str, str],
        cmd: list[str],
    ) -> None:
        from execution_backend.antigravity_cli_v1 import _redact_argv

        started = rec.get("started_at")
        try:
            # _refresh compares now-started_at with timeout_sec. started_at stays round 1,
            # so the span is the original deadline offset, not the time still left.
            timeout = float(review.get("deadline")) - float(started)
        except (TypeError, ValueError):
            timeout = float(rec.get("timeout_sec") or 0)
        if timeout < 0:
            timeout = 0.0
        profile = str(spawn_env.get(ENV_PROFILE) or "").strip()
        rec["proc"] = None
        rec["harvested"] = False
        rec["stdout"] = ""
        rec["stderr"] = ""
        rec["returncode"] = None
        rec["agy_json"] = None
        rec["usage"] = None
        rec["response"] = ""
        rec["finish"] = None
        rec["assistant_error"] = ""
        rec["timed_out"] = False
        rec["cancelled"] = False
        rec["state"] = "running"
        rec["activity"] = "busy"
        rec["spawn_environ"] = dict(spawn_env)
        rec["agy_profile"] = profile
        rec["argv_flags"] = _redact_argv(list(cmd))
        rec["timeout_sec"] = timeout
        rec["_session_baseline"] = self._backend._session.baseline(spawn_env)
        rec["stdout_chunks"] = []
        rec["stderr_chunks"] = []
        rec.pop("error_source", None)
        rec["_children_seen"] = []

    def _arm_lead_review(self, rec: dict[str, Any], charter: dict | None, prompt: str) -> None:
        started = float(rec.get("started_at") or time.time())
        try:
            timeout = float(rec.get("timeout_sec") or 0)
        except (TypeError, ValueError):
            timeout = 0.0
        review = {
            "state": "running",
            "round": 1,
            "redos": 0,
            "max_redos": max_redos_from_charter(charter if isinstance(charter, dict) else None),
            "base_prompt": prompt,
            "deadline": started + timeout,
            "acceptance_text": self._acceptance_text(charter),
            "history": [],
            "usage_rounds": [],
            "resolutions": {},
            "request_id": None,
            "pool_done_round": None,
            "collected": False,
            "artifact_hash": None,
            "payload": None,
            "unavailable_reason": None,
            "error_source": None,
            "last_result": None,
            "agy_err_class": None,
            "skip_reason": None,
            "context_hash": None,
        }
        with self._lock:
            rec["review"] = review
            self._store.put(str(rec.get("run_id") or ""), self._review_store_record(rec))

    def _review_on_exit(self, rec: dict[str, Any]) -> None:
        """running + harvested exit → awaiting_review or worker_failed. Idempotent."""
        with self._lock:
            review = rec.get("review")
            if not isinstance(review, dict) or review.get("state") != "running":
                return
            if not rec.get("harvested"):
                return
            proc = rec.get("proc")
            if proc is not None and callable(getattr(proc, "poll", None)) and proc.poll() is None:
                return
            missing = self._missing_artifact_names(rec)
            clean = (
                rec.get("state") == "succeeded"
                and rec.get("finish") == "stop"
                and not rec.get("assistant_error")
                and not rec.get("timed_out")
                and not rec.get("cancelled")
                and not missing
            )
            snap = None
            if clean:
                snap = snapshot_artifacts(
                    Path(str(rec.get("directory") or "")),
                    list(rec.get("artifacts") or []),
                )
                if snap.get("missing"):
                    clean = False
            rounds = review.get("usage_rounds")
            if not isinstance(rounds, list):
                rounds = []
                review["usage_rounds"] = rounds
            rounds.append(self._copy_usage(rec.get("usage")))
            response = rec.get("response")
            excerpt = response if isinstance(response, str) else ("" if response is None else str(response))
            review["last_result"] = {
                "error": str(rec.get("assistant_error") or "")[:2000],
                "returncode": rec.get("returncode"),
                "response_excerpt": excerpt[:2000],
                "conversation_id": str(rec.get("conversation_id") or ""),
            }
            if not clean or snap is None:
                review["state"] = "worker_failed"
                self._persist_review(rec)
                return
            tmp = {
                "ok": True,
                "error": "",
                "stdout": rec.get("stdout") or "",
                "stderr": rec.get("stderr") or "",
                "returncode": rec.get("returncode"),
            }
            self._backend._persist_pool_after_collect(tmp, rec)
            review["pool_done_round"] = review.get("round")
            if tmp.get("agy_err_class") is not None:
                review["agy_err_class"] = tmp.get("agy_err_class")
            payload = build_payload(
                review,
                snap,
                acceptance_text=str(review.get("acceptance_text") or ""),
                response_excerpt=excerpt[:2000],
                tools=read_tool_evidence(
                    rec.get("spawn_environ"),
                    str(rec.get("conversation_id") or ""),
                ),
            )
            rnd = int(review.get("round") or 1)
            review["request_id"] = f"agyrev:{rec.get('run_id')}:r{rnd}"
            review["artifact_hash"] = snap.get("artifact_hash")
            review["payload"] = payload
            review["context_hash"] = context_hash(str(snap.get("artifact_hash") or ""), rnd)
            review["state"] = "awaiting_review"
            self._persist_review(rec)

    def _review_pending_row(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            review = rec.get("review")
            if not isinstance(review, dict) or review.get("state") != "awaiting_review":
                return None
            if not self._backend._lead_review:
                review["state"] = "review_unavailable"
                review["unavailable_reason"] = (
                    "lead_review_unavailable: lead_review_disabled after 1 attempt(s)"
                )
                review["error_source"] = None
                self._persist_review(rec)
                return None
            payload = review.get("payload") if isinstance(review.get("payload"), dict) else {}
            art = str(payload.get("artifact_hash") or review.get("artifact_hash") or "")
            try:
                rnd = int(review.get("round") or 1)
            except (TypeError, ValueError):
                rnd = 1
            return {
                "request_id": review.get("request_id"),
                "kind": "review",
                "payload": payload,
                "context_hash": context_hash(art, rnd),
            }

    def _has_review_rec(self) -> bool:
        for rec in self._backend._runs.values():
            if isinstance(rec, dict) and isinstance(rec.get("review"), dict):
                return True
        return False

    def _missing_artifact_names(self, rec: Mapping[str, Any]) -> list[str]:
        missing: list[str] = []
        root = Path(str(rec.get("directory") or ""))
        for name in rec.get("artifacts") or []:
            path = Path(str(name))
            if not path.is_absolute():
                path = root / str(name)
            if not path.is_file():
                missing.append(str(name))
        return missing

    def _persist_review(self, rec: Mapping[str, Any]) -> None:
        run_id = str(rec.get("run_id") or "")
        if not run_id or not isinstance(rec.get("review"), dict):
            return
        record = self._review_store_record(rec)
        fields = {key: value for key, value in record.items() if key != "run_id"}
        try:
            self._store.update(run_id, **fields)
        except KeyError:
            self._store.put(run_id, record)

    def _forget_review(self, rec: Mapping[str, Any]) -> None:
        run_id = str(rec.get("run_id") or "")
        if not run_id:
            return
        try:
            self._store.forget(run_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("agy review forget failed (%s)", type(exc).__name__)

    def _review_store_record(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        review = rec.get("review") if isinstance(rec.get("review"), dict) else {}
        last = review.get("last_result") if isinstance(review.get("last_result"), dict) else None
        return {
            "run_id": rec.get("run_id"),
            "native_handle": rec.get("native_handle"),
            "directory": rec.get("directory"),
            "artifacts": list(rec.get("artifacts") or []),
            "title": rec.get("title") or "",
            "base_prompt": review.get("base_prompt") or "",
            "model": rec.get("model") or "",
            "skip_permissions": bool(rec.get("skip_permissions")),
            "timeout_sec": rec.get("timeout_sec"),
            "started_at": rec.get("started_at"),
            "deadline": review.get("deadline"),
            "max_redos": review.get("max_redos"),
            "redos": review.get("redos"),
            "round": review.get("round"),
            "state": review.get("state"),
            "request_id": review.get("request_id"),
            "artifact_hash": review.get("artifact_hash"),
            "payload": review.get("payload"),
            "history": list(review.get("history") or []),
            "usage_rounds": list(review.get("usage_rounds") or []),
            "resolutions": dict(review.get("resolutions") or {}),
            "unavailable_reason": review.get("unavailable_reason"),
            "error_source": review.get("error_source"),
            "last_result": last,
            "collected": bool(review.get("collected")),
            "updated_at": time.time(),
            "acceptance_text": review.get("acceptance_text") or "",
            "pool_done_round": review.get("pool_done_round"),
            "agy_err_class": review.get("agy_err_class"),
            "skip_reason": review.get("skip_reason"),
            "context_hash": review.get("context_hash"),
            "conversation_id": rec.get("conversation_id") or "",
            "returncode": rec.get("returncode"),
            "response": rec.get("response") if isinstance(rec.get("response"), str) else "",
            "contract_sha256": rec.get("contract_sha256") or "",
            "contract_fields": list(rec.get("contract_fields") or []),
            "waiting_since": review.get("waiting_since"),
            "pending_rework": review.get("pending_rework") if isinstance(review.get("pending_rework"), dict) else None,
        }

    def _restore_review_runs(self) -> None:
        self._store.prune()
        rows = self._store.load()
        for run_id, saved in rows.items():
            if not isinstance(saved, dict) or saved.get("collected") is True:
                continue
            state = saved.get("state")
            if state == "running":
                try:
                    self._store.update(
                        run_id, state="interrupted_worker", updated_at=time.time()
                    )
                except KeyError:
                    pass
                saved = dict(saved)
                saved["state"] = "interrupted_worker"
            elif state not in {
                "awaiting_review",
                "accepted",
                "rejected_final",
                "review_unavailable",
                "interrupted_worker",
                "worker_failed",
                "rework_waiting",
            }:
                continue
            rec = self._rec_from_review_store(saved)
            if not rec.get("run_id"):
                continue
            self._backend._runs[str(rec["run_id"])] = rec
            handle = rec.get("native_handle")
            if handle:
                self._backend._runs[str(handle)] = rec
            conv = str(rec.get("conversation_id") or "")
            if conv:
                self._backend._runs[conv] = rec

    def _rec_from_review_store(self, saved: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(saved.get("run_id") or "")
        handle = str(saved.get("native_handle") or (f"agy_native_{run_id}" if run_id else ""))
        last = saved.get("last_result") if isinstance(saved.get("last_result"), dict) else {}
        review = {
            "state": saved.get("state"),
            "round": saved.get("round") if saved.get("round") is not None else 1,
            "redos": saved.get("redos") if saved.get("redos") is not None else 0,
            "max_redos": saved.get("max_redos") if saved.get("max_redos") is not None else 1,
            "base_prompt": saved.get("base_prompt") or "",
            "deadline": saved.get("deadline"),
            "acceptance_text": saved.get("acceptance_text") or "",
            "history": list(saved.get("history") or []),
            "usage_rounds": list(saved.get("usage_rounds") or []),
            "resolutions": dict(saved.get("resolutions") or {}),
            "request_id": saved.get("request_id"),
            "pool_done_round": saved.get("pool_done_round"),
            "collected": bool(saved.get("collected")),
            "artifact_hash": saved.get("artifact_hash"),
            "payload": saved.get("payload") if isinstance(saved.get("payload"), dict) else None,
            "unavailable_reason": saved.get("unavailable_reason"),
            "error_source": saved.get("error_source"),
            "last_result": last or None,
            "agy_err_class": saved.get("agy_err_class"),
            "skip_reason": saved.get("skip_reason"),
            "context_hash": saved.get("context_hash"),
            "waiting_since": saved.get("waiting_since"),
            "pending_rework": (
                dict(saved["pending_rework"]) if isinstance(saved.get("pending_rework"), dict) else None
            ),
        }
        if not review["acceptance_text"] and isinstance(review["payload"], dict):
            review["acceptance_text"] = str(review["payload"].get("acceptance_text") or "")
        state = str(saved.get("state") or "")
        interrupted = self._interrupted_text(saved)
        if state == "interrupted_worker":
            finish, run_state, activity = "error", "failed", "idle"
            assistant_error = interrupted
        elif state == "worker_failed":
            rc = last.get("returncode")
            err = str(last.get("error") or "")
            if err or rc not in (0, None):
                finish, run_state, activity = "error", "failed", "idle"
                assistant_error = err
            else:
                finish, run_state, activity = "stop", "succeeded", "idle"
                assistant_error = ""
        elif state == "awaiting_review":
            finish, run_state, activity = "stop", "succeeded", "idle"
            assistant_error = ""
        elif state == "rework_waiting":
            finish, run_state, activity = "stop", "running", "waiting_account"
            assistant_error = ""
        elif state == "accepted":
            finish, run_state, activity = "stop", "succeeded", "idle"
            assistant_error = ""
        else:
            finish, run_state, activity = "stop", "succeeded", "idle"
            assistant_error = ""
        return {
            "run_id": run_id,
            "native_handle": handle,
            "conversation_id": str(saved.get("conversation_id") or last.get("conversation_id") or ""),
            "title": saved.get("title") or "",
            "directory": saved.get("directory") or "",
            "instruction": "",
            "artifacts": list(saved.get("artifacts") or []),
            "started_at": saved.get("started_at"),
            "activity": activity,
            "finish": finish,
            "assistant_error": assistant_error,
            "cancelled": False,
            "timed_out": False,
            "state": run_state,
            "skip_permissions": bool(saved.get("skip_permissions")),
            "argv_flags": [],
            "model": saved.get("model") or "",
            "bin_path": self._backend.bin_path,
            "timeout_sec": saved.get("timeout_sec"),
            "proc": None,
            "harvested": True,
            "stdout": "",
            "stderr": "",
            "returncode": saved.get("returncode", last.get("returncode")),
            "agy_json": None,
            "usage": None,
            "response": str(saved.get("response") or last.get("response_excerpt") or ""),
            "path_errors": [],
            "agy_profile": "",
            "contract_sha256": str(saved.get("contract_sha256") or ""),
            "contract_fields": list(saved.get("contract_fields") or []),
            "review": review,
            "restored": True,
        }

    @staticmethod
    def _interrupted_text(saved: Mapping[str, Any]) -> str:
        rnd = saved.get("round") if saved.get("round") is not None else 1
        redos = saved.get("redos") if saved.get("redos") is not None else 0
        max_redos = saved.get("max_redos") if saved.get("max_redos") is not None else 0
        return (
            f"lead_review_interrupted: round {rnd} worker lost in "
            f"service restart; used reworks {redos}/{max_redos} are kept"
        )

    @staticmethod
    def _acceptance_text(charter: dict | None) -> str:
        if not isinstance(charter, dict):
            return ""
        acc = charter.get("acceptance")
        if isinstance(acc, str):
            return acc
        if acc is None:
            return ""
        try:
            return json.dumps(acc, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(acc)

    @staticmethod
    def _review_actor(answers: Any) -> str:
        if isinstance(answers, list) and answers:
            first = answers[0]
            if isinstance(first, dict):
                by = first.get("by")
                if isinstance(by, str) and by.strip():
                    return by.strip()
        return "api"

    @staticmethod
    def _copy_usage(usage: Any) -> Any:
        if not isinstance(usage, dict):
            return None
        copied: dict[str, Any] = {}
        for key, value in usage.items():
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                copied[str(key)] = value
            elif value is None or isinstance(value, bool):
                copied[str(key)] = value
        return copied

    @staticmethod
    def _proc_live(rec: Mapping[str, Any]) -> bool:
        proc = rec.get("proc")
        return proc is not None and callable(getattr(proc, "poll", None)) and proc.poll() is None

    @staticmethod
    def _one_line(msg: str, limit: int = 300) -> str:
        text = " ".join(str(msg or "").split())
        return (text[:limit] or "spawn failed")

    def _notify_review_listeners(
        self,
        listeners: list[Any],
        request_id: str,
        verdict: str,
        state: str,
    ) -> None:
        for cb in listeners:
            try:
                cb(request_id, verdict, state)
            except Exception as exc:  # noqa: BLE001 — a listener must not break resolve
                _LOG.warning("agy review listener failed (%s)", type(exc).__name__)
