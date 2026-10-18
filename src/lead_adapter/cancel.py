"""Cancellable lead subprocesses (refs #5 #13).

A planner call runs on its own thread. When the Goal is cancelled the
coordinator cancels the thread's ``CancelScope``; every lead process started
under that scope is stopped with its whole tree:

* POSIX: the lead runs in its own session/process group; SIGTERM to the
  group, then SIGKILL after a short grace.
* Windows: the lead runs in a new process group; ``taskkill /F /T /PID``.

Adapters spawn through ``run_cancellable`` (or ``attach`` + ``communicate``)
so a cancelled scope never leaves a lead or its children running. No argv,
prompt or environment is recorded, only pids and how they were stopped.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

from platform_services import win_job

CREATE_NEW_PROCESS_GROUP = 0x00000200

# Service-wide record of live lead processes (``<persist>/lead-runs.json``),
# so a service killed with SIGKILL can stop leftover lead trees on restart.
_REGISTRY: Any = None
_REGISTRY_LOCK = threading.Lock()


def set_process_registry(registry: Any) -> None:
    """Install (or clear with None) the registry lead processes are recorded in.

    Any object with ``record(run_id, pid=...)`` and ``forget(run_id)`` works;
    the service uses ``AgyRunRegistry`` on its own file (pid, process group,
    start token, owning service) so pid reuse is never killed by mistake.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = registry


def register_process(proc: subprocess.Popen) -> str | None:
    with _REGISTRY_LOCK:
        registry = _REGISTRY
    if registry is None:
        return None
    key = f"lead_{uuid.uuid4().hex[:12]}"
    try:
        registry.record(key, pid=int(proc.pid))
    except Exception:  # noqa: BLE001 — bookkeeping must never break a lead call
        return None
    return key


def unregister_process(key: str | None) -> None:
    if not key:
        return
    with _REGISTRY_LOCK:
        registry = _REGISTRY
    if registry is None:
        return
    try:
        registry.forget(key)
    except Exception:  # noqa: BLE001
        pass
_POLL_SEC = 0.2
_GRACE_SEC = 1.5


class LeadCancelled(RuntimeError):
    """The scope this lead call ran under was cancelled."""


def _is_windows(platform: str | None) -> bool:
    return str(platform if platform is not None else sys.platform).startswith("win")


def popen_group_kwargs(platform: str | None = None) -> dict[str, Any]:
    """Start the lead as its own group so the whole tree can be stopped."""
    if _is_windows(platform):
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process_tree(
    proc: subprocess.Popen, platform: str | None = None, *, grace_sec: float = _GRACE_SEC
) -> dict[str, Any]:
    """Stop ``proc`` and its descendants. Never raises. Returns what was done."""
    row: dict[str, Any] = {"pid": int(proc.pid)}
    if proc.poll() is not None:
        row.update(method="none", result="already_exited")
        return row
    if _is_windows(platform):
        row["method"] = "taskkill /F /T"
        if win_job.terminate(proc):
            row["job_object"] = "terminated"
        try:
            done = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
            row["taskkill_exit"] = int(done.returncode)
        except Exception as exc:  # noqa: BLE001
            row["taskkill_error"] = type(exc).__name__
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=grace_sec + 3)
            row["result"] = "stopped"
        except Exception:  # noqa: BLE001
            row["result"] = "still_running"
        return row
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = 0
    own = os.getpgid(0) if hasattr(os, "getpgid") else -1
    if pgid and pgid != own:
        row["method"] = "killpg"
        row["pgid"] = pgid

        def _signal(sig: int) -> None:
            os.killpg(pgid, sig)
    else:  # not started in its own group: never signal the service's group
        row["method"] = "kill"

        def _signal(sig: int) -> None:
            os.kill(proc.pid, sig)
    try:
        _signal(signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_sec)
        row["result"] = "terminated"
    except subprocess.TimeoutExpired:
        try:
            _signal(signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
            row["result"] = "killed"
        except subprocess.TimeoutExpired:
            row["result"] = "still_running"
    if pgid and pgid != own:
        # Children that ignored TERM or outlived the leader still share the group.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return row


class CancelScope:
    """Cancellation handle for one planner call (one thread)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: list[tuple[subprocess.Popen, str | None]] = []
        self._event = threading.Event()
        self.reason = ""
        self.stopped: list[dict[str, Any]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def attach(self, proc: subprocess.Popen, platform: str | None = None) -> None:
        with self._lock:
            if not self._event.is_set():
                self._procs.append((proc, platform))
                return
        # Cancelled before the lead got registered: stop it right away.
        self.stopped.append(stop_process_tree(proc, platform))

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs = [(p, plat) for p, plat in self._procs if p is not proc]

    def cancel(self, reason: str = "cancelled") -> list[dict[str, Any]]:
        """Stop every attached lead tree. Idempotent. Returns this call's stops."""
        with self._lock:
            if not self._event.is_set():
                self.reason = str(reason or "cancelled")[:200]
                self._event.set()
            procs, self._procs = self._procs, []
        rows = [stop_process_tree(p, plat) for p, plat in procs]
        self.stopped.extend(rows)
        return rows


_CURRENT: contextvars.ContextVar[CancelScope | None] = contextvars.ContextVar("collab_lead_cancel_scope", default=None)


def current_scope() -> CancelScope | None:
    return _CURRENT.get()


@contextlib.contextmanager
def use_scope(scope: CancelScope | None) -> Iterator[CancelScope | None]:
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def communicate(
    proc: subprocess.Popen,
    *,
    timeout: float,
    input_text: str | None = None,
    platform: str | None = None,
    scope: CancelScope | None = None,
) -> tuple[str, str]:
    """``proc.communicate`` that also returns early when ``scope`` is cancelled.

    Raises ``LeadCancelled`` (tree already stopped) or
    ``subprocess.TimeoutExpired`` (tree stopped) like ``subprocess.run`` would.
    """
    deadline = time.monotonic() + float(timeout)
    pending_input = input_text
    while True:
        remaining = deadline - time.monotonic()
        try:
            # Input goes in on the first call only; CPython keeps feeding it on
            # retries and raises ValueError if it is passed again.
            send, pending_input = pending_input, None
            out, err = proc.communicate(input=send, timeout=max(0.0, min(_POLL_SEC, remaining)))
            if scope is not None and scope.cancelled:
                exc = LeadCancelled(scope.reason)
                setattr(exc, 'output', out)
                setattr(exc, 'stderr', err)
                setattr(exc, 'cleanup', {**stop_process_tree(proc, platform),
                    'drain_complete': True, 'scope_cancelled': True})
                raise exc
            return (out if out is not None else (b"" if not proc.text_mode else "")), (err if err is not None else (b"" if not proc.text_mode else ""))
        except subprocess.TimeoutExpired as poll_timeout:
            # CPython exposes accumulated bytes on TimeoutExpired. Retain them
            # if post-stop draining cannot finish (e.g. inherited pipe handles).
            partial_out, partial_err = poll_timeout.output, poll_timeout.stderr
        if (scope is not None and scope.cancelled) or time.monotonic() >= deadline:
            cleanup = stop_process_tree(proc, platform)
            out, err, drained = _drain(proc, partial_out, partial_err)
            if scope is not None and scope.cancelled:
                exc = LeadCancelled(scope.reason)
                setattr(exc, 'output', out)
                setattr(exc, 'stderr', err)
            else:
                exc = subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err)
            setattr(exc, 'cleanup', {**cleanup, 'drain_complete': drained})
            raise exc


def _drain(proc: subprocess.Popen, partial_out=None, partial_err=None):
    try:
        out, err = proc.communicate(timeout=5)
        return out, err, True
    except subprocess.TimeoutExpired as exc:
        return (exc.output if exc.output is not None else partial_out,
                exc.stderr if exc.stderr is not None else partial_err, False)
    except Exception:  # noqa: BLE001
        return partial_out, partial_err, False


def _run_unaccounted(
    cmd: Sequence[str],
    *,
    timeout: float,
    input_text: str | None = None,
    platform: str | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True, text=True)`` for lead CLIs, but the
    lead gets its own process group and the current ``CancelScope`` can stop it."""
    scope = current_scope()
    if scope is not None and scope.cancelled:
        raise LeadCancelled(scope.reason)
    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        cwd=cwd,
        **popen_group_kwargs(platform),
    )
    admission = _SPAWN_ADMISSION.get()
    if admission is not None:
        hook, binding, state = admission
        state["spawned"] = True
        try:
            import psutil
            hook.actual(state["reservation"], pid=proc.pid, birth=psutil.Process(proc.pid).create_time())
        except BaseException:
            proc.kill()
            proc.wait(timeout=5)
            raise
    # Windows: kill-on-close Job Object, so a dead service takes the tree with it.
    win_job.attach(proc, platform)
    key = register_process(proc)
    if scope is not None:
        scope.attach(proc, platform)
    try:
        out, err = communicate(proc, timeout=timeout, input_text=input_text.encode("utf-8") if input_text is not None else None, platform=platform, scope=scope)
    except (LeadCancelled, subprocess.TimeoutExpired) as exc:
        try:
            partial_out, partial_err = getattr(exc, 'output', None), getattr(exc, 'stderr', None)
            if partial_out is None or partial_err is None:
                partial_out, partial_err = proc.communicate(timeout=3)
            captured = _binary_capture_result(cmd, proc, partial_out, partial_err, complete=False)
            evidence = getattr(captured, 'raw_evidence')
            evidence['cleanup'] = getattr(exc, 'cleanup', {'result': 'not_recorded'})
            setattr(exc, 'raw_evidence', evidence)
        except Exception:
            exc.raw_evidence = {"capture_status": "not_recorded", "capture_complete": False}
        raise
    finally:
        if scope is not None:
            scope.detach(proc)
        unregister_process(key)
        win_job.close(proc)
    return _binary_capture_result(cmd, proc, out, err)


def _binary_capture_result(cmd, proc, out, err, *, complete=True):
    # Persist original binary streams BEFORE decoding or parsing model fields.
    import hashlib, json, os
    from pathlib import Path
    captured = isinstance(out, bytes) and isinstance(err, bytes)
    evidence = {"capture_status": "not_recorded", "capture_complete": False}
    if captured:
        root = Path(os.environ.get("TMPDIR") or Path.cwd()) / ".collab-lead-raw"
        root.mkdir(parents=True, exist_ok=True)
        streams = {}
        for name, data in (("stdout", out), ("stderr", err)):
            digest = hashlib.sha256(data).hexdigest()
            path = root / (digest + ".bin")
            try:
                with path.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError("raw stream digest collision")
            streams[name] = {"path": str(path), "bytes": len(data), "sha256": digest}
        evidence = {"capture_status": "original_binary", "capture_complete": complete,
                    "returncode": proc.returncode, "streams": streams}
        receipt = json.dumps(evidence, sort_keys=True).encode("utf-8")
        receipt_path = root / (hashlib.sha256(receipt).hexdigest() + ".json")
        try:
            with receipt_path.open("xb") as stream:
                stream.write(receipt)
        except FileExistsError:
            if receipt_path.read_bytes() != receipt:
                raise ValueError("raw receipt collision")
        evidence["receipt_path"] = str(receipt_path)
    result = subprocess.CompletedProcess(list(cmd), proc.returncode,
                out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out,
                err.decode("utf-8", errors="replace") if isinstance(err, bytes) else err)
    result.raw_evidence = evidence
    return result


__all__ = [
    "CancelScope",
    "LeadCancelled",
    "communicate",
    "current_scope",
    "popen_group_kwargs",
    "register_process",
    "set_process_registry",
    "unregister_process",
    "run_cancellable",
    "stop_process_tree",
    "use_scope",
]

# Internal per-thread/context service boundary. HTTP bodies never create hooks.
_SPAWN_ADMISSION = contextvars.ContextVar("trusted_lead_spawn_admission", default=None)

@contextlib.contextmanager
def trusted_spawn_admission(hook, binding):
    token = _SPAWN_ADMISSION.set((hook, dict(binding), {}))
    try:
        yield
    finally:
        _SPAWN_ADMISSION.reset(token)

def run_cancellable(cmd, **kwargs):
    admission = _SPAWN_ADMISSION.get()
    if admission is None:
        return _run_unaccounted(cmd, **kwargs)
    hook, binding, state = admission
    # Every actual adapter subprocess attempt gets an independent reservation.
    state.clear()
    state["reservation"] = hook.reserve("lead", binding)
    state["spawned"] = False
    try:
        return _run_unaccounted(cmd, **kwargs)
    finally:
        if not state["spawned"]:
            hook.no_spawn(state["reservation"])
