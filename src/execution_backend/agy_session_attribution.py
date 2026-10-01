"""Tie agy session files to one worker run before using them as a heartbeat (refs #5 #10).

agy keeps per-conversation files under ``<HOME|USERPROFILE>/.gemini/antigravity-cli``:
``conversations/<uuid>.db`` and ``brain/<uuid>/`` (plus ``log/cli-*.log``).
Several agy processes may share one HOME (a second service, a manual
``agy`` session, a pool that shares USERPROFILE). A file is used for a run's
heartbeat only when it can be tied to that run:

1. ``open_handle``: the run's process tree holds the file open
   (``/proc/<pid>/fd``; Linux only). The conversation uuid / log file is then
   claimed for that run and keeps counting after the handle closes.
2. ``new_file``: exactly one conversation appeared after the run started, no
   other run of this service uses the same HOME, and no other process holds
   it open (checked where ``/proc`` exists).

Anything else is not used and the reason is reported. Only names, mtimes and
fd link targets are read, never file contents. Shared files such as
``conversation_summaries.db`` are never attributed.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_MAX_ENTRIES = 400
_MAX_PROCS = 4000
_PROC = Path("/proc")


def session_base(environ: Mapping[str, Any] | None) -> Path | None:
    env = environ if isinstance(environ, Mapping) else {}
    home = str(env.get("HOME") or env.get("USERPROFILE") or "").strip()
    if not home:
        return None
    return Path(home) / ".gemini" / "antigravity-cli"


def stem_of(path: str, base: Path) -> str | None:
    """Claim key for a session path, or None for shared / unrelated files."""
    try:
        rel = Path(path).resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "conversations":
        stem = parts[1].split(".", 1)[0]
        return stem or None
    if len(parts) >= 2 and parts[0] == "brain":
        return parts[1] or None
    if len(parts) == 2 and parts[0] == "log":
        return "log/" + parts[1]
    return None


def list_stems(base: Path) -> set[str]:
    """Conversation stems currently present (conversations/*.db*, brain/*)."""
    out: set[str] = set()
    for sub in ("conversations", "brain"):
        try:
            entries = list(os.scandir(base / sub))[:_MAX_ENTRIES]
        except OSError:
            continue
        for entry in entries:
            stem = entry.name.split(".", 1)[0] if sub == "conversations" else entry.name
            if stem:
                out.add(stem)
    return out


def stem_mtime(base: Path, stem: str) -> float | None:
    """Newest mtime of the entries that belong to ``stem``."""
    paths: list[Path] = []
    if stem.startswith("log/"):
        paths.append(base / stem)
    else:
        try:
            for entry in list(os.scandir(base / "conversations"))[:_MAX_ENTRIES]:
                if entry.name.split(".", 1)[0] == stem:
                    paths.append(Path(entry.path))
        except OSError:
            pass
        brain = base / "brain" / stem
        paths.append(brain)
        try:
            paths.extend(Path(e.path) for e in list(os.scandir(brain))[:200])
        except OSError:
            pass
    latest: float | None = None
    for path in paths:
        try:
            if path.is_symlink():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest


def fd_scan_available() -> bool:
    return _PROC.is_dir() and (_PROC / "self" / "fd").is_dir()


def _fd_targets(pid: int) -> list[str]:
    try:
        names = os.listdir(_PROC / str(pid) / "fd")
    except OSError:
        return []
    out = []
    for name in names[:_MAX_ENTRIES]:
        try:
            out.append(os.readlink(_PROC / str(pid) / "fd" / name))
        except OSError:
            continue
    return out


def stems_held_by(pids: Iterable[int], base: Path) -> set[str]:
    held: set[str] = set()
    for pid in pids:
        for target in _fd_targets(int(pid)):
            stem = stem_of(target, base)
            if stem:
                held.add(stem)
    return held


def holders_outside(stem: str, base: Path, own: set[int]) -> int:
    """How many processes outside ``own`` hold a file of ``stem`` open."""
    count = 0
    try:
        pids = [int(n) for n in os.listdir(_PROC) if n.isdigit()][:_MAX_PROCS]
    except OSError:
        return 0
    for pid in pids:
        if pid in own:
            continue
        if any(stem_of(t, base) == stem for t in _fd_targets(pid)):
            count += 1
    return count


class SessionAttributor:
    """Per-backend claims so one conversation feeds exactly one run."""

    def __init__(self, *, fd_scan: bool | None = None) -> None:
        self._lock = threading.Lock()
        self._claims: dict[tuple[str, str], str] = {}
        self.fd_scan = fd_scan_available() if fd_scan is None else bool(fd_scan)

    def baseline(self, environ: Mapping[str, Any] | None) -> list[str]:
        base = session_base(environ)
        return sorted(list_stems(base)) if base is not None else []

    def release(self, run_id: str) -> None:
        with self._lock:
            for key in [k for k, v in self._claims.items() if v == run_id]:
                del self._claims[key]

    def _claim(self, home: str, stem: str, run_id: str) -> bool:
        with self._lock:
            owner = self._claims.get((home, stem))
            if owner not in (None, run_id):
                return False
            self._claims[(home, stem)] = run_id
            return True

    def _owner(self, home: str, stem: str) -> str | None:
        with self._lock:
            return self._claims.get((home, stem))

    def resolve(
        self,
        rec: dict[str, Any],
        *,
        pids: list[int],
        runs_sharing_home: int,
    ) -> tuple[float | None, dict[str, Any]]:
        """Return (newest attributable session mtime, signal note)."""
        run_id = str(rec.get("run_id") or "")
        base = session_base(rec.get("spawn_environ"))
        if base is None:
            return None, {"used": False, "attribution": "none", "reason": "run has no HOME/USERPROFILE"}
        home = str(base)
        claimed: list[str] = rec.setdefault("_session_stems", [])
        if self.fd_scan and pids:
            for stem in sorted(stems_held_by(pids, base)):
                if stem not in claimed and self._claim(home, stem, run_id):
                    claimed.append(stem)
                    rec["_session_attribution"] = "open_handle"
        reason = ""
        if not claimed:
            baseline = set(rec.get("_session_baseline") or [])
            fresh = sorted(
                s for s in list_stems(base) if s not in baseline and self._owner(home, s) in (None, run_id)
            )
            held_note = " and none is held open by this run" if self.fd_scan else ""
            if not fresh:
                reason = "no session file created since this run started" + held_note
            elif runs_sharing_home > 1:
                reason = (
                    f"ambiguous: {runs_sharing_home} runs of this service share this HOME" + held_note
                )
            elif len(fresh) > 1:
                reason = f"ambiguous: {len(fresh)} session files appeared since this run started" + held_note
            elif self.fd_scan and holders_outside(fresh[0], base, set(pids)) > 0:
                reason = "the only new session file is held open by another process"
            elif self._claim(home, fresh[0], run_id):
                claimed.append(fresh[0])
                rec["_session_attribution"] = "new_file"
        if not claimed:
            return None, {"used": False, "attribution": "none", "reason": reason or "not attributable"}
        latest: float | None = None
        for stem in claimed[:8]:
            mtime = stem_mtime(base, stem)
            if mtime is not None and (latest is None or mtime > latest):
                latest = mtime
        note = {"used": latest is not None, "attribution": str(rec.get("_session_attribution") or "none")}
        if latest is None:
            note["reason"] = "claimed session files are gone"
        return latest, note


__all__ = ["SessionAttributor", "session_base", "stem_of", "list_stems", "stem_mtime", "fd_scan_available"]
