"""Durable record of agy worker processes, so a restart can stop orphans.

agy runs are one-shot child processes whose pipes belong to the service that
spawned them. After the service dies, a surviving child can no longer be
collected, but it keeps writing into the task workspace. The registry records
just enough to find that child again (pid, process group, a start-time token
that defeats pid reuse, and the owning service process), never argv, prompt or
environment.

On start, ``reap_orphans`` stops every recorded worker whose owner is gone:
POSIX kills the whole process group (agy is spawned with
``start_new_session=True``); Windows uses ``taskkill /F /T /PID`` on the
recorded root. A pid whose start token no longer matches is someone else's
process and is left alone.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

REGISTRY_VERSION = 1
ENV_RUN_REGISTRY = "COLLAB_AGY_RUN_REGISTRY"
_REAPED_KEEP_SEC = 24 * 3600


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_start_token(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            ok = kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
            if not ok:
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
                return None  # exited (STILL_ACTIVE == 259)
            return f"win:{created}"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _proc_stat_start(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # comm may contain spaces and parens; fields after the last ')' are fixed.
    tail = raw.rsplit(")", 1)[-1].split()
    # tail[0] is field 3 (state); starttime is field 22 -> tail[19].
    if len(tail) < 20:
        return None
    if tail[0] == "Z":
        return None
    return f"linux:{tail[19]}"


def _ps_start(pid: int) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"ps:{out}" if out else None


def process_start_token(pid: int) -> str | None:
    """Opaque per-process identity (start time). None when not running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if _is_windows():
        return _windows_start_token(pid)
    if Path("/proc/self/stat").exists():
        return _proc_stat_start(pid)
    return _ps_start(pid)


def _proc_table() -> dict[int, tuple[int, int, int, str]]:
    """pid -> (ppid, pgid, sid, state) from /proc. Empty when /proc is absent."""
    table: dict[int, tuple[int, int, int, str]] = {}
    proc = Path("/proc")
    if not (proc / "self" / "stat").exists():
        return table
    try:
        names = [p.name for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return table
    for name in names:
        try:
            tail = (proc / name / "stat").read_text(encoding="ascii", errors="replace").rsplit(")", 1)[-1].split()
            table[int(name)] = (int(tail[1]), int(tail[2]), int(tail[3]), tail[0])
        except (OSError, ValueError, IndexError):
            continue
    return table


def descendant_pids(pid: int, table: Mapping[int, tuple[int, int, int, str]] | None = None) -> list[int]:
    """Live (non-zombie) descendants of ``pid`` by parent links. Linux only; else []."""
    rows = _proc_table() if table is None else table
    children: dict[int, list[int]] = {}
    for child, (ppid, _pg, _sid, _st) in rows.items():
        children.setdefault(ppid, []).append(child)
    out: list[int] = []
    stack = list(children.get(int(pid), []))
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        if rows.get(cur, (0, 0, 0, "Z"))[3] != "Z":
            out.append(cur)
        stack.extend(children.get(cur, []))
    return sorted(out)


def _posix_targets(pid: int, pgid: int | None, children: list[Mapping[str, Any]] | None) -> tuple[set[int], set[int]]:
    """(pids, pgids) to signal: the group, the session, descendants, recorded children.

    agy runs tool commands in their own process group, so killing only the
    worker's group leaves e.g. a ``sleep``/build step running. Recorded
    children are only used while their start token still matches.
    """
    table = _proc_table()
    pids: set[int] = {int(pid)}
    groups: set[int] = {int(pgid or pid)}
    sid = int(pgid or pid)  # spawned with start_new_session: sid == pid
    for other, (_pp, grp, osid, state) in table.items():
        if state != "Z" and (osid == sid or grp in groups):
            pids.add(other)
    pids.update(descendant_pids(int(pid), table))
    for row in children or []:
        cpid = row.get("pid") if isinstance(row, Mapping) else None
        token = str(row.get("start_token") or "") if isinstance(row, Mapping) else ""
        if isinstance(cpid, int) and cpid > 0 and token and process_start_token(cpid) == token:
            pids.add(cpid)
            pids.update(descendant_pids(cpid, table))
    for other in list(pids):
        if other in table and table[other][3] != "Z":
            groups.add(table[other][1])
    groups.discard(0)
    groups.discard(os.getpgid(0) if hasattr(os, "getpgid") else -1)  # never our own group
    pids.discard(os.getpid())
    return pids, groups


def _posix_any_alive(pids: set[int]) -> bool:
    table = _proc_table()
    if table:
        return any(p in table and table[p][3] != "Z" for p in pids)
    alive = False
    for p in pids:
        try:
            os.kill(p, 0)
            alive = True
        except OSError:
            continue
    return alive


def _posix_group_alive(pgid: int) -> bool:
    """Any non-zombie member left in the group. Zombies cannot write files."""
    proc = Path("/proc")
    if (proc / "self" / "stat").exists():
        try:
            names = [p.name for p in proc.iterdir() if p.name.isdigit()]
        except OSError:
            names = []
        for name in names:
            try:
                tail = (proc / name / "stat").read_text(encoding="ascii", errors="replace").rsplit(")", 1)[-1].split()
            except OSError:
                continue
            if len(tail) > 2 and tail[2] == str(int(pgid)) and tail[0] != "Z":
                return True
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def kill_process_tree(
    pid: int,
    *,
    pgid: int | None = None,
    wait_sec: float = 3.0,
    children: list[Mapping[str, Any]] | None = None,
) -> bool:
    """Stop a recorded worker tree. True when nothing of it is left running.

    POSIX: SIGTERM then SIGKILL to the worker's group and session, every
    descendant and every recorded child (their own groups too). Windows:
    ``taskkill /F /T`` on the root, then on recorded children still alive.
    """
    if _is_windows():
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 15, "check": False}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
        roots = [int(pid)]
        for row in children or []:
            cpid = row.get("pid") if isinstance(row, Mapping) else None
            token = str(row.get("start_token") or "") if isinstance(row, Mapping) else ""
            if isinstance(cpid, int) and cpid > 0 and token and _windows_start_token(cpid) == token:
                roots.append(cpid)
        for root in roots:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(root)], **kwargs)
            except (OSError, subprocess.SubprocessError):
                pass
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if _windows_start_token(int(pid)) is None:
                return True
            time.sleep(0.1)
        return _windows_start_token(int(pid)) is None
    pids, groups = _posix_targets(int(pid), pgid, children)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for grp in groups:
            try:
                os.killpg(grp, sig)
            except OSError:
                pass
        for target in pids:
            try:
                os.kill(target, sig)
            except OSError:
                pass
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if not _posix_any_alive(pids) and not any(_posix_group_alive(g) for g in groups):
                return True
            time.sleep(0.05)
    return not _posix_any_alive(pids)


class AgyRunRegistry:
    """JSON file of live agy runs for one service persist directory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.owner_pid = os.getpid()
        self.owner_token = process_start_token(self.owner_pid) or ""

    # -- file io -------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": REGISTRY_VERSION, "runs": {}, "reaped": {}}
        if not isinstance(data, dict):
            return {"version": REGISTRY_VERSION, "runs": {}, "reaped": {}}
        data.setdefault("runs", {})
        data.setdefault("reaped", {})
        if not isinstance(data["runs"], dict):
            data["runs"] = {}
        if not isinstance(data["reaped"], dict):
            data["reaped"] = {}
        return data

    def _save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- run lifecycle -------------------------------------------------
    def record(self, run_id: str, *, pid: int, directory: str = "") -> None:
        entry = {
            "pid": int(pid),
            "pgid": int(pid) if not _is_windows() else None,
            "start_token": process_start_token(int(pid)) or "",
            "owner_pid": self.owner_pid,
            "owner_token": self.owner_token,
            "directory": str(directory or ""),
            "started_at": time.time(),
            "platform": sys.platform,
        }
        with self._lock:
            data = self._load()
            data["runs"][str(run_id)] = entry
            self._save(data)

    def record_children(self, run_id: str, pids: list[int]) -> None:
        """Remember the worker's tool processes (they may live in other groups)."""
        rows = []
        for cpid in pids[:64]:
            token = process_start_token(int(cpid))
            if token:
                rows.append({"pid": int(cpid), "start_token": token})
        with self._lock:
            data = self._load()
            entry = data["runs"].get(str(run_id))
            if not isinstance(entry, dict):
                return
            known = {(r.get("pid"), r.get("start_token")) for r in entry.get("children") or [] if isinstance(r, dict)}
            merged = list(entry.get("children") or [])
            for row in rows:
                if (row["pid"], row["start_token"]) not in known:
                    merged.append(row)
            entry["children"] = merged[-64:]
            self._save(data)

    def forget(self, run_id: str) -> None:
        with self._lock:
            data = self._load()
            if data["runs"].pop(str(run_id), None) is not None:
                self._save(data)

    def mark_stopped(self, run_id: str, outcome: str) -> None:
        """This service stopped the run itself (shutdown); explain it after restart."""
        with self._lock:
            data = self._load()
            entry = data["runs"].pop(str(run_id), None)
            if entry is None:
                return
            entry["outcome"] = outcome
            entry["reaped_at"] = time.time()
            data["reaped"][str(run_id)] = entry
            self._save(data)

    def reaped(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._load()["reaped"].items() if isinstance(v, dict)}

    # -- restart -------------------------------------------------------
    def reap_orphans(
        self,
        *,
        killer: Callable[..., bool] = kill_process_tree,
        token_of: Callable[[int], str | None] = process_start_token,
    ) -> list[dict[str, Any]]:
        """Stop workers recorded by a dead service. Returns one row per run."""
        rows: list[dict[str, Any]] = []
        with self._lock:
            data = self._load()
            now = time.time()
            for run_id, entry in list(data["runs"].items()):
                if not isinstance(entry, dict):
                    data["runs"].pop(run_id, None)
                    continue
                owner_pid = entry.get("owner_pid")
                owner_token = str(entry.get("owner_token") or "")
                if isinstance(owner_pid, int) and owner_pid != self.owner_pid and owner_token:
                    if token_of(owner_pid) == owner_token:
                        continue  # owner service is alive; not an orphan
                pid = entry.get("pid")
                outcome = "already_exited"
                if isinstance(pid, int) and pid > 0:
                    current = token_of(pid)
                    recorded = str(entry.get("start_token") or "")
                    if current is not None and recorded and current != recorded:
                        outcome = "pid_reused_left_alone"
                    elif current is not None:
                        ok = killer(pid, pgid=entry.get("pgid"), children=entry.get("children"))
                        outcome = "killed" if ok else "kill_failed"
                    else:
                        # Worker gone; its group, session or recorded tool
                        # children may still be running.
                        kids = [
                            r for r in entry.get("children") or []
                            if isinstance(r, dict) and isinstance(r.get("pid"), int)
                            and r.get("start_token") and token_of(int(r["pid"])) == r.get("start_token")
                        ]
                        group_alive = bool(entry.get("pgid")) and not _is_windows() and _posix_group_alive(int(entry["pgid"]))
                        if kids or group_alive:
                            ok = killer(pid, pgid=entry.get("pgid"), children=kids)
                            outcome = "killed" if ok else "kill_failed"
                entry["outcome"] = outcome
                entry["reaped_at"] = now
                data["runs"].pop(run_id, None)
                data["reaped"][run_id] = entry
                rows.append({"run_id": run_id, "pid": pid, "outcome": outcome})
            for run_id, entry in list(data["reaped"].items()):
                at = entry.get("reaped_at") if isinstance(entry, dict) else None
                if not isinstance(at, (int, float)) or now - float(at) > _REAPED_KEEP_SEC:
                    data["reaped"].pop(run_id, None)
            if rows or self.path.exists():
                self._save(data)
        return rows


_PREVIOUS_EXIT_NOTE = ""


def set_previous_exit_note(note: str) -> None:
    """Why the previous service process ended (e.g. OOM); appended to reap reasons."""
    global _PREVIOUS_EXIT_NOTE
    _PREVIOUS_EXIT_NOTE = str(note or "").strip()


def describe_reaped(run_id: str, entry: Mapping[str, Any]) -> str:
    """One line for the task failure. No paths, argv or env."""
    outcome = str(entry.get("outcome") or "unknown")
    pid = entry.get("pid")
    text = {
        "killed": "its worker process tree was stopped when the service restarted",
        "stopped_at_shutdown": "its worker process tree was stopped when the service shut down",
        "already_exited": "its worker had already exited; output was lost with the old service",
        "pid_reused_left_alone": "its pid now belongs to another process, which was left alone",
        "kill_failed": "stopping its worker failed; it may still be running",
    }.get(outcome, outcome)
    cause = f" ({_PREVIOUS_EXIT_NOTE})" if _PREVIOUS_EXIT_NOTE and outcome != "stopped_at_shutdown" else ""
    return (
        f"agy run {run_id} (pid {pid}) belonged to a previous service process{cause}; {text}. "
        "Its result was not collected; artifacts already in the workspace are candidates only"
    )


__all__ = [
    "AgyRunRegistry",
    "ENV_RUN_REGISTRY",
    "descendant_pids",
    "describe_reaped",
    "set_previous_exit_note",
    "kill_process_tree",
    "process_start_token",
]
