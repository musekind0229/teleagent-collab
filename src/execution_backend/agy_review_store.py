"""Durable agy lead-review records. No credentials, tokens, or environment.

``path is None`` keeps the same API in memory and writes nothing. A configured
path is a JSON document ``{"version": 1, "runs": {run_id: record}}`` replaced
atomically (temp file in the same directory + ``os.replace``), mode 0600 on
POSIX. An unreadable file is renamed aside and treated as empty.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

STORE_VERSION = 1
ENV_REVIEW_STORE = "COLLAB_AGY_REVIEW_STORE"
_DEFAULT_NAME = "agy-reviews.json"


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


class AgyReviewStore:
    """JSON review records keyed by run_id. Memory-only when ``path`` is None.

    A path-backed write builds the next mapping, writes that mapping, and only
    then swaps it into ``_mem``. A failed write leaves ``_mem`` unchanged.
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
        with self._lock:
            new_mem = dict(self._mem)
            new_mem[str(run_id)] = stored
            self._write_locked(new_mem)
            self._mem = new_mem

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        key = str(run_id)
        with self._lock:
            current = self._mem.get(key)
            if not isinstance(current, dict):
                raise KeyError(key)
            updated = copy.deepcopy(current)
            for name, value in fields.items():
                updated[name] = copy.deepcopy(value)
            if "updated_at" not in fields:
                updated["updated_at"] = time.time()
            new_mem = dict(self._mem)
            new_mem[key] = updated
            returned = copy.deepcopy(updated)
            self._write_locked(new_mem)
            self._mem = new_mem
            return returned

    def forget(self, run_id: str) -> None:
        key = str(run_id)
        with self._lock:
            if key not in self._mem:
                return
            new_mem = dict(self._mem)
            del new_mem[key]
            self._write_locked(new_mem)
            self._mem = new_mem

    def prune(self, collected_older_than_sec: float = 7 * 86400, now: float | None = None) -> int:
        """Drop collected records whose timestamp is older than ``now - N``."""
        moment = time.time() if now is None else float(now)
        try:
            window = float(collected_older_than_sec)
        except (TypeError, ValueError):
            window = 7 * 86400
        cutoff = moment - window
        with self._lock:
            new_mem = dict(self._mem)
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
            if dropped:
                self._write_locked(new_mem)
                self._mem = new_mem
            return dropped

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
