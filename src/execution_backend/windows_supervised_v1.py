"""ExecutionBackend bridge to the verified Windows supervision controller.

This keeps the old controller's safety properties (session-local ask policy,
durable request inbox, independent artifact review, cancellation confirmation,
and no ambiguous redispatch) while exposing it to the application Goal API.
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from execution_backend.base import (
    BackendError,
    BackendStatus,
    ExecutionBackendABC,
    default_capabilities,
)
from framework.artifact_handoff import HandoffError, copy_staged_inputs
from win_collab.client import Client, KEYS
from win_collab.core import TERMINAL, Engine, Store, external_directory_scope



def _need_human_fields(error: str) -> dict:
    raw = str(error or "").strip()
    if not raw.lower().startswith("need_human:"):
        return {"need_human": False}
    reason = " ".join(raw.split(":", 1)[-1].split())[:300]
    return {"need_human": True, "failure_reason": reason}


def _mirror_outputs_to_scheduler(job: Mapping[str, Any], root: Path, present: list[str]) -> None:
    """Copy declared outputs into the scheduler directory passed to start_run.

    TeleAgent keeps its own UUID workspace. Handoff only trusts the scheduler
    directory, so the accepted artifact path has to live there too. Failures
    are ignored: collect_result still reports the controller outcome.
    """
    raw = str(job.get("scheduler_directory") or "").strip()
    if not raw:
        return
    stage = Path(raw)
    try:
        if stage.resolve() == root.resolve():
            return
    except OSError:
        return
    names: list[str] = []
    for rel in [str(x) for x in (job.get("charter") or {}).get("artifacts", [])]:
        src = root / rel
        try:
            linked = src.is_symlink() or bool(hasattr(src, "is_junction") and src.is_junction())
        except OSError:
            continue
        if src.is_file() and not linked:
            names.append(rel)
    if not names:
        return
    try:
        copied = copy_staged_inputs(root, stage, names)
    except (HandoffError, OSError, ValueError):
        return
    for rel in copied:
        dest = stage / rel
        try:
            linked = dest.is_symlink() or bool(hasattr(dest, "is_junction") and dest.is_junction())
        except OSError:
            continue
        if dest.is_file() and not linked:
            text = str(dest)
            if text not in present:
                present.append(text)


class WindowsSupervisedExecutionBackend(ExecutionBackendABC):
    backend_id = "teleagent.windows.supervised_v1"
    capability_kind = "teleagent_windows"

    def capabilities(self) -> dict[str, Any]:
        """TeleAgent native permission/question/review. Not an OS sandbox.

        External reads go through the permission gate (hard reject until a
        lead or human decides). Decisions are stored on the controller
        (``access_audit=decision_log``). ``skip_permissions`` is never on.
        Jobs live in the controller Store: a new process with the same
        state_dir continues non-terminal jobs in ``Engine.tick`` (a changed
        TeleAgent instance fails closed; it does not silently redispatch).
        """
        caps = default_capabilities(backend_id=self.backend_id, kind=self.capability_kind)
        caps["channels"] = {"permission": True, "question": True, "review": True}
        caps["external_inputs"]["enforcement"] = "permission_gate"
        caps["isolation"]["os_sandbox"] = False
        caps["isolation"]["access_audit"] = "decision_log"
        caps["isolation"]["prompt_constraints"] = True
        caps["skip_permissions"] = False
        caps["resume"] = True
        caps["acceptance"]["artifact_presence"] = True
        caps["acceptance"]["exact_content"] = False
        caps["acceptance"]["lead_review"] = True
        caps["acceptance"]["executable_checks"] = False
        caps["usage"]["source"] = "unknown"
        caps["warnings"] = []
        return caps

    def __init__(
        self,
        *,
        state_dir: str | Path,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        max_parallel: int = 3,
        stdin_wrap: bool = False,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._client_factory = client_factory
        self.max_parallel = int(max_parallel)
        self.stdin_wrap = bool(stdin_wrap)
        self._wrap_handle: Any | None = None
        self._lock = threading.RLock()

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            elif self.stdin_wrap:
                from teleagent_adapter.windows_stdin_wrap import ensure_stdin_wrap

                handle = ensure_stdin_wrap()
                self._wrap_handle = handle
                self._client = Client(
                    base=handle.base_url,
                    creds={
                        KEYS[0]: handle.username,
                        KEYS[1]: handle.password,
                        KEYS[2]: handle.session_key,
                    },
                )
            else:
                self._client = Client()
        return self._client

    def close(self) -> None:
        if self._wrap_handle is not None:
            from teleagent_adapter.windows_stdin_wrap import stop_stdin_wrap

            stop_stdin_wrap(handle=self._wrap_handle)
            self._wrap_handle = None
        self._client = None

    @contextmanager
    def _engine(self) -> Iterator[tuple[Store, Engine]]:
        store = Store(self.state_dir)
        try:
            yield store, Engine(store, self._get_client(), max_parallel=self.max_parallel)
        finally:
            store.db.close()

    @staticmethod
    def _charter(
        *,
        title: str,
        instruction: str,
        artifacts: list[str],
        charter: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        src = dict(charter or {})
        done = src.get("done_when") if isinstance(src.get("done_when"), Mapping) else {}
        acceptance = src.get("acceptance") or done or {"artifacts": artifacts}
        if not isinstance(acceptance, str):
            acceptance = json.dumps(acceptance, ensure_ascii=False, sort_keys=True)
        timeout = src.get("timeout_sec") or ((src.get("budget") or {}).get("wall_sec") if isinstance(src.get("budget"), Mapping) else None)
        body: dict[str, Any] = {
            "name": title,
            "goal": str(src.get("goal") or instruction or title),
            "must": [str(x) for x in (src.get("must") or ["Work only in the assigned workspace"])],
            "must_not": [str(x) for x in (src.get("must_not") or [
                "Do not access credentials or account data",
                "Do not use the network",
                "Do not modify system settings",
            ])],
            "artifacts": [str(x) for x in artifacts],
            "acceptance": str(acceptance),
            "timeout_sec": max(10, min(7200, int(timeout or 900))),
            "max_lead_requests": max(1, min(100, int(src.get("max_lead_requests") or 12))),
            "max_redos": max(0, min(3, int(src.get("max_redos") if src.get("max_redos") is not None else 1))),
        }
        for key, env_name, default in (
            ("provider", "TELEAGENT_PROVIDER_ID", "NewApi"),
            ("model", "TELEAGENT_MODEL_ID", "chat-lite"),
            ("agent", "TELEAGENT_AGENT", "opencowork-default"),
        ):
            body[key] = str(src.get(key) or os.environ.get(env_name) or default)
        for key in ("forbidden_tools", "external_inputs", "min_approved_permissions", "allow_aigc_marks"):
            if key in src:
                body[key] = src[key]
        raw_inputs = src.get("input_files")
        if isinstance(raw_inputs, list) and raw_inputs:
            names: list[str] = []
            for item in raw_inputs:
                if isinstance(item, Mapping) and str(item.get("relative") or "").strip():
                    names.append(str(item.get("relative")).strip())
                elif isinstance(item, str) and item.strip():
                    names.append(item.strip())
            if names:
                body["input_files"] = names
        return body

    def start_run(
        self,
        *,
        title: str,
        directory: str,
        instruction: str = "",
        artifacts: list[str] | None = None,
        charter: dict | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._engine() as (store, engine):
            # The verified controller owns an ASCII UUID workspace.  The
            # application Goal id may contain Unicode and TeleAgent carries
            # the workspace in an HTTP header, whose Windows client encoding
            # cannot safely represent such paths.  `directory` is only the
            # staging source for declared dependency files, never the worker root.
            built = self._charter(
                title=title,
                instruction=instruction,
                artifacts=list(artifacts or []),
                charter=charter,
            )
            job = engine.submit(built)
            with store.transaction():
                job = store.get(job["id"])
                # Scheduler directory passed into start_run. Outputs are mirrored
                # back here at collect time; the TeleAgent UUID workspace stays
                # the worker root and is not the handoff root.
                job["scheduler_directory"] = str(Path(directory))
                store.save(job)
            names = [str(x) for x in (built.get("input_files") or [])]
            try:
                copied = copy_staged_inputs(Path(directory), Path(job["workspace"]), names)
            except (HandoffError, OSError, ValueError) as e:
                try:
                    engine.cancel(job["id"])
                except (ValueError, RuntimeError, OSError):
                    pass
                return {
                    "ok": False,
                    "backend": self.backend_id,
                    "run_id": job["id"],
                    "native_handle": job.get("session_id") or job["id"],
                    "state": "failed",
                    "error": f"input handoff failed: {e}",
                    "contract_version": "contract.v0.1-draft",
                }
            if copied:
                with store.transaction():
                    job = store.get(job["id"])
                    updated = dict(job.get("charter") or {})
                    updated["input_files"] = list(copied)
                    updated["input_file_paths"] = [str(Path(job["workspace"]) / rel) for rel in copied]
                    job["charter"] = updated
                    store.save(job)
            engine.tick()
            job = store.get(job["id"])
            failed = job.get("state") in {"failed", "timed_out"}
            err = job.get("error") or ""
            nh = _need_human_fields(err)
            out = {
                "ok": not failed,
                "backend": self.backend_id,
                "run_id": job["id"],
                "native_handle": job.get("session_id") or job["id"],
                "state": job.get("state"),
                "error": err,
                "contract_version": "contract.v0.1-draft",
            }
            if nh.get("need_human"):
                out["need_human"] = True
                out["failure_reason"] = nh.get("failure_reason") or ""
            return out

    def observe_run(
        self,
        run_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict[str, Any]:
        del dispatch_user_message_id, fetch_messages
        with self._lock, self._engine() as (store, engine):
            engine.tick()
            job = store.get(run_id)
            state = str(job.get("state") or "unknown")
            successful = state == "passed"
            cancelled = state == "cancelled"
            errored = state in {"failed", "timed_out"}
            err = job.get("error") or ""
            nh = _need_human_fields(err)
            return {
                "session_id": job.get("session_id") or "",
                "native_handle": job.get("session_id") or run_id,
                "activity": "idle" if state in TERMINAL else "busy",
                "busy": state not in TERMINAL,
                "status_ok": True,
                "finish": "stop" if successful else ("cancelled" if cancelled else ("error" if errored else None)),
                "assistant_error": err,
                "finish_successful": successful,
                "cancelled": cancelled,
                "errored": errored,
                "controller_state": state,
                "readonly": True,
                "backend": self.backend_id,
                "need_human": bool(nh.get("need_human")),
                "failure_reason": nh.get("failure_reason") or "",
            }

    def collect_result(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._engine() as (store, _engine):
            job = store.get(run_id)
            state = str(job.get("state") or "unknown")
            root = Path(job["workspace"])
            expected = [str(x) for x in (job.get("charter") or {}).get("artifacts", [])]
            present = [str(root / rel) for rel in expected if (root / rel).is_file()]
            missing = [rel for rel in expected if not (root / rel).is_file()]
            _mirror_outputs_to_scheduler(job, root, present)
            ok = state == "passed" and not missing
            err = job.get("error") or ""
            nh = _need_human_fields(err)
            out = {
                "ok": ok,
                "backend": self.backend_id,
                "run_id": run_id,
                "native_handle": job.get("session_id") or run_id,
                "state": "ok" if ok else state,
                "finish": "stop" if ok else ("cancelled" if state == "cancelled" else "error"),
                "artifacts": present,
                "workspace": str(root),
                "missing": missing,
                "error": err,
                "controller_state": state,
            }
            if nh.get("need_human"):
                out["need_human"] = True
                out["failure_reason"] = nh.get("failure_reason") or ""
            return out

    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        with self._lock:
            store = Store(self.state_dir)
            try:
                rows = store.inbox()
                if session_id:
                    rows = [row for row in rows if str(row.get("job_id") or "") == str(session_id)]
                jobs: dict[str, dict] = {}
                try:
                    for job in store.jobs():
                        if isinstance(job, dict) and job.get("id"):
                            jobs[str(job.get("id"))] = job
                except Exception:
                    jobs = {}
                actions = []
                for row in rows:
                    # scope is computed here and is not part of the permission
                    # payload. Engine.decide digest-compares that payload with
                    # the live TeleAgent object and must not see scope in it.
                    action = {
                        "request_id": row.get("request_id"),
                        "kind": row.get("kind"),
                        "run_id": row.get("job_id"),
                        "session_id": row.get("session_id"),
                        "context_hash": row.get("context_hash"),
                        "payload": row.get("payload"),
                    }
                    if row.get("kind") == "permission":
                        charter: dict = {}
                        job = jobs.get(str(row.get("job_id") or ""))
                        if isinstance(job, dict) and isinstance(job.get("charter"), dict):
                            charter = job["charter"]
                        elif isinstance(row.get("charter"), dict):
                            charter = row["charter"]
                        try:
                            scope = external_directory_scope(
                                row.get("payload"),
                                charter.get("external_inputs") or [],
                            )
                        except Exception:
                            scope = None
                        if scope is not None:
                            action["scope"] = scope
                    actions.append(action)
                return 200, actions
            finally:
                store.db.close()

    @staticmethod
    def _choice(kind: str, verdict: str) -> str:
        value = str(verdict or "").strip().lower()
        maps = {
            "permission": {
                "once": "once", "allow": "once", "approve": "once",
                "reject": "reject", "deny": "reject", "demand_safe_path": "reject", "deny_job": "deny_job",
            },
            "question": {
                "answer": "answer", "approve": "answer", "allow": "answer",
                "reject": "deny_job", "deny": "deny_job", "deny_job": "deny_job",
            },
            "review": {
                "pass": "pass", "approve": "pass", "allow": "pass",
                "fail": "fail", "reject": "fail", "deny": "fail",
            },
            "system_action": {
                "approve": "approve", "allow": "approve", "once": "approve",
                "reject": "reject", "deny": "reject", "deny_job": "deny_job",
            },
        }
        choice = maps.get(kind, {}).get(value)
        if not choice:
            raise BackendError(BackendStatus.FAILED, f"illegal verdict {verdict!r} for {kind}", capability="resolve_decision")
        return choice

    def resolve_decision(
        self,
        request_id: str,
        *,
        verdict: str,
        reason: str,
        answers: list | None = None,
    ) -> dict[str, Any]:
        """Apply one human verdict on the controller inbox.

        Refusals propagate unchanged. ``ArtifactContaminatedError`` keeps
        ``summary`` and ``findings``; other ``ValueError`` messages stay
        intact so the application API can explain the HTTP 409. Do not wrap
        them in ``BackendError`` — that would drop the type and attributes.
        """
        with self._lock, self._engine() as (store, engine):
            row = store.db.execute(
                "SELECT kind,data,resolved FROM requests WHERE id=?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                raise BackendError(BackendStatus.FAILED, "unknown controller decision", capability="resolve_decision")
            if int(row["resolved"]):
                return {"ok": True, "idempotent": True, "request_id": request_id}
            packet = json.loads(row["data"])
            kind = str(row["kind"])
            decision: dict[str, Any] = {
                "request_id": request_id,
                "context_hash": packet["context_hash"],
                "decision": self._choice(kind, verdict),
                "reason": str(reason or "Resolved through the application API"),
            }
            if kind == "question" and decision["decision"] == "answer":
                decision["answers"] = list(answers or [])
            # engine.decide raises ArtifactContaminatedError / ValueError with
            # message and attributes. Let them leave this method unwrapped.
            job = engine.decide(decision)
            return {
                "ok": True,
                "request_id": request_id,
                "kind": kind,
                "controller_state": job.get("state"),
            }

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        try:
            return 200, self.resolve_decision(
                request_id,
                verdict=reply,
                reason="Permission resolved through the application API",
            )
        except (BackendError, ValueError, RuntimeError) as e:
            return 409, {"ok": False, "error": str(e)}

    def cancel(self, run_id: str) -> tuple[int, Any]:
        try:
            with self._lock, self._engine() as (_store, engine):
                job = engine.cancel(run_id)
                return 200, {
                    "ok": job.get("state") == "cancelled",
                    "run_id": run_id,
                    "state": job.get("state"),
                    "pending": job.get("state") == "stopping",
                }
        except (ValueError, RuntimeError, OSError) as e:
            return 409, {"ok": False, "error": str(e), "run_id": run_id}


__all__ = ["WindowsSupervisedExecutionBackend"]
