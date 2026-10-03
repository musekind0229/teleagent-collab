"""cgroup v2 OOM evidence for processes the service started (Linux only).

A SIGKILL alone does not say who killed a worker. When the kernel's cgroup
OOM killer did it, the cgroup's ``memory.events`` ``oom_kill`` counter goes
up. Record the counter when a run starts and compare when it ends.

``memory.events`` counts the cgroup and its descendants, so with several runs
in one service cgroup the increase cannot name which run's process was the
victim; the reason says "in this service's cgroup". Non-Linux hosts and cgroup
v1 return None everywhere (no claim is made).
"""

from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path

OOM_REASON = "killed by OOM (cgroup memory limit)"
_CGROUP_ROOT = Path("/sys/fs/cgroup")


def cgroup_dir(pid: int | str = "self", *, root: Path = _CGROUP_ROOT) -> Path | None:
    """cgroup v2 directory of ``pid`` that has ``memory.events``, or None."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip().lstrip("/")
            path = root / rel if rel else root
            if (path / "memory.events").is_file():
                return path
            return None
    return None


def oom_kill_count(directory: str | Path | None) -> int | None:
    """``oom_kill`` from ``memory.events`` (hierarchical), or None."""
    if not directory:
        return None
    try:
        text = (Path(directory) / "memory.events").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "oom_kill":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def memory_max(directory: str | Path | None) -> str:
    """``memory.max`` as written by the kernel (bytes or ``max``), or ""."""
    if not directory:
        return ""
    try:
        return (Path(directory) / "memory.max").read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


def killed_by_sigkill(returncode: int | None) -> bool:
    """Popen reports a signal as ``-N``; a shell wrapper reports ``128+N``."""
    sigkill = int(getattr(signal, "SIGKILL", 9))
    return returncode in (-sigkill, 128 + sigkill)


def oom_note(directory: str | Path | None, before: int | None, returncode: int | None) -> str | None:
    """Reason text when this cgroup's OOM killer fired during the run, else None."""
    after = oom_kill_count(directory)
    if before is None or after is None or after <= before:
        return None
    delta = after - before
    limit = memory_max(directory)
    where = f"memory.max={limit}" if limit else "cgroup"
    if killed_by_sigkill(returncode):
        return f"{OOM_REASON}: worker exit={returncode}, oom_kill +{delta} in this service's cgroup ({where})"
    # The worker itself exited on its own, but something in the cgroup (most
    # likely a tool it started) was OOM-killed meanwhile.
    return (
        f"{OOM_REASON} likely: a process in this service's cgroup was OOM-killed during the run "
        f"(oom_kill +{delta}, {where}); worker exit={returncode}"
    )


# ---- ledger: which oom_kill increases the main process already explained ----
# A service main process killed by the OOM killer cannot report itself, and
# systemd only says result=oom-kill under OOMPolicy=stop (under continue it is
# result=signal). ExecStopPost runs in the same cgroup before it is removed, so
# it can compare memory.events with what the main process already attributed
# to OOM-killed workers. Only the unexplained remainder points at the main one.
LEDGER_NAME = "oom-ledger.json"
_LEDGER: Path | None = None
_LEDGER_LOCK = threading.Lock()


def _read_json(path: Path) -> dict:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def _write_json(path: Path, row: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(row), encoding="utf-8")
    os.replace(tmp, path)


def start_ledger(persist: str | Path, directory: str | Path | None = None) -> dict | None:
    """Main process start: everything counted so far is not ours to explain."""
    global _LEDGER
    d = directory if directory is not None else cgroup_dir("self")
    count = oom_kill_count(d)
    if count is None:
        _LEDGER = None
        return None
    path = Path(persist) / LEDGER_NAME
    row = {"cgroup": str(d), "explained_upto": count, "pid": os.getpid()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, row)
    except OSError:
        _LEDGER = None
        return None
    _LEDGER = path
    return row


def mark_explained(count: int | None) -> None:
    """A worker OOM was reported for every oom_kill up to ``count``."""
    if _LEDGER is None or count is None:
        return
    with _LEDGER_LOCK:
        row = _read_json(_LEDGER)
        if int(row.get("explained_upto") or 0) >= count:
            return
        row["explained_upto"] = count
        try:
            _write_json(_LEDGER, row)
        except OSError:
            pass


def unexplained_oom_kills(persist: str | Path, directory: str | Path | None = None) -> int | None:
    """oom_kill increases in this cgroup that no worker OOM report accounted for."""
    d = directory if directory is not None else cgroup_dir("self")
    now = oom_kill_count(d)
    if now is None:
        return None
    row = _read_json(Path(persist) / LEDGER_NAME)
    base = int(row.get("explained_upto") or 0) if row.get("cgroup") == str(d) else 0
    return max(0, now - base)


def is_linux() -> bool:
    return os.name == "posix" and Path("/proc/self/cgroup").exists()


__all__ = [
    "OOM_REASON",
    "LEDGER_NAME",
    "cgroup_dir",
    "oom_kill_count",
    "memory_max",
    "killed_by_sigkill",
    "oom_note",
    "start_ledger",
    "mark_explained",
    "unexplained_oom_kills",
]
