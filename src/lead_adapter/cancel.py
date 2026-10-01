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
from collections.abc import Iterator, Sequence
from typing import Any

CREATE_NEW_PROCESS_GROUP = 0x00000200
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
    while True:
        remaining = deadline - time.monotonic()
        try:
            out, err = proc.communicate(input=input_text, timeout=max(0.0, min(_POLL_SEC, remaining)))
            if scope is not None and scope.cancelled:
                raise LeadCancelled(scope.reason)
            return out or "", err or ""
        except subprocess.TimeoutExpired:
            pass
        if scope is not None and scope.cancelled:
            stop_process_tree(proc, platform)  # normally already done by cancel()
            _drain(proc)
            raise LeadCancelled(scope.reason)
        if time.monotonic() >= deadline:
            stop_process_tree(proc, platform)
            _drain(proc)
            raise subprocess.TimeoutExpired(proc.args, timeout)


def _drain(proc: subprocess.Popen) -> None:
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def run_cancellable(
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
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        **popen_group_kwargs(platform),
    )
    if scope is not None:
        scope.attach(proc, platform)
    try:
        out, err = communicate(proc, timeout=timeout, input_text=input_text, platform=platform, scope=scope)
    finally:
        if scope is not None:
            scope.detach(proc)
    return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)


__all__ = [
    "CancelScope",
    "LeadCancelled",
    "communicate",
    "current_scope",
    "popen_group_kwargs",
    "run_cancellable",
    "stop_process_tree",
    "use_scope",
]
