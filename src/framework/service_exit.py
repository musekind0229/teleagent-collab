"""Why did the previous service process end? (systemd ExecStopPost record)

The cgroup's memory.events is recreated when systemd restarts the unit, so a
restarted service cannot see that its predecessor was OOM-killed. systemd
passes ``$SERVICE_RESULT`` (``oom-kill``, ``signal``, ``exit-code`` ...),
``$EXIT_CODE`` and ``$EXIT_STATUS`` to ``ExecStopPost=``. ``collab-service.py
--record-exit`` writes them to ``<persist>/last-exit.json``; the next start
consumes the file and uses it in orphan-reap and resume reasons.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

FILE_NAME = "last-exit.json"
SEEN_NAME = "last-exit.prev.json"
_KEYS = ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_.")[:40]


def record_exit(persist: str | Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    row = {k.lower(): _clean(env.get(k)) for k in _KEYS}
    row["recorded_at"] = time.time()
    path = Path(persist) / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(row), encoding="utf-8")
    os.replace(tmp, path)
    return row


def consume_previous_exit(persist: str | Path) -> dict[str, Any] | None:
    """Read and retire the record (kept as last-exit.prev.json). Never raises."""
    path = Path(persist) / FILE_NAME
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        os.replace(path, path.with_name(SEEN_NAME))
    except OSError:
        pass
    return row if isinstance(row, dict) else None


def was_oom(row: Mapping[str, Any] | None) -> bool:
    return isinstance(row, Mapping) and str(row.get("service_result") or "") == "oom-kill"


def previous_exit_note(row: Mapping[str, Any] | None) -> str:
    """Short cause for reasons. "" for a normal stop or no record."""
    if not isinstance(row, Mapping):
        return ""
    result = str(row.get("service_result") or "")
    if result == "oom-kill":
        return "the previous service process was killed by OOM (cgroup memory limit; systemd result oom-kill)"
    if result in {"", "success"}:
        return ""
    detail = "/".join(x for x in (str(row.get("exit_code") or ""), str(row.get("exit_status") or "")) if x)
    return f"the previous service process ended abnormally (systemd result {result}{', ' + detail if detail else ''})"


__all__ = ["record_exit", "consume_previous_exit", "was_oom", "previous_exit_note", "FILE_NAME"]
