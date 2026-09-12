"""Deterministic in-process ExecutionBackend — writes charter artifacts locally.

No TeleAgent HTTP. No Hermes ledger. Permissions are unsupported (never auto-approve).
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from execution_backend.base import BackendError, BackendStatus, ExecutionBackendABC, unsupported


class InProcessExecutionBackend(ExecutionBackendABC):
    backend_id = "inprocess.local_v1"

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def start_run(
        self,
        *,
        title: str,
        directory: str,
        instruction: str = "",
        artifacts: list[str] | None = None,
        charter: dict | None = None,
    ) -> dict[str, Any]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        arts = list(artifacts or [])
        if not arts and isinstance(charter, dict):
            done = charter.get("done_when") if isinstance(charter.get("done_when"), dict) else {}
            arts = list(done.get("artifacts") or [])
        run_id = f"inproc_{uuid.uuid4().hex[:12]}"
        handle = f"native_{run_id}"
        written: list[str] = []
        errors: list[str] = []
        for rel in arts:
            rel_s = str(rel).strip()
            if not rel_s or ".." in Path(rel_s).parts:
                errors.append(f"refuse_path:{rel_s}")
                continue
            cand = Path(rel_s)
            if cand.is_absolute():
                try:
                    rel_s = str(cand.relative_to(root.resolve()))
                except ValueError:
                    errors.append(f"refuse_path_outside_workdir:{rel_s}")
                    continue
            if ".." in Path(rel_s).parts:
                errors.append(f"refuse_path:{rel_s}")
                continue
            path = root / rel_s
            path.parent.mkdir(parents=True, exist_ok=True)
            # Deterministic one-line greeting for hello-style jobs; generic marker otherwise.
            if rel_s.endswith("hello-from-worker.txt") or "hello" in rel_s.lower():
                body = "hello from inprocess backend\n"
            else:
                body = f"inprocess artifact for {title or 'job'}\n"
            path.write_text(body, encoding="utf-8")
            written.append(str(path))

        ok = bool(written) and not errors
        finish = "stop" if ok else "error"
        rec = {
            "run_id": run_id,
            "native_handle": handle,
            "title": title,
            "directory": str(root),
            "instruction": instruction,
            "artifacts": arts,
            "written": written,
            "errors": errors,
            "started_at": time.time(),
            "activity": "idle",
            "finish": finish,
            "assistant_error": "; ".join(errors),
            "cancelled": False,
            "state": "succeeded" if ok else "failed",
        }
        self._runs[run_id] = rec
        self._runs[handle] = rec  # allow observe by native_handle
        return {
            "ok": ok,
            "backend": self.backend_id,
            "run_id": run_id,
            "native_handle": handle,
            "state": rec["state"],
            "artifacts_written": written,
            "errors": errors,
            "contract_version": "contract.v0.1-draft",
        }

    def _get(self, run_id: str) -> dict[str, Any]:
        rec = self._runs.get(run_id)
        if rec is None:
            raise BackendError(BackendStatus.FAILED, f"unknown run_id={run_id}", capability="observe_run")
        return rec

    def observe_run(
        self,
        run_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict[str, Any]:
        rec = self._get(run_id)
        if rec.get("cancelled"):
            activity = "idle"
            fin = "cancelled"
            finish_successful = False
            errored = False
            cancelled = True
        else:
            activity = rec.get("activity") or "unknown"
            fin = rec.get("finish")
            finish_successful = fin in ("stop", "complete", "completed") and not rec.get("errors")
            errored = fin == "error" or bool(rec.get("assistant_error"))
            cancelled = False
        # Align with teleagent_adapter.run_observe public keys
        return {
            "session_id": rec["run_id"],
            "native_handle": rec["native_handle"],
            "activity": activity,
            "busy": activity != "idle",
            "status_ok": True,
            "status_http": 200,
            "messages_ok": True if fetch_messages else None,
            "message_http": 200 if fetch_messages else None,
            "finish": fin,
            "assistant_error": rec.get("assistant_error") or "",
            "this_round_found": True if fetch_messages else None,
            "dispatch_user_message_id": dispatch_user_message_id or "",
            "finish_successful": finish_successful,
            "cancelled": cancelled,
            "errored": errored,
            "readonly": True,
            "backend": self.backend_id,
        }

    def collect_result(self, run_id: str) -> dict[str, Any]:
        rec = self._get(run_id)
        obs = self.observe_run(run_id)
        present = [p for p in rec.get("written") or [] if Path(p).is_file()]
        ok = bool(obs.get("finish_successful")) and len(present) == len(rec.get("artifacts") or [])
        if rec.get("cancelled"):
            ok = False
        return {
            "ok": ok,
            "backend": self.backend_id,
            "run_id": rec["run_id"],
            "native_handle": rec["native_handle"],
            "state": "ok" if ok else ("cancelled" if rec.get("cancelled") else "fail"),
            "finish": obs.get("finish"),
            "artifacts": present,
            "missing": [a for a in (rec.get("artifacts") or []) if not (Path(rec["directory"]) / a).is_file()],
            "error": rec.get("assistant_error") or "",
            "run_observation": {
                k: obs.get(k)
                for k in ("activity", "finish_successful", "cancelled", "errored", "busy")
            },
        }

    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        # Inprocess never raises TeleAgent permissions — empty public list.
        return 200, []

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        # Missing capability: unsupported — never pretend once/reject success.
        body = unsupported("reply_permission", f"inprocess has no permission channel; id={request_id}")
        return 501, body

    def cancel(self, run_id: str) -> tuple[int, Any]:
        try:
            rec = self._get(run_id)
        except BackendError as e:
            return 404, {"ok": False, "error": str(e)}
        rec["cancelled"] = True
        rec["finish"] = "cancelled"
        rec["activity"] = "idle"
        rec["state"] = "cancelled"
        return 200, {"ok": True, "run_id": rec["run_id"], "state": "cancelled"}


def run_file_job_via_public_api(
    *,
    workdir: str | Path,
    charter: dict,
    backend: InProcessExecutionBackend | None = None,
) -> dict[str, Any]:
    """Prove a small file job completes using ONLY ExecutionBackend public methods.

    Does not call TeleAgent HTTP or glue.run_job.
    """
    be = backend or InProcessExecutionBackend()
    from charter import expected_artifacts as resolve_arts, job_name

    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    name = job_name(charter) if callable(job_name) else str(charter.get("name") or "job")
    try:
        arts = resolve_arts(charter, workspace=root)
    except Exception:
        done = charter.get("done_when") if isinstance(charter.get("done_when"), dict) else {}
        arts = list(done.get("artifacts") or [])

    started = be.start_run(
        title=name,
        directory=str(root),
        instruction=str(charter.get("goal") or ""),
        artifacts=[Path(a).name if Path(a).is_absolute() else a for a in arts],
        charter=charter,
    )
    run_id = started["run_id"]
    code, pending = be.list_pending_actions(session_id=run_id)
    if code >= 300:
        return {"ok": False, "error": "list_pending failed", "started": started}
    # Must not invent approvals for pending (there should be none).
    for p in pending:
        rid = str((p or {}).get("request_id") or "")
        if rid:
            be.reply_permission(rid, "once")  # will be unsupported — do not treat as approve

    obs = be.observe_run(run_id)
    if obs.get("busy") or not obs.get("finish_successful"):
        return {
            "ok": False,
            "error": "observe gate failed",
            "started": started,
            "run_observation": obs,
            "pending_http": code,
            "pending_count": len(pending),
        }
    result = be.collect_result(run_id)
    result["started"] = started
    result["pending_count"] = len(pending)
    result["used_public_api_only"] = True
    result["backend"] = be.backend_id
    return result
