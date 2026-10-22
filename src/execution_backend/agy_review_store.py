"""Durable agy lead-review records. No credentials, tokens, or environment.

``path is None`` keeps the same API in memory and writes nothing. A configured
path is a JSON document ``{"version": 1, "runs": {run_id: record}}`` replaced
atomically (temp file in the same directory + ``os.replace``), mode 0600 on
POSIX. An unreadable file is renamed aside and treated as empty.

Path-backed mutations hold an exclusive OS lock on the sidecar ``<path>.lock``
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows) across the whole
read-modify-write. The kernel drops that lock when the descriptor is closed,
including when the process dies, so a leftover lock file does not block.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

STORE_VERSION = 1
ENV_REVIEW_STORE = "COLLAB_AGY_REVIEW_STORE"
_DEFAULT_NAME = "agy-reviews.json"
# msvcrt locks a byte range. 1 byte at offset 0 is the whole protocol on Windows.
_LOCK_NBYTES = 1


def resolve_review_store_path(
    *,
    explicit: str | os.PathLike | None = None,
    environ: Any = None,
    run_registry_path: str | os.PathLike | None = None,
) -> str | None:
    """explicit > ``COLLAB_AGY_REVIEW_STORE`` > ``<registry dir>/agy-reviews.json`` > None."""
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env = environ if environ is not None else os.environ
    raw = ""
    try:
        raw = str(env.get(ENV_REVIEW_STORE) or "").strip()
    except Exception:  # noqa: BLE001 — a non-mapping environ is "unset"
        raw = ""
    if raw:
        return raw
    reg = str(run_registry_path or "").strip()
    if not reg:
        return None
    return str(Path(reg).parent / _DEFAULT_NAME)


def _acquire_os_lock(fd: int) -> None:
    """Block until this descriptor holds the exclusive OS lock.

    Not a PID recorded in the lock file: the kernel releases ``flock`` and
    ``msvcrt.locking`` when the descriptor is closed, including on process death.
    """
    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if os.name != "nt":
        raise OSError(f"no cross-process review lock for os.name={os.name!r}")
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_LOCK, _LOCK_NBYTES)


def _release_os_lock(fd: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if os.name != "nt":
        return
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_NBYTES)


class AgyReviewStore:
    """JSON review records keyed by run_id. Memory-only when ``path`` is None.

    A path-backed write re-reads the disk file under the sidecar lock, applies
    one operation to that fresh mapping, writes the mapping, and only then
    swaps it into ``_mem``. A failed write leaves ``_mem`` unchanged. ``load``
    and ``get`` keep returning this instance's memory until its next successful
    write, which is when another instance's update becomes visible here.

    The sidecar is ``<path>.lock``. The lock is ``fcntl.flock`` on POSIX and
    ``msvcrt.locking`` of 1 byte at offset 0 on Windows. It is an OS lock on
    the open descriptor, not a PID stored in the file, and the kernel releases
    it when the descriptor closes — a lock file left by a dead process does
    not block. The in-process ``threading.Lock`` is acquired first, then the
    OS lock, so threads and other processes cannot deadlock each other or
    lose a read-modify-write. ``path is None`` takes only the thread lock and
    writes nothing.
    """

    def __init__(self, path: str | os.PathLike | None) -> None:
        self.path: Path | None = Path(path) if path else None
        self._lock = threading.Lock()
        self._mem: dict[str, dict[str, Any]] = {}
        if self.path is not None:
            self._mem = self._read_disk()

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._mem)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._mem.get(str(run_id))
            return copy.deepcopy(rec) if isinstance(rec, dict) else None

    def put(self, run_id: str, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise TypeError("review record must be a dict")
        stored = copy.deepcopy(record)
        if stored.get("updated_at") is None:
            stored["updated_at"] = time.time()
        key = str(run_id)

        def apply(
            fresh: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, dict[str, Any]] | None, None]:
            new_mem = dict(fresh)
            new_mem[key] = stored
            return new_mem, None

        self._commit(apply)

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        key = str(run_id)

        def apply(
            fresh: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any]]:
            current = fresh.get(key)
            if not isinstance(current, dict):
                raise KeyError(key)
            updated = copy.deepcopy(current)
            for name, value in fields.items():
                updated[name] = copy.deepcopy(value)
            if "updated_at" not in fields:
                updated["updated_at"] = time.time()
            new_mem = dict(fresh)
            new_mem[key] = updated
            return new_mem, copy.deepcopy(updated)

        return self._commit(apply)

    def forget(self, run_id: str) -> None:
        key = str(run_id)

        def apply(
            fresh: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, dict[str, Any]] | None, None]:
            if key not in fresh:
                return None, None
            new_mem = dict(fresh)
            del new_mem[key]
            return new_mem, None

        self._commit(apply)

    def prune(self, collected_older_than_sec: float = 7 * 86400, now: float | None = None) -> int:
        """Drop collected records whose timestamp is older than ``now - N``."""
        moment = time.time() if now is None else float(now)
        try:
            window = float(collected_older_than_sec)
        except (TypeError, ValueError):
            window = 7 * 86400
        cutoff = moment - window

        def apply(
            fresh: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, dict[str, Any]] | None, int]:
            new_mem = dict(fresh)
            dropped = 0
            for run_id, rec in list(new_mem.items()):
                if not isinstance(rec, dict) or rec.get("collected") is not True:
                    continue
                stamp = rec.get("updated_at")
                if stamp is None:
                    stamp = rec.get("started_at")
                try:
                    when = float(stamp) if stamp is not None else 0.0
                except (TypeError, ValueError):
                    when = 0.0
                if when < cutoff:
                    del new_mem[run_id]
                    dropped += 1
            if not dropped:
                return None, 0
            return new_mem, dropped

        return self._commit(apply)

    def _commit(
        self,
        apply: Callable[
            [dict[str, dict[str, Any]]],
            tuple[dict[str, dict[str, Any]] | None, Any],
        ],
    ) -> Any:
        """Re-read, apply one op, write, then publish. Memory moves only after the write."""
        with self._locked_mutation():
            if self.path is None:
                base = self._mem
            else:
                base = self._read_disk()
                if not self.path.is_file():
                    # Missing, or just renamed aside as unreadable: keep this
                    # instance's records instead of writing an empty store.
                    base = self._mem
            new_mem, result = apply(dict(base))
            if new_mem is None:
                return result
            self._write_locked(new_mem)
            self._mem = new_mem
            return result

    @contextmanager
    def _locked_mutation(self) -> Iterator[None]:
        """Thread lock, then the sidecar OS lock. Memory-only stores skip the file."""
        with self._lock:
            if self.path is None:
                yield
                return
            lock_path = Path(f"{self.path}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT
            cloexec = getattr(os, "O_CLOEXEC", 0) or getattr(os, "O_NOINHERIT", 0)
            if cloexec:
                flags |= cloexec
            fd = os.open(os.fspath(lock_path), flags, 0o600)
            acquired = False
            try:
                _acquire_os_lock(fd)
                acquired = True
                yield
            finally:
                try:
                    if acquired:
                        _release_os_lock(fd)
                finally:
                    os.close(fd)

    def _read_disk(self) -> dict[str, dict[str, Any]]:
        path = self.path
        if path is None or not path.is_file():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            self._quarantine(type(exc).__name__)
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            self._quarantine("bad shape")
            return {}
        runs: dict[str, dict[str, Any]] = {}
        for key, value in data["runs"].items():
            if isinstance(key, str) and isinstance(value, dict):
                runs[key] = value
        return runs

    def _quarantine(self, why: str) -> None:
        path = self.path
        if path is None or not path.is_file():
            return
        dest = Path(f"{path}.corrupt-{time.time_ns()}")
        try:
            os.replace(path, dest)
        except OSError as exc:
            _LOG.warning("agy review store quarantine failed (%s)", type(exc).__name__)
            return
        _LOG.warning("agy review store unreadable (%s); renamed aside", why)

    def _write_locked(self, mem: dict[str, dict[str, Any]] | None = None) -> None:
        path = self.path
        if path is None:
            return
        runs = self._mem if mem is None else mem
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STORE_VERSION, "runs": runs}
        fd, tmp_name = tempfile.mkstemp(prefix=".agy-reviews-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            if os.name == "posix":
                try:
                    os.chmod(tmp_name, 0o600)
                except OSError:
                    pass
            os.replace(tmp_name, path)
            if os.name == "posix":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
