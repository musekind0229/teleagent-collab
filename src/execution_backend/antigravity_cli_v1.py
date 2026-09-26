"""Antigravity CLI worker backend — agy --print= (ExecutionBackendABC).

Worker only. Not a LeadAdapter. Not a Hermes ledger. Does not touch
teleagent_adapter. Permissions stay fail-closed: reply_permission is
unsupported, and --dangerously-skip-permissions is OFF unless charter/env
explicitly sets agy_auto_approve=true.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from execution_backend.agy_account_pool import (
    ENV_POOL,
    ENV_PROFILE,
    clear_windows_antigravity_keyring,
)
from execution_backend.base import BackendError, BackendStatus, ExecutionBackendABC, unsupported

BACKEND_ID = "antigravity.cli_v1"
DEFAULT_AGY_MODEL = "gemini-3.8-flash-low"
DEFAULT_AGY_FALLBACK_BIN = "/home/box/.local/bin/agy"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
PRINT_FLAG_PREFIX = "--print="

_OK_STATUS = frozenset(
    {"ok", "success", "succeeded", "completed", "complete", "stop", "done", "finished"}
)
_FAIL_STATUS = frozenset(
    {"error", "failed", "fail", "cancelled", "canceled", "timeout", "unavailable"}
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def resolve_agy_bin(
    *,
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """AGY_BIN > PATH `agy` > /home/box/.local/bin/agy > `agy`."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    env = environ if environ is not None else os.environ
    raw = str(env.get("AGY_BIN") or "").strip()
    if raw:
        return raw
    found = shutil.which("agy")
    if found:
        return found
    fallback = Path(DEFAULT_AGY_FALLBACK_BIN)
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    return "agy"


def resolve_agy_model(
    *,
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
    charter: dict | None = None,
) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    env = environ if environ is not None else os.environ
    raw = str(env.get("AGY_MODEL") or "").strip()
    if raw:
        return raw
    if isinstance(charter, dict):
        for key in ("agy_model", "model"):
            val = charter.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return DEFAULT_AGY_MODEL


def agy_auto_approve_enabled(
    charter: dict | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Default False. Only charter/env agy_auto_approve=true turns skip-permissions on."""
    env = environ if environ is not None else os.environ
    for key in ("AGY_AUTO_APPROVE", "COLLAB_AGY_AUTO_APPROVE"):
        raw = str(env.get(key) or "").strip().lower()
        if raw in _TRUTHY:
            return True
    if isinstance(charter, dict):
        val = charter.get("agy_auto_approve")
        if val is True:
            return True
        if isinstance(val, str) and val.strip().lower() in _TRUTHY:
            return True
    return False


def build_agy_argv(
    *,
    bin_path: str,
    model: str,
    prompt: str,
    skip_permissions: bool = False,
) -> list[str]:
    """Verified non-interactive argv. ``--print`` MUST use ``=`` or the next flag is the prompt."""
    cmd = [
        bin_path,
        "--output-format=json",
        f"--model={model}",
    ]
    if skip_permissions:
        cmd.append(SKIP_PERMISSIONS_FLAG)
    # Pitfall: `agy --print --output-format=json` treats `--output-format=json` as the prompt.
    cmd.append(f"{PRINT_FLAG_PREFIX}{prompt}")
    return cmd


def build_agy_prompt(
    *,
    title: str = "",
    instruction: str = "",
    charter: dict | None = None,
    artifacts: list[str] | None = None,
) -> str:
    chunks: list[str] = []
    if title and str(title).strip():
        chunks.append(f"Job title: {title.strip()}")
    instr = (instruction or "").strip()
    if instr:
        chunks.append(instr)
    elif isinstance(charter, dict) and charter.get("goal"):
        try:
            from charter import build_instruction

            chunks.append(build_instruction(charter))
        except Exception:
            chunks.append(str(charter.get("goal") or "").strip())
    arts = [str(a).strip() for a in (artifacts or []) if str(a).strip()]
    if arts:
        chunks.append("Create these artifacts in the current working directory, then stop:")
        for a in arts:
            chunks.append(f"- {a}")
    chunks.append(
        "Stay inside the working directory. When finished, stop. Do not wait for further input."
    )
    return "\n\n".join(chunks)


def parse_agy_json(stdout: str = "", stderr: str = "") -> dict[str, Any] | None:
    """Parse agy --output-format=json object (conversation_id / status / response / usage)."""
    for text in (stdout, stderr):
        if not text or not str(text).strip():
            continue
        s = str(text).strip()
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    return None


def artifacts_from_charter(
    charter: dict | None,
    artifacts: list[str] | None = None,
) -> list[str]:
    arts = [str(a).strip() for a in (artifacts or []) if str(a).strip()]
    if arts:
        return arts
    if not isinstance(charter, dict):
        return []
    done = charter.get("done_when") if isinstance(charter.get("done_when"), dict) else {}
    raw = list(done.get("artifacts") or done.get("files") or [])
    extra = charter.get("artifacts")
    if isinstance(extra, str) and extra.strip():
        raw.append(extra)
    elif isinstance(extra, list):
        raw.extend(extra)
    return [str(a).strip() for a in raw if str(a).strip()]


def _sanitize_artifact_rel(rel: str, root: Path) -> tuple[str | None, str | None]:
    rel_s = str(rel).strip()
    if not rel_s or ".." in Path(rel_s).parts:
        return None, f"refuse_path:{rel_s}"
    cand = Path(rel_s)
    if cand.is_absolute():
        try:
            rel_s = str(cand.relative_to(root.resolve()))
        except ValueError:
            return None, f"refuse_path_outside_workdir:{rel_s}"
        if ".." in Path(rel_s).parts:
            return None, f"refuse_path:{rel_s}"
    return rel_s, None


def _redact_argv(cmd: list[str]) -> list[str]:
    out: list[str] = []
    for a in cmd:
        if a.startswith(PRINT_FLAG_PREFIX):
            out.append(f"{PRINT_FLAG_PREFIX}<redacted>")
        else:
            out.append(a)
    return out


def _status_ok(status: str | None, returncode: int | None) -> bool:
    if returncode not in (0, None):
        return False
    key = str(status or "").strip().lower()
    if key in _FAIL_STATUS:
        return False
    if not key:
        return returncode in (0, None)
    return key in _OK_STATUS or key not in _FAIL_STATUS


class AntigravityCliExecutionBackend(ExecutionBackendABC):
    """Spawn `agy --print=` under the job directory. Ledger remains collab."""

    backend_id = BACKEND_ID

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        model: str | None = None,
        timeout_sec: float | None = None,
        environ: Mapping[str, str] | None = None,
        poll_sec: float = 0.1,
    ) -> None:
        self._environ = dict(environ) if environ is not None else None
        self.bin_path = resolve_agy_bin(explicit=bin_path, environ=self._env())
        self.model = resolve_agy_model(explicit=model, environ=self._env())
        self.timeout_sec = 300.0 if timeout_sec is None else float(timeout_sec)
        self.poll_sec = float(poll_sec)
        self._runs: dict[str, dict[str, Any]] = {}

    def _env(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    def _agy_profile(self) -> str:
        return str(self._env().get("AGY_PROFILE") or "").strip()

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
        raw_arts = artifacts_from_charter(charter, artifacts)
        arts: list[str] = []
        errors: list[str] = []
        for rel in raw_arts:
            cleaned, err = _sanitize_artifact_rel(rel, root)
            if err:
                errors.append(err)
                continue
            if cleaned:
                arts.append(cleaned)

        skip = agy_auto_approve_enabled(charter, self._env())
        model = resolve_agy_model(explicit=self.model, environ=self._env(), charter=charter)
        prompt = build_agy_prompt(
            title=title,
            instruction=instruction,
            charter=charter,
            artifacts=arts,
        )
        cmd = build_agy_argv(
            bin_path=self.bin_path,
            model=model,
            prompt=prompt,
            skip_permissions=skip,
        )
        timeout = self.timeout_sec
        if isinstance(charter, dict) and charter.get("timeout_sec") is not None:
            try:
                timeout = float(charter.get("timeout_sec"))
            except (TypeError, ValueError):
                timeout = self.timeout_sec

        run_id = f"agy_{uuid.uuid4().hex[:12]}"
        handle = f"agy_native_{run_id}"
        rec: dict[str, Any] = {
            "run_id": run_id,
            "native_handle": handle,
            "conversation_id": "",
            "title": title,
            "directory": str(root),
            "instruction": instruction,
            "artifacts": arts,
            "started_at": time.time(),
            "activity": "busy",
            "finish": None,
            "assistant_error": "",
            "cancelled": False,
            "timed_out": False,
            "state": "running",
            "skip_permissions": bool(skip),
            "argv_flags": _redact_argv(cmd),
            "model": model,
            "bin_path": self.bin_path,
            "timeout_sec": timeout,
            "proc": None,
            "harvested": False,
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "agy_json": None,
            "usage": None,
            "response": "",
            "path_errors": errors,
        }
        self._runs[run_id] = rec
        self._runs[handle] = rec

        try:
            # Machine-wide slot: clear only for a pool-bound spawn, immediately before Popen.
            spawn_env = self._env()
            if str(spawn_env.get(ENV_POOL) or "").strip() or str(spawn_env.get(ENV_PROFILE) or "").strip():
                clear_windows_antigravity_keyring()
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                env=dict(self._env()),
            )
        except OSError as e:
            rec["state"] = "failed"
            rec["activity"] = "idle"
            rec["finish"] = "error"
            rec["assistant_error"] = f"agy spawn failed: {e}"
            rec["harvested"] = True
            rec["path_errors"] = errors + [rec["assistant_error"]]
            return self._start_payload(rec, ok=False)

        rec["proc"] = proc
        rec["pid"] = proc.pid
        return self._start_payload(rec, ok=True)

    def _start_payload(self, rec: dict[str, Any], *, ok: bool) -> dict[str, Any]:
        payload = {
            "ok": ok,
            "backend": self.backend_id,
            "run_id": rec["run_id"],
            "native_handle": rec["native_handle"],
            "state": rec["state"],
            "artifacts_written": [],
            "errors": list(rec.get("path_errors") or []),
            "skip_permissions": bool(rec.get("skip_permissions")),
            "argv_flags": list(rec.get("argv_flags") or []),
            "contract_version": "contract.v0.1-draft",
        }
        profile = self._agy_profile()
        if profile:
            payload["agy_profile"] = profile
        return payload

    def _get(self, run_id: str) -> dict[str, Any]:
        rec = self._runs.get(run_id)
        if rec is None:
            raise BackendError(BackendStatus.FAILED, f"unknown run_id={run_id}", capability="observe_run")
        return rec

    def _kill_proc(self, proc: subprocess.Popen) -> bool:
        if proc.poll() is not None:
            return True
        # Windows has no os.killpg. taskkill /T is the process-tree equivalent
        # (a .cmd agy shim otherwise leaves the Python child holding the pipes).
        if not hasattr(os, "killpg"):
            return self._kill_proc_tree_windows(proc)
        killed = False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            killed = True
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
                killed = True
            except OSError:
                return False
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    return False
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return proc.poll() is not None
        return killed or proc.poll() is not None

    def _kill_proc_tree_windows(self, proc: subprocess.Popen) -> bool:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 10,
            "check": False,
        }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], **kwargs)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                return False
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return proc.poll() is not None
        return proc.poll() is not None

    def _harvest(self, rec: dict[str, Any]) -> None:
        if rec.get("harvested"):
            return
        proc: subprocess.Popen | None = rec.get("proc")
        if proc is None:
            rec["harvested"] = True
            rec["activity"] = "idle"
            if not rec.get("finish"):
                rec["finish"] = "error"
                rec["state"] = "failed"
            return
        if proc.poll() is None:
            return
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception as e:  # noqa: BLE001 — harvest must not raise to callers
            stdout, stderr = rec.get("stdout") or "", f"{rec.get('stderr') or ''}{e}"
        rec["stdout"] = stdout or ""
        rec["stderr"] = stderr or ""
        rec["returncode"] = proc.returncode
        rec["harvested"] = True
        rec["activity"] = "idle"
        parsed = parse_agy_json(rec["stdout"], rec["stderr"])
        rec["agy_json"] = parsed
        if isinstance(parsed, dict):
            conv = str(parsed.get("conversation_id") or "").strip()
            if conv:
                rec["conversation_id"] = conv
                self._runs[conv] = rec
            rec["response"] = parsed.get("response") if parsed.get("response") is not None else ""
            rec["usage"] = parsed.get("usage")
            status = parsed.get("status")
        else:
            status = None
        if rec.get("cancelled"):
            rec["finish"] = "cancelled"
            rec["state"] = "cancelled"
            return
        if rec.get("timed_out"):
            rec["finish"] = "error"
            rec["state"] = "failed"
            rec["assistant_error"] = rec.get("assistant_error") or "timeout"
            return
        rc = rec.get("returncode")
        if parsed is None:
            rec["finish"] = "error"
            rec["state"] = "failed"
            err = (rec.get("stderr") or rec.get("stdout") or f"agy exit={rc}").strip()
            rec["assistant_error"] = err[:2000] or f"agy exit={rc}"
            return
        if _status_ok(str(status) if status is not None else None, rc):
            rec["finish"] = "stop"
            rec["state"] = "succeeded"
            rec["assistant_error"] = ""
        else:
            rec["finish"] = "error"
            rec["state"] = "failed"
            rec["assistant_error"] = (
                str((parsed or {}).get("error") or rec.get("stderr") or rec.get("response") or f"agy status={status} exit={rc}")
            )[:2000]

    def _refresh(self, rec: dict[str, Any]) -> None:
        proc: subprocess.Popen | None = rec.get("proc")
        if rec.get("cancelled") and rec.get("harvested"):
            rec["activity"] = "idle"
            rec["finish"] = "cancelled"
            return
        if proc is not None and proc.poll() is None:
            rec["activity"] = "busy"
            rec["state"] = rec.get("state") or "running"
            return
        self._harvest(rec)

    def observe_run(
        self,
        run_id: str,
        *,
        dispatch_user_message_id: str | None = None,
        fetch_messages: bool = True,
    ) -> dict[str, Any]:
        rec = self._get(run_id)
        self._refresh(rec)
        if rec.get("cancelled"):
            activity = "idle"
            fin = "cancelled"
            finish_successful = False
            errored = False
            cancelled = True
            busy = False
        else:
            activity = rec.get("activity") or "unknown"
            fin = rec.get("finish")
            busy = activity != "idle"
            finish_successful = (
                (not busy)
                and fin in ("stop", "complete", "completed")
                and not rec.get("assistant_error")
                and not rec.get("timed_out")
            )
            errored = (not busy) and (fin == "error" or bool(rec.get("assistant_error")))
            cancelled = False
        out = {
            "session_id": rec.get("conversation_id") or rec["run_id"],
            "native_handle": rec["native_handle"],
            "activity": activity,
            "busy": busy,
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
            "conversation_id": rec.get("conversation_id") or "",
            "skip_permissions": bool(rec.get("skip_permissions")),
            "usage": rec.get("usage"),
        }
        profile = self._agy_profile()
        if profile:
            out["agy_profile"] = profile
        return out

    def collect_result(self, run_id: str) -> dict[str, Any]:
        rec = self._get(run_id)
        proc: subprocess.Popen | None = rec.get("proc")
        if proc is not None and proc.poll() is None and not rec.get("cancelled"):
            timeout = rec.get("timeout_sec")
            try:
                proc.wait(timeout=None if timeout is None else float(timeout))
            except subprocess.TimeoutExpired:
                rec["timed_out"] = True
                rec["assistant_error"] = rec.get("assistant_error") or "timeout"
                self._kill_proc(proc)
        self._refresh(rec)
        obs = self.observe_run(run_id)
        present: list[str] = []
        missing: list[str] = []
        root = Path(rec["directory"])
        for a in rec.get("artifacts") or []:
            p = Path(a)
            path = p if p.is_absolute() else root / a
            if path.is_file():
                present.append(str(path))
            else:
                missing.append(str(a))
        ok = bool(obs.get("finish_successful")) and not missing
        if rec.get("cancelled") or rec.get("timed_out"):
            ok = False
        state = "ok" if ok else ("cancelled" if rec.get("cancelled") else "fail")
        parsed = rec.get("agy_json") if isinstance(rec.get("agy_json"), dict) else {}
        out = {
            "ok": ok,
            "backend": self.backend_id,
            "run_id": rec["run_id"],
            "native_handle": rec["native_handle"],
            "conversation_id": rec.get("conversation_id") or parsed.get("conversation_id") or "",
            "state": state,
            "finish": obs.get("finish"),
            "artifacts": present,
            "missing": missing,
            "error": rec.get("assistant_error") or "",
            "response": rec.get("response") if rec.get("response") is not None else parsed.get("response"),
            "usage": rec.get("usage") if rec.get("usage") is not None else parsed.get("usage"),
            "skip_permissions": bool(rec.get("skip_permissions")),
            "run_observation": {
                k: obs.get(k)
                for k in ("activity", "finish_successful", "cancelled", "errored", "busy")
            },
        }
        profile = self._agy_profile()
        if profile:
            out["agy_profile"] = profile
        return out

    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        # agy print is one-shot. No permission channel — same as inprocess.
        return 200, []

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        # Fail-closed. skip-permissions is NOT an approval through this contract.
        body = unsupported(
            "reply_permission",
            f"antigravity.cli_v1 has no permission channel; id={request_id}",
        )
        return 501, body

    def cancel(self, run_id: str) -> tuple[int, Any]:
        try:
            rec = self._get(run_id)
        except BackendError as e:
            return 404, {"ok": False, "error": str(e)}
        proc: subprocess.Popen | None = rec.get("proc")
        if proc is None or proc.poll() is not None:
            self._harvest(rec)
            if rec.get("cancelled"):
                return 200, {"ok": True, "run_id": rec["run_id"], "state": "cancelled"}
            return 200, {
                "ok": True,
                "run_id": rec["run_id"],
                "state": rec.get("state") or "already_finished",
            }
        if not self._kill_proc(proc):
            body = unsupported("cancel", f"could not signal agy pid={getattr(proc, 'pid', None)}")
            return 501, body
        rec["cancelled"] = True
        rec["finish"] = "cancelled"
        rec["activity"] = "idle"
        rec["state"] = "cancelled"
        self._harvest(rec)
        return 200, {"ok": True, "run_id": rec["run_id"], "state": "cancelled"}


def run_antigravity_job_via_public_api(
    *,
    workdir: str | Path,
    charter: dict,
    instruction: str = "",
    name: str = "",
    timeout_sec: float | None = None,
    backend: AntigravityCliExecutionBackend | None = None,
    poll_sec: float | None = None,
) -> dict[str, Any]:
    """Prove a job via ONLY ExecutionBackend public methods (no glue, no TA HTTP)."""
    be = backend or AntigravityCliExecutionBackend(
        timeout_sec=timeout_sec,
        poll_sec=0.1 if poll_sec is None else float(poll_sec),
    )
    from charter import expected_artifacts as resolve_arts, job_name

    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    job = name or (job_name(charter) if callable(job_name) else str(charter.get("name") or "job"))
    try:
        arts = resolve_arts(charter, workspace=root)
        arts = [Path(a).name if Path(a).is_absolute() else a for a in arts]
    except Exception:
        arts = artifacts_from_charter(charter)

    wall = timeout_sec
    if wall is None:
        wall = float(charter.get("timeout_sec") or be.timeout_sec)
    started = be.start_run(
        title=job,
        directory=str(root),
        instruction=instruction or str(charter.get("goal") or ""),
        artifacts=arts,
        charter=charter,
    )
    run_id = started["run_id"]
    code, pending = be.list_pending_actions(session_id=run_id)
    if code >= 300:
        return {"ok": False, "error": "list_pending failed", "started": started, "backend": be.backend_id}
    for p in pending:
        rid = str((p or {}).get("request_id") or "")
        if rid:
            be.reply_permission(rid, "once")  # unsupported — do not treat as approve

    deadline = time.time() + float(wall)
    obs = be.observe_run(run_id)
    while obs.get("busy"):
        if time.time() >= deadline:
            be.cancel(run_id)
            result = be.collect_result(run_id)
            result["ok"] = False
            result["error"] = result.get("error") or "timeout"
            result["started"] = started
            result["pending_count"] = len(pending)
            result["used_public_api_only"] = True
            result["backend"] = be.backend_id
            result["path"] = be.backend_id
            result["skip_permissions"] = bool(started.get("skip_permissions"))
            return result
        time.sleep(be.poll_sec)
        obs = be.observe_run(run_id)

    result = be.collect_result(run_id)
    result["started"] = started
    result["pending_count"] = len(pending)
    result["pending_summaries"] = []
    result["pending_seen"] = False
    result["used_public_api_only"] = True
    result["backend"] = be.backend_id
    result["path"] = be.backend_id
    result["skip_permissions"] = bool(started.get("skip_permissions"))
    return result


__all__ = [
    "BACKEND_ID",
    "DEFAULT_AGY_MODEL",
    "SKIP_PERMISSIONS_FLAG",
    "AntigravityCliExecutionBackend",
    "agy_auto_approve_enabled",
    "artifacts_from_charter",
    "build_agy_argv",
    "build_agy_prompt",
    "parse_agy_json",
    "resolve_agy_bin",
    "resolve_agy_model",
    "run_antigravity_job_via_public_api",
]
