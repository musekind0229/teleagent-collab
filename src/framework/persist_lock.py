"""v0.3 §5.1: cross-process lock covering read-validate-modify-save.

Scheme
------
File-backed ledgers keep atomic replace (tmp + os.replace + fsync). Atomic
replace alone does **not** stop lost updates: two independent processes can
still read the same snapshot and the later replace clobbers the earlier Goal.

This module forces a single exclusive lock around the **entire**
read → validate → modify → save cycle for every ledger under a persist root.
The lock is not taken only around the write.

Linux: ``fcntl.flock(LOCK_EX)``. flock is per-process, so a threading.RLock
serializes threads in the same process. Nested acquisition (durable submit
claiming ownership under the same persist root) reuses one fd — closing a
second fd would drop the process lock (flock(2)).

Windows lock semantics are **not** implemented here (v0.3 §5.3). Callers
must not pretend a no-op or threading-only lock is cross-process on Windows.

Crash recovery (file storage, not a SQL transaction)
----------------------------------------------------
Canonical files (one per ledger):

* durable: ``<root>/.collab-durable/store.json``
  Per-goal ``goals/<id>.json`` copies are derived. Write store.json first;
  a crash before derived copies is repaired by the next persist/open from
  store.json. Leftover ``*.tmp`` is never promoted.

* ownership: ``<root>/.collab-goal-ownership/<goal_id>.json``
* budget: ``<root>/.collab-goal-budget/<goal_id>.json``
* outbox: ``<root>/.collab-outbox/journal.json`` is the transaction;
  ``entities.json`` / ``outbox.json`` are applied snapshots. A leftover
  journal is replayed under the lock. State + outbox stay one journal txn.

Cross-file (durable Goal + ownership claim on submit):

* Same persist-root lock serializes the pair.
* Canonical Goal set is store.json. If a crash lands after an ownership
  claim and before store.json replace, the Goal was not committed; retry
  with the same submit_key creates it. An ownership file for a Goal id
  that never landed in store.json is an orphan and is not treated as a
  Goal. Reconciliation is "store.json wins; orphan side files are ignored".
* Missing file → initialize. Corrupt / permission / incompatible open
  raises and **does not rewrite** the original.

Not in this module: Windows LockFileEx, single-writer network service,
SQLite/other transactional engines.
"""
from __future__ import annotations

import errno
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

LOCK_FILENAME = ".collab-persist.lock"

REASON_CORRUPT = "corrupt"
REASON_PERMISSION = "permission"
REASON_INCOMPATIBLE = "incompatible"


class PersistLockUnsupported(RuntimeError):
    """Raised when this host cannot take a cross-process persist lock."""


class StoreError(RuntimeError):
    """Ledger I/O or format failure. The original file must not be rewritten."""

    reason = "store"

    def __init__(self, path: str | Path, message: str, *, reason: str | None = None) -> None:
        self.path = Path(path)
        self.reason = reason or type(self).reason
        super().__init__(f"{self.reason}: {message} ({self.path})")


class StoreCorruptError(StoreError):
    reason = REASON_CORRUPT


class StorePermissionError(StoreError):
    reason = REASON_PERMISSION


class StoreIncompatibleError(StoreError):
    reason = REASON_INCOMPATIBLE


# Test-only: invoked after a depth-1 reload, still holding the persist lock.
# Independent-process tests stall here to prove the lock covers RMW, not just write.
after_lock_reload_hook: Callable[[], None] | None = None


def run_after_lock_reload() -> None:
    hook = after_lock_reload_hook
    if hook is not None:
        hook()


def persist_lock_path(persist_root: str | Path) -> Path:
    return Path(persist_root) / LOCK_FILENAME


def _root_key(persist_root: Path) -> str:
    try:
        return str(persist_root.resolve())
    except OSError:
        return str(persist_root)


_ROOT_THREAD_LOCKS: dict[str, threading.RLock] = {}
_ROOT_THREAD_LOCKS_MU = threading.Lock()


def _thread_lock(key: str) -> threading.RLock:
    with _ROOT_THREAD_LOCKS_MU:
        lock = _ROOT_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_THREAD_LOCKS[key] = lock
        return lock


@dataclass
class _Held:
    fd: int
    depth: int


# One flock fd per persist-root per process. Nested contexts increment depth.
_HELD: dict[str, _Held] = {}


def _require_linux_flock():
    if sys.platform == "win32":
        raise PersistLockUnsupported(
            "Windows persist-lock semantics are deferred to v0.3 §5.3; "
            "this knife uses Linux fcntl.flock only"
        )
    try:
        import fcntl
    except ImportError as e:  # pragma: no cover — Linux always has fcntl
        raise PersistLockUnsupported(
            "fcntl is unavailable; Linux fcntl.flock is required for §5.1"
        ) from e
    return fcntl


@contextmanager
def persist_lock(persist_root: str | Path) -> Iterator[Path]:
    """Exclusive lock for the whole RMW of ledgers under ``persist_root``.

    Not write-only. Nested calls from the same thread reuse the same fd.
    """
    fcntl = _require_linux_flock()
    root = Path(persist_root)
    root.mkdir(parents=True, exist_ok=True)
    key = _root_key(root)
    lock_path = persist_lock_path(root)
    mu = _thread_lock(key)
    mu.acquire()
    try:
        held = _HELD.get(key)
        if held is None:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
            held = _Held(fd=fd, depth=1)
            _HELD[key] = held
        else:
            held.depth += 1
        try:
            yield lock_path
        finally:
            held.depth -= 1
            if held.depth <= 0:
                try:
                    fcntl.flock(held.fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(held.fd)
                _HELD.pop(key, None)
    finally:
        mu.release()


@contextmanager
def rmw_lock(owner: Any, persist_root: str | Path, *, init_if_missing: bool = False) -> Iterator[None]:
    """Reentrant per-object RMW: lock, reload at depth 1, yield, keep lock.

    ``owner`` must provide ``_mu`` (threading.RLock), ``_rmw_depth`` (int),
    and ``_reload_unlocked(init_if_missing=...)``.
    """
    with persist_lock(persist_root):
        with owner._mu:
            owner._rmw_depth = int(getattr(owner, "_rmw_depth", 0)) + 1
            try:
                if owner._rmw_depth == 1:
                    owner._reload_unlocked(init_if_missing=init_if_missing)
                    run_after_lock_reload()
                yield
            finally:
                owner._rmw_depth = int(getattr(owner, "_rmw_depth", 1)) - 1


def atomic_write_json(path: Path, payload: Any) -> None:
    """fsync'd tmp + os.replace. Does not take the persist lock (caller must)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_json_file(path: Path) -> Any:
    """Read JSON from an existing file. Never invents an empty store.

    Distinguishes permission vs corrupt. Missing files raise FileNotFoundError
    (caller decides whether to initialize).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except PermissionError as e:
        raise StorePermissionError(path, "permission denied reading ledger") from e
    except OSError as e:
        if getattr(e, "errno", None) in (errno.EACCES, errno.EPERM):
            raise StorePermissionError(path, f"permission denied reading ledger: {e}") from e
        raise StoreCorruptError(path, f"unreadable ledger: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise StoreCorruptError(path, f"corrupt JSON: {e}") from e


def require_json_object(path: Path, raw: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise StoreIncompatibleError(path, f"{what} is not a JSON object")
    return dict(raw)


def check_contract_version(path: Path, payload: Mapping[str, Any], *, supported: set[str]) -> None:
    cv = payload.get("contract_version")
    if cv is None or cv == "":
        return
    if not isinstance(cv, str):
        raise StoreIncompatibleError(path, f"contract_version must be a string, got {type(cv).__name__}")
    if cv not in supported:
        raise StoreIncompatibleError(path, f"unsupported contract_version {cv!r}")


def classify_ledger_path(path: Path) -> str:
    """``missing`` | ``file`` | ``incompatible`` (exists but is not a regular file)."""
    try:
        if path.is_file():
            return "file"
        if path.exists():
            return "incompatible"
    except PermissionError as e:
        raise StorePermissionError(path, "permission denied stating ledger") from e
    except OSError as e:
        if getattr(e, "errno", None) in (errno.EACCES, errno.EPERM):
            raise StorePermissionError(path, f"permission denied stating ledger: {e}") from e
        raise StoreCorruptError(path, f"unreadable ledger path: {e}") from e
    return "missing"
