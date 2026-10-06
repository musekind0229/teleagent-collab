"""Known agy model ids for per-Goal ``model`` validation.

``agy models`` prints ``<id>\\t<label>`` lines (after a "Fetching..." banner).
The Application API rejects a Goal whose ``model`` is not in the known list,
so a typo fails at open time instead of minutes later inside agy.

The live list comes from ``agy models`` (run under a pool account HOME). It is
cached and refreshed in a background thread, never on the request path, so a
slow or unavailable ``agy models`` cannot stall ``POST /v1/requests``. Until the
first probe succeeds (or when it keeps failing), a built-in list is used; a
failed refresh keeps the last good live list. Probing is off unless the service
enables it, so tests and one-off backends never spawn ``agy models``.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

# `agy models` on agy 1.2.17 (both mde pool accounts), 2026-10-06.
BUILTIN_AGY_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
DEFAULT_TTL_SEC = 1800.0
DEFAULT_RETRY_SEC = 120.0

Probe = Callable[[], "list[str] | None"]


def parse_agy_models_output(text: str) -> list[str]:
    """Model ids from ``agy models`` output; [] when none (e.g. not signed in)."""
    out: list[str] = []
    for line in str(text or "").splitlines():
        if "\t" not in line:
            continue
        ident = line.split("\t", 1)[0].strip()
        if MODEL_ID_RE.match(ident) and ident not in out:
            out.append(ident)
    return out


def valid_model_id(value: object) -> bool:
    return isinstance(value, str) and bool(MODEL_ID_RE.match(value))


class AgyModelCatalog:
    """Cached model list: live ``agy models`` when known, else built-in."""

    def __init__(
        self,
        probe: Probe | None = None,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        retry_sec: float = DEFAULT_RETRY_SEC,
        builtin: tuple[str, ...] = BUILTIN_AGY_MODELS,
    ) -> None:
        self._probe = probe
        self._ttl = float(ttl_sec)
        self._retry = float(retry_sec)
        self._builtin = list(builtin)
        self._lock = threading.Lock()
        self._live: list[str] | None = None
        self._live_at: float | None = None
        self._last_attempt: float | None = None
        self._inflight = False
        self._auto = False

    def enable_auto_refresh(self) -> None:
        """Allow background ``agy models`` probes (service start); kicks one off."""
        self._auto = True
        self._maybe_refresh()

    def known(self) -> tuple[list[str], str]:
        """(model ids, source) without blocking. Source: agy_models | builtin."""
        self._maybe_refresh()
        with self._lock:
            if self._live:
                return list(self._live), "agy_models"
        return list(self._builtin), "builtin"

    def checked_at(self) -> str | None:
        with self._lock:
            at = self._live_at
        if at is None:
            return None
        return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def refresh_now(self) -> bool:
        """Probe synchronously. True when a non-empty live list was stored."""
        with self._lock:
            self._last_attempt = time.time()
        models: list[str] | None = None
        if self._probe is not None:
            try:
                models = self._probe()
            except Exception:  # noqa: BLE001 - a broken probe just keeps the cache
                models = None
        ok = bool(models)
        with self._lock:
            if ok:
                self._live = [str(m) for m in models or []]
                self._live_at = time.time()
            self._inflight = False
        return ok

    def _maybe_refresh(self) -> None:
        if not self._auto or self._probe is None:
            return
        now = time.time()
        with self._lock:
            if self._inflight:
                return
            fresh = self._live_at is not None and now - self._live_at < self._ttl
            recent_try = self._last_attempt is not None and now - self._last_attempt < self._retry
            if fresh or recent_try:
                return
            self._inflight = True
        threading.Thread(target=self.refresh_now, name="agy-models-refresh", daemon=True).start()


__all__ = [
    "AgyModelCatalog",
    "BUILTIN_AGY_MODELS",
    "MODEL_ID_RE",
    "parse_agy_models_output",
    "valid_model_id",
]
