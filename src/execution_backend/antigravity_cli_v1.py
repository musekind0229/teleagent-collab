"""Antigravity CLI worker backend — agy --print= (ExecutionBackendABC).

Worker only. Not a LeadAdapter. Not a Hermes ledger. Does not touch
teleagent_adapter. Permissions stay fail-closed: reply_permission is
unsupported, and --dangerously-skip-permissions is OFF unless charter/env
explicitly sets agy_auto_approve=true.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from execution_backend.agy_account_pool import (
    ENV_LEASE_ID,
    ENV_POOL,
    ENV_PROFILE,
    AccountPoolError,
    account_switch_lock,
    clear_windows_antigravity_keyring,
    finish_account_lease,
    prepare_antigravity_environ_from_pool,
)
from execution_backend.base import (
    BackendError,
    BackendStatus,
    ExecutionBackendABC,
    SKIP_PERMISSIONS_WARNING,
    default_capabilities,
    unsupported,
)
from framework import contract_render

_LOG = logging.getLogger(__name__)

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
    """Title, instruction (or charter goal), contract, artifacts, and a tail.

    The contract section is rendered strictly. A bad charter raises
    ``ContractRenderError``; callers must not catch that and continue with a
    goal-only prompt. Prompt text is not an OS sandbox.
    """
    normalized = contract_render.normalize_worker_contract(charter)
    chunks: list[str] = []
    if title and str(title).strip():
        chunks.append(f"Job title: {str(title).strip()}")
    instr = (instruction or "").strip()
    if not instr:
        goal = normalized.get("goal")
        if isinstance(goal, str) and goal.strip():
            instr = goal.strip()
    if instr:
        chunks.append(instr)
    section = contract_render.render_contract_section(normalized)
    if section:
        chunks.append(section)
    arts = [str(a).strip() for a in (artifacts or []) if str(a).strip()]
    if arts:
        chunks.append("Create these artifacts in the current working directory, then stop:")
        for a in arts:
            chunks.append(f"- {a}")
    if normalized.get("external_inputs"):
        chunks.append(
            "Write only inside the working directory. Outside it, you may only read the pinned "
            "external inputs listed above. When finished, stop. Do not wait for further input."
        )
    else:
        chunks.append(
            "Stay inside the working directory. When finished, stop. Do not wait for further input."
        )
    return "\n\n".join(chunks)


def _contract_meta(charter: dict | None) -> dict[str, Any]:
    normalized = contract_render.normalize_worker_contract(charter)
    return {
        "contract_sha256": contract_render.contract_fingerprint(normalized),
        "contract_fields": contract_render.contract_fields_present(normalized),
    }


def _apply_contract_meta(payload: dict[str, Any], rec: Mapping[str, Any]) -> dict[str, Any]:
    digest = rec.get("contract_sha256")
    if isinstance(digest, str) and digest:
        payload["contract_sha256"] = digest
        fields = rec.get("contract_fields")
        payload["contract_fields"] = list(fields) if isinstance(fields, list) else []
    return payload


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

    def capabilities(self) -> dict[str, Any]:
        """One-shot ``agy --print``. No permission/question/review channel.

        ``list_pending_actions`` is always empty and ``reply_permission`` is 501.
        External pins are prompt text only. ``skip_permissions`` mirrors
        ``agy_auto_approve_enabled(charter=None)`` on this backend's env.
        Run records are in-process; restart does not resume them.
        Exact-content acceptance is checked by ``apply_agy_acceptance_gate``,
        not by a lead. Prompt constraints are not an OS sandbox.
        """
        skip = agy_auto_approve_enabled(None, self._base_env())
        caps = default_capabilities(backend_id=self.backend_id, kind="antigravity_cli")
        caps["channels"] = {"permission": False, "question": False, "review": False}
        caps["external_inputs"]["enforcement"] = "prompt_only"
        caps["isolation"]["os_sandbox"] = False
        caps["isolation"]["access_audit"] = False
        caps["isolation"]["prompt_constraints"] = True
        caps["skip_permissions"] = bool(skip)
        caps["resume"] = False
        caps["acceptance"]["artifact_presence"] = True
        caps["acceptance"]["exact_content"] = True
        caps["acceptance"]["lead_review"] = False
        caps["acceptance"]["executable_checks"] = False
        caps["usage"]["source"] = "worker_self_reported"
        caps["concurrency"] = {
            "max_runs": self._account_pool_run_limit(),
            "limited_by": ["agy_account_pool"],
        }
        caps["warnings"] = [SKIP_PERMISSIONS_WARNING] if skip else []
        return caps

    def _account_pool_run_limit(self) -> int:
        """Lease cap. Known pool size, otherwise one run. Never logs the pool."""
        path = self._account_pool_path
        if not path:
            return 1
        try:
            from execution_backend.agy_account_pool import load_pool

            pool = load_pool(path)
            size = len(pool.accounts)
        except Exception:
            return 1
        return size if size >= 1 else 1

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        model: str | None = None,
        timeout_sec: float | None = None,
        environ: Mapping[str, str] | None = None,
        poll_sec: float = 0.1,
        account_pool_path: str | None = None,
        persist_pool: bool = True,
        precheck: Any | None = None,
    ) -> None:
        # Base environ (no account pin). Per-dispatch HOME lives on each run rec.
        # Only an explicit account_pool_path (from inject / factory) enables
        # per-dispatch select. ENV_POOL alone in environ is for pre-bound spawns
        # (collect writeback) and must not trigger prepare on start_run.
        self._base_environ = dict(environ) if environ is not None else None
        self._environ = dict(environ) if environ is not None else None
        self._account_pool_path = (str(account_pool_path).strip() if account_pool_path else "") or None
        self._persist_pool = bool(persist_pool)
        self._precheck = precheck
        self.bin_path = resolve_agy_bin(explicit=bin_path, environ=self._base_env())
        self.model = resolve_agy_model(explicit=model, environ=self._base_env())
        self.timeout_sec = 300.0 if timeout_sec is None else float(timeout_sec)
        self.poll_sec = float(poll_sec)
        self._runs: dict[str, dict[str, Any]] = {}

    def _base_env(self) -> Mapping[str, str]:
        return self._base_environ if self._base_environ is not None else os.environ

    def _env(self) -> Mapping[str, str]:
        # Last dispatch environ (serial collab-service); prefer rec["spawn_environ"].
        return self._environ if self._environ is not None else os.environ

    def _agy_profile(self, rec: Mapping[str, Any] | None = None) -> str:
        if rec is not None:
            profile = str(rec.get("agy_profile") or "").strip()
            if profile:
                return profile
            spawn = rec.get("spawn_environ")
            if isinstance(spawn, Mapping):
                return str(spawn.get(ENV_PROFILE) or "").strip()
        return str(self._env().get(ENV_PROFILE) or "").strip()

    def _bind_pool_environ_for_dispatch(self) -> dict[str, str]:
        """Select+reserve one account for this start_run (per-dispatch).

        When no explicit account_pool_path was wired, return the base environ
        unchanged (pre-bound AGY_PROFILE / ENV_POOL for tests and one-offs).
        """
        base = dict(self._base_env())
        pool_path = self._account_pool_path
        if not pool_path:
            return base
        prepared = prepare_antigravity_environ_from_pool(
            pool_path,
            base_environ=base,
            precheck=self._precheck,
            persist=self._persist_pool,
        )
        env = dict(prepared["environ"])
        self._environ = env
        return env

    def _release_pool_after_failed_spawn(self, spawn_env: Mapping[str, str] | None) -> None:
        """Drop lease when Popen fails after a successful prepare."""
        if not spawn_env:
            return
        pool_path = str(spawn_env.get(ENV_POOL) or "").strip()
        profile = str(spawn_env.get(ENV_PROFILE) or "").strip()
        if not pool_path or not profile:
            return
        try:
            finish_account_lease(
                pool_path,
                profile,
                lease_id=str(spawn_env.get(ENV_LEASE_ID) or "").strip() or None,
                persist=self._persist_pool,
                environ=spawn_env,
                classify_result_state=False,
            )
        except Exception as exc:  # noqa: BLE001 — spawn-fail path must stay local
            _LOG.warning("agy account lease release after spawn fail (%s)", type(exc).__name__)

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

        # Render before the account lock and before any process spawn. A bad
        # contract must not start a degraded goal-only run.
        try:
            prompt = build_agy_prompt(
                title=title,
                instruction=instruction,
                charter=charter,
                artifacts=arts,
            )
            contract_meta = _contract_meta(charter)
        except contract_render.ContractRenderError as exc:
            msg = f"contract_render_error: {exc}"
            return {
                "ok": False,
                "backend": self.backend_id,
                "state": "failed",
                "error": msg,
                "errors": [msg],
                "artifacts_written": [],
                "skip_permissions": False,
                "argv_flags": [],
                "contract_version": "contract.v0.1-draft",
            }

        root.mkdir(parents=True, exist_ok=True)
        # Account switch critical section (pool-global cross-process lock):
        # select+reserve → keyring clear → HOME/USERPROFILE env → Popen.
        # Released once the child has its env and is started; the agy run
        # itself is protected by the per-account busy lease + HOME lock.
        base_env = self._base_env()
        lock_pool = self._account_pool_path or str(base_env.get(ENV_POOL) or "").strip()
        with (account_switch_lock(lock_pool, environ=base_env) if lock_pool else nullcontext()):
            rec, failed = self._start_run_locked(
                title=title, root=root, instruction=instruction,
                arts=arts, errors=errors, charter=charter,
                prompt=prompt, contract_meta=contract_meta,
            )
        if failed is not None:
            return failed
        proc = rec["proc"]
        rec["pid"] = proc.pid
        self._start_pipe_drainers(rec, proc)
        return self._start_payload(rec, ok=True)

    def _start_run_locked(
        self,
        *,
        title: str,
        root: Path,
        instruction: str,
        arts: list[str],
        errors: list[str],
        charter: dict | None,
        prompt: str,
        contract_meta: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Bind env + clear keyring + Popen (caller holds the switch lock).

        Returns ``(rec, failure_payload)``; failure_payload is None on spawn.
        The prompt is already rendered; this method does not rebuild it.
        """
        spawn_env = self._bind_pool_environ_for_dispatch()

        skip = agy_auto_approve_enabled(charter, spawn_env)
        model = resolve_agy_model(explicit=self.model, environ=spawn_env, charter=charter)
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
        profile = str(spawn_env.get(ENV_PROFILE) or "").strip()
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
            "spawn_environ": dict(spawn_env),
            "agy_profile": profile,
            "contract_sha256": str(contract_meta.get("contract_sha256") or ""),
            "contract_fields": list(contract_meta.get("contract_fields") or []),
        }
        self._runs[run_id] = rec
        self._runs[handle] = rec

        try:
            # Machine-wide slot: clear only for a pool-bound spawn, immediately before Popen.
            # prepare_antigravity_environ_from_pool already cleared; clear again right
            # before Popen so a concurrent entrance cannot leave a keyring shadow.
            if str(spawn_env.get(ENV_POOL) or "").strip() or profile:
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
                env=dict(spawn_env),
            )
        except OSError as e:
            self._release_pool_after_failed_spawn(spawn_env)
            rec["state"] = "failed"
            rec["activity"] = "idle"
            rec["finish"] = "error"
            rec["assistant_error"] = f"agy spawn failed: {e}"
            rec["harvested"] = True
            rec["path_errors"] = errors + [rec["assistant_error"]]
            return rec, self._start_payload(rec, ok=False)
        rec["proc"] = proc
        return rec, None

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
        profile = self._agy_profile(rec)
        if profile:
            payload["agy_profile"] = profile
        return _apply_contract_meta(payload, rec)

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

    @staticmethod
    def _drain_stream(stream: Any, chunks: list[str], done: threading.Event) -> None:
        """Continuously read one PIPE so the child cannot block on a full buffer."""
        try:
            while True:
                try:
                    piece = stream.read(65536)
                except (ValueError, OSError):
                    break
                if not piece:
                    break
                chunks.append(piece)
        finally:
            done.set()

    def _start_pipe_drainers(self, rec: dict[str, Any], proc: subprocess.Popen) -> None:
        out_chunks: list[str] = []
        err_chunks: list[str] = []
        out_done = threading.Event()
        err_done = threading.Event()
        rec["stdout_chunks"] = out_chunks
        rec["stderr_chunks"] = err_chunks
        rec["stdout_done"] = out_done
        rec["stderr_done"] = err_done
        threads: list[threading.Thread] = []
        if proc.stdout is not None:
            t = threading.Thread(
                target=self._drain_stream,
                args=(proc.stdout, out_chunks, out_done),
                name=f"agy-stdout-{rec.get('run_id')}",
                daemon=True,
            )
            t.start()
            threads.append(t)
        else:
            out_done.set()
        if proc.stderr is not None:
            t = threading.Thread(
                target=self._drain_stream,
                args=(proc.stderr, err_chunks, err_done),
                name=f"agy-stderr-{rec.get('run_id')}",
                daemon=True,
            )
            t.start()
            threads.append(t)
        else:
            err_done.set()
        rec["drain_threads"] = threads

    def _join_drainers(self, rec: dict[str, Any], *, timeout: float = 5.0) -> None:
        threads = list(rec.get("drain_threads") or [])
        deadline = time.time() + float(timeout)
        for t in threads:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)
        out_done = rec.get("stdout_done")
        err_done = rec.get("stderr_done")
        if isinstance(out_done, threading.Event):
            out_done.wait(timeout=max(0.0, deadline - time.time()))
        if isinstance(err_done, threading.Event):
            err_done.wait(timeout=max(0.0, deadline - time.time()))

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
        # Child has exited; finish draining (already streaming, no communicate deadlock).
        self._join_drainers(rec, timeout=5.0)
        try:
            # Reap without reading pipes again (drainers already consumed them).
            if proc.returncode is None:
                proc.wait(timeout=1)
        except Exception:  # noqa: BLE001 — harvest must not raise to callers
            pass
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        stdout = "".join(rec.get("stdout_chunks") or [])
        stderr = "".join(rec.get("stderr_chunks") or [])
        # Prefer live chunks; fall back to any prior partials.
        if not stdout and rec.get("stdout"):
            stdout = str(rec.get("stdout") or "")
        if not stderr and rec.get("stderr"):
            stderr = str(rec.get("stderr") or "")
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
            # Enforce wall deadline for observe-only callers (not only collect/wait).
            timeout = rec.get("timeout_sec")
            started = rec.get("started_at")
            if (
                timeout is not None
                and started is not None
                and not rec.get("cancelled")
                and not rec.get("timed_out")
            ):
                try:
                    wall = float(timeout)
                except (TypeError, ValueError):
                    wall = None
                if wall is not None and wall >= 0 and (time.time() - float(started)) >= wall:
                    rec["timed_out"] = True
                    rec["assistant_error"] = rec.get("assistant_error") or "timeout"
                    self._kill_proc(proc)
                    self._harvest(rec)
                    return
            rec["activity"] = "busy"
            rec["state"] = rec.get("state") or "running"
            # Keep snapshot of drained text so large output never waits on poll.
            chunks_out = rec.get("stdout_chunks")
            chunks_err = rec.get("stderr_chunks")
            if isinstance(chunks_out, list):
                rec["stdout"] = "".join(chunks_out)
            if isinstance(chunks_err, list):
                rec["stderr"] = "".join(chunks_err)
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
        profile = self._agy_profile(rec)
        if profile:
            out["agy_profile"] = profile
        return _apply_contract_meta(out, rec)

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
        profile = self._agy_profile(rec)
        if profile:
            out["agy_profile"] = profile
        _apply_contract_meta(out, rec)
        self._attach_spawn_output(out, rec)
        self._persist_pool_after_collect(out, rec)
        return out

    def _attach_spawn_output(self, out: dict[str, Any], rec: dict[str, Any]) -> None:
        """Copy harvested streams onto the collect payload when absent.

        ``assistant_error`` is truncated. Classify needs the raw streams.
        """
        if "stdout" not in out:
            out["stdout"] = rec.get("stdout") or ""
        if "stderr" not in out:
            out["stderr"] = rec.get("stderr") or ""
        if "returncode" not in out:
            out["returncode"] = rec.get("returncode")

    def _persist_pool_after_collect(self, out: dict[str, Any], rec: dict[str, Any]) -> None:
        """Quota / rate-limit / auth classes update the selected account.

        Runs only when this spawn was bound to a pool (``COLLAB_AGY_ACCOUNT_POOL``
        and ``AGY_PROFILE``). Releases the busy lease so the next start_run in
        this process can select a different account. Pool I/O failures are logged
        by exception type and swallowed. Never logs stdout, stderr, or token material.
        """
        spawn = rec.get("spawn_environ")
        env: Mapping[str, str]
        if isinstance(spawn, Mapping) and spawn:
            env = spawn
        else:
            env = self._env()
        pool_path = str(env.get(ENV_POOL) or "").strip()
        profile = str(rec.get("agy_profile") or env.get(ENV_PROFILE) or "").strip()
        if not pool_path or not profile:
            return
        try:
            cls = finish_account_lease(
                pool_path,
                profile,
                out,
                lease_id=str(env.get(ENV_LEASE_ID) or "").strip() or None,
                persist=self._persist_pool,
                environ=env,
            )
            out["agy_err_class"] = cls
        except Exception as exc:  # noqa: BLE001 — pool I/O must not fail collect
            _LOG.warning("agy account pool update failed (%s)", type(exc).__name__)

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



_EXACT_CONTENT_RE = re.compile(
    r"(?P<path>\S+)\s+must\s+contain\s+exactly\s+(?P<body>.+)$",
    re.IGNORECASE,
)


def _charter_requests_lead_review(charter: dict | None) -> bool:
    if not isinstance(charter, dict):
        return False
    if bool(charter.get("force_lead_review")):
        return True
    acc = charter.get("acceptance")
    if isinstance(acc, str) and acc.strip():
        return True
    if isinstance(acc, dict) and acc:
        # done_when-style artifact lists alone are presence checks, not lead review.
        keys = {str(k).lower() for k in acc.keys()}
        if keys - {"artifacts", "files", "outputs"}:
            return True
    return False


def _check_exact_content_acceptance(workdir: Path, acceptance: str) -> tuple[bool, str]:
    """Validate "path must contain exactly BODY" acceptance strings."""
    m = _EXACT_CONTENT_RE.match(str(acceptance or "").strip())
    if not m:
        return True, ""
    rel = m.group("path").strip().strip("\"'")
    body = m.group("body").strip()
    path = Path(rel)
    if not path.is_absolute():
        path = workdir / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"acceptance_failed: cannot read {rel}: {e}"
    # Compare stripped single-line body by default; keep exact match when body has newlines.
    if "\n" in body or "\r" in body:
        ok = text == body
    else:
        ok = text.strip() == body.strip()
    if ok:
        return True, ""
    return False, (
        f"acceptance_failed: {rel} must contain exactly {body!r}; "
        f"got {text.strip()[:200]!r}"
    )


ACCEPTANCE_UNVERIFIED_WARNING = "acceptance text was not independently verified"
_REVIEW_EVIDENCE_MAX = 240


def _review_record(status: str, source: str, evidence: str = "") -> dict[str, str]:
    text = str(evidence or "").strip()
    if len(text) > _REVIEW_EVIDENCE_MAX:
        text = text[:_REVIEW_EVIDENCE_MAX]
    return {"status": status, "source": source, "evidence": text}


def _ensure_notes(result: dict[str, Any]) -> list[Any]:
    notes = result.get("notes")
    if not isinstance(notes, list):
        notes = []
        result["notes"] = notes
    return notes


def _append_result_warning(result: dict[str, Any], text: str) -> None:
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
        result["warnings"] = warnings
    if text not in warnings:
        warnings.append(text)


def _fail_acceptance(
    result: dict[str, Any],
    notes: list[Any],
    *,
    reason: str,
    source: str,
    note: str | None = None,
) -> dict[str, Any]:
    result["ok"] = False
    result["state"] = "fail"
    result["error"] = reason
    result["acceptance_failed"] = True
    result["review"] = _review_record("failed", source, reason)
    notes.append(note or reason)
    return result


def apply_agy_acceptance_gate(
    *,
    charter: dict | None,
    workdir: str | Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Enforce force_lead_review / acceptance on the agy path.

    Presence-only artifact checks already happen in collect_result. An
    exact-content acceptance string is checked here. Prose acceptance that
    nothing actually reviews is ``review.status=unsupported`` — never
    ``passed``. ``force_lead_review`` without a checkable criterion still
    fails closed. Never report ok=true for WRONG exact-content output.
    """
    if not isinstance(result, dict):
        return result
    if not isinstance(charter, dict):
        result["review"] = _review_record("not_requested", "none", "")
        return result
    acceptance = charter.get("acceptance")
    result["force_lead_review"] = bool(charter.get("force_lead_review"))
    notes = _ensure_notes(result)
    exact_text = acceptance.strip() if isinstance(acceptance, str) else ""
    exact = bool(exact_text and _EXACT_CONTENT_RE.match(exact_text))
    root = Path(workdir)

    if exact:
        ok_acc, reason = _check_exact_content_acceptance(root, exact_text)
        if not ok_acc:
            return _fail_acceptance(
                result, notes, reason=reason, source="agy_exact_content"
            )
        notes.append("agy acceptance: exact-content criteria passed")
        result["review"] = _review_record(
            "passed", "agy_exact_content", "exact-content criteria passed"
        )
        return result

    # force_lead_review without a checkable exact-content string: local
    # artifact_review when it can run, otherwise fail closed. Do not claim
    # the prose was independently verified.
    if bool(charter.get("force_lead_review")):
        try:
            from execution_backend.closed_loop import artifact_review

            review = artifact_review(
                charter=charter,
                workdir=root,
                collect=result,
                run_id=str(result.get("run_id") or ""),
                job_name_s=str(charter.get("name") or ""),
            )
            result["artifact_review"] = {
                "verdict": review.get("verdict"),
                "reason": review.get("reason"),
                "error_class": review.get("error_class"),
            }
            if str(review.get("verdict") or "").lower() != "pass":
                reason = str(
                    review.get("reason")
                    or review.get("error_class")
                    or "acceptance_failed"
                )
                return _fail_acceptance(
                    result,
                    notes,
                    reason=reason,
                    source="artifact_review",
                    note=f"agy artifact_review failed: {reason}",
                )
            notes.append("agy artifact_review: pass")
            result["review"] = _review_record(
                "passed", "artifact_review", "agy artifact_review: pass"
            )
            return result
        except Exception as e:  # noqa: BLE001 — fail closed on review wiring errors
            reason = (
                "force_lead_review unsupported on antigravity.cli_v1 without "
                f"checkable acceptance ({type(e).__name__})"
            )
            return _fail_acceptance(result, notes, reason=reason, source="none")

    requested = bool(exact_text) or _charter_requests_lead_review(charter)
    if requested:
        # Acceptance text (or another criterion) was asked for, and neither
        # exact-content nor artifact_review checked it. Do not mark it passed.
        result["review"] = _review_record(
            "unsupported", "none", ACCEPTANCE_UNVERIFIED_WARNING
        )
        _append_result_warning(result, ACCEPTANCE_UNVERIFIED_WARNING)
        notes.append(ACCEPTANCE_UNVERIFIED_WARNING)
        return result

    result["review"] = _review_record("not_requested", "none", "")
    return result


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
    # Keep charter artifacts workspace-relative. Resolving against ``root`` and
    # then passing the result back as a relative artifact double-prefixed a
    # relative --workspace (``ws/ws/x``) and ``Path.name`` dropped subdirs, so
    # collect_result reported artifacts=[] while the file existed. Absolute
    # charter paths are still mapped under root by start_run's sanitizer.
    try:
        arts = resolve_arts(charter, workspace=None)
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
    result = apply_agy_acceptance_gate(charter=charter, workdir=root, result=result)
    return result


__all__ = [
    "BACKEND_ID",
    "DEFAULT_AGY_MODEL",
    "SKIP_PERMISSIONS_FLAG",
    "ACCEPTANCE_UNVERIFIED_WARNING",
    "AntigravityCliExecutionBackend",
    "agy_auto_approve_enabled",
    "apply_agy_acceptance_gate",
    "artifacts_from_charter",
    "build_agy_argv",
    "build_agy_prompt",
    "parse_agy_json",
    "resolve_agy_bin",
    "resolve_agy_model",
    "run_antigravity_job_via_public_api",
]
