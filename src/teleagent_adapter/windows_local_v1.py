"""Windows TeleAgent adapter: same Basic + local-v1 HMAC worker HTTP as Linux.

**Windows 真机未验收** — this module is a contract-level port. Tests inject
mock transport / HTTP responses. Do not treat a green unittest run as proof
that a live Windows TeleAgent install speaks this surface.

Differences vs Linux (`linux_local_v1.py`):
- Creds: process environment (`OPENCODE_SERVER_*`, `SUPER_AGENT_LOCAL_SESSION_KEY`),
  not `/proc/*/environ`.
- Port discovery: probe loopback 4399 then 4397 (env `TELEAGENT_BASE_URL` /
  `TELEAGENT_PORT` override). Linux glue historically hard-codes :4399.
- Paths: session `directory` / `x-opencode-directory` are passed through
  (Windows drive-letter paths). Do not POSIX-rewrite.

`WindowsBlockedAdapter` remains the explicit blocked/degraded path; it is
not the factory default for `win32` / `windows`.
"""
from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from typing import Callable
from urllib.parse import urlparse

from teleagent_adapter.base import AdapterError, AdapterStatus
from teleagent_adapter.linux_local_v1 import FindCredsFn, LocalV1HttpAdapter

# Same worker HTTP candidates as Linux discovery notes (4397 was a stale env;
# 4399 is the verified Linux listen port). Win live bind is unconfirmed.
DEFAULT_WIN_PORTS: tuple[int, ...] = (4399, 4397)
DEFAULT_WIN_HOST = "127.0.0.1"


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _first_env(env: Mapping[str, str], *names: str) -> str:
    for n in names:
        v = env.get(n)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def default_find_creds_windows(
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    """Read local-v1 creds from process env (no /proc on Windows).

    Username defaults to ``super-agent`` when password + session key are set
    (Linux documented Basic user). Missing password or HMAC key → MISSING_CREDS.

    Windows 真机未验收: GUI/SAC child-process environ scrape is not implemented
    here (would need Win32 process APIs). Inject ``find_creds_fn`` in tests.
    """
    env = environ if environ is not None else os.environ
    user = _first_env(env, "OPENCODE_SERVER_USERNAME", "SUPER_AGENT_OPENCODE_USERNAME") or "super-agent"
    pw = _first_env(env, "OPENCODE_SERVER_PASSWORD", "SUPER_AGENT_OPENCODE_PASSWORD")
    key = _first_env(env, "SUPER_AGENT_LOCAL_SESSION_KEY")
    if not pw or not key:
        raise AdapterError(
            AdapterStatus.MISSING_CREDS,
            "Windows TeleAgent local API creds not found in process environment "
            "(need OPENCODE_SERVER_PASSWORD + SUPER_AGENT_LOCAL_SESSION_KEY). "
            "Windows 真机未验收.",
        )
    return user, pw, key


def discover_windows_base_url(
    *,
    host: str = DEFAULT_WIN_HOST,
    ports: tuple[int, ...] | None = None,
    probe_fn: Callable[[str, int], bool] | None = None,
    env: Mapping[str, str] | None = None,
    allow_non_loopback: bool = False,
) -> str:
    """Resolve TeleAgent worker HTTP base URL for Windows.

    Order: ``TELEAGENT_BASE_URL``, then ``TELEAGENT_PORT`` among candidates,
    then TCP probe of 4399 then 4397. If nothing accepts TCP, return the
    first candidate (4399) so doctor can classify ``not_running``.

    Windows 真机未验收 — live bind address/port may differ.
    """
    environ = env if env is not None else os.environ
    explicit = _first_env(environ, "TELEAGENT_BASE_URL").rstrip("/")
    if explicit:
        if not allow_non_loopback:
            n = urlparse(explicit if "://" in explicit else f"http://{explicit}")
            h = (n.hostname or "").lower()
            if h and h not in ("127.0.0.1", "localhost", "::1"):
                raise AdapterError(
                    AdapterStatus.BLOCKED,
                    f"windows adapter TELEAGENT_BASE_URL must be loopback (got host={h!r})",
                )
        if "://" not in explicit:
            explicit = f"http://{explicit}"
        return explicit.rstrip("/")

    candidate_ports: tuple[int, ...] = ports or DEFAULT_WIN_PORTS
    port_s = _first_env(environ, "TELEAGENT_PORT")
    if port_s.isdigit():
        p = int(port_s)
        candidate_ports = (p,) + tuple(x for x in candidate_ports if x != p)

    probe = probe_fn or _tcp_open
    for port in candidate_ports:
        if probe(host, port):
            return f"http://{host}:{port}"
    return f"http://{host}:{candidate_ports[0]}"


class WindowsLocalV1Adapter(LocalV1HttpAdapter):
    """Win32 worker adapter — isomorphic HTTP with Linux local-v1.

    Windows 真机未验收. Factory default for ``win32`` / ``windows``.
    """

    PLATFORM = "windows"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        find_creds_fn: FindCredsFn | None = None,
        lazy_creds: bool = True,
        allow_non_loopback: bool = False,
        discover: bool = True,
        probe_fn: Callable[[str, int], bool] | None = None,
        env: Mapping[str, str] | None = None,
        host: str = DEFAULT_WIN_HOST,
    ) -> None:
        self._probe_fn = probe_fn
        self._env = env
        self._discover_host = host or DEFAULT_WIN_HOST
        # Re-discover on reconnect only when the caller did not pin base_url.
        self._auto_discover = bool(discover) and not base_url
        if not base_url:
            if discover:
                base_url = discover_windows_base_url(
                    host=self._discover_host,
                    probe_fn=probe_fn,
                    env=env,
                    allow_non_loopback=allow_non_loopback,
                )
            else:
                base_url = f"http://{self._discover_host}:{DEFAULT_WIN_PORTS[0]}"

        def _creds() -> tuple[str, str, str]:
            return default_find_creds_windows(environ=env)

        super().__init__(
            base_url=base_url,
            find_creds_fn=find_creds_fn or _creds,
            lazy_creds=lazy_creds,
            allow_non_loopback=allow_non_loopback,
        )

    def reconnect(self) -> None:
        """Re-resolve loopback port (if auto-discover) + refresh creds once."""
        if self._auto_discover:
            self.base_url = discover_windows_base_url(
                host=self._discover_host,
                probe_fn=self._probe_fn,
                env=self._env,
                allow_non_loopback=self._allow_non_loopback,
            )
        super().reconnect()


__all__ = [
    "WindowsLocalV1Adapter",
    "default_find_creds_windows",
    "discover_windows_base_url",
    "DEFAULT_WIN_PORTS",
    "DEFAULT_WIN_HOST",
]
