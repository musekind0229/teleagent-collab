"""Windows TeleAgent adapter: same Basic + local-v1 HMAC worker HTTP as Linux.

**Windows 真机未验收** — this module is a contract-level port. Tests inject
mock transport / HTTP responses. Do not treat a green unittest run as proof
that a live Windows TeleAgent install speaks this surface. Live doctor/hello
on DESKTOP-TBB531F is workshop-owned (``windows_live_verified=false``).

Differences vs Linux (`linux_local_v1.py`):
- Creds: this-process env first, then other TeleAgent/SAC process environ
  (Win32 PEB / ``ReadProcessMemory``, historical / unreliable on current GUI),
  then a controlled **stdin_wrap** parent spawn (loopback :4401) using the
  same stdin env payload as the GUI. Never Credential Manager. Never disable
  auth. Never SeDebug / token.json scrape.
- Port discovery: probe loopback 4399 then 4397 then 4398 (env
  `TELEAGENT_BASE_URL` / `TELEAGENT_PORT` override). Live Win worker HTTP
  has been observed on **:4397** and **:4398**.
- Paths: session `directory` / `x-opencode-directory` are passed through
  (Windows drive-letter paths). Do not POSIX-rewrite.

`WindowsBlockedAdapter` remains the explicit blocked/degraded path; it is
not the factory default for `win32` / `windows`.
"""
from __future__ import annotations

import os
import socket
from collections.abc import Callable, Mapping
from urllib.parse import urlparse

from teleagent_adapter.base import AdapterError, AdapterStatus
from teleagent_adapter.linux_local_v1 import FindCredsFn, LocalV1HttpAdapter
from teleagent_adapter.windows_process_environ import (
    CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING,
    MISSING_CREDS_MESSAGE,
    resolve_windows_local_v1_creds,
)

# Probe 4399 first (Linux verified listen), then 4397 (historical Win),
# then 4398 (DESKTOP-TBB531F after TeleAgent restart).
DEFAULT_WIN_PORTS: tuple[int, ...] = (4399, 4397, 4398)
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
    foreign_finder: Callable[[], tuple[str, str, str] | None] | None = None,
    wrap_fn: Callable | None = None,
) -> tuple[str, str, str]:
    """Read local-v1 creds: this-process env, then PEB/environ, then stdin_wrap.

    Username defaults to ``super-agent`` when password + session key are set
    (Linux documented Basic user). Missing password or HMAC key → MISSING_CREDS
    after scanning other processes' environ (not this-process env) and, when
    the creds channel allows it, a controlled stdin_wrap spawn. Does not
    suggest disabling auth. Inject ``foreign_finder`` / ``wrap_fn`` in tests.

    Windows 真机未验收 — live doctor/hello is workshop-owned.
    """
    creds, presence = resolve_windows_local_v1_creds(
        environ=environ,
        foreign_finder=foreign_finder,
        wrap_fn=wrap_fn,
    )
    if creds is None:
        msg = MISSING_CREDS_MESSAGE
        blocker = getattr(presence, "blocker", None)
        if isinstance(blocker, str) and (
            blocker.startswith("stdin_wrap") or blocker == CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING
        ):
            msg = msg + f" blocker={blocker}."
        raise AdapterError(AdapterStatus.MISSING_CREDS, msg)
    return creds


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
    then TCP probe of 4399 then 4397 then 4398. If nothing accepts TCP,
    return the first candidate (4399) so doctor can classify ``not_running``.

    Live Win worker HTTP has been observed on **:4397** and **:4398**
    (DESKTOP-TBB531F). Windows 真机未验收.
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
            result = default_find_creds_windows(environ=env)
            if self._auto_discover:
                try:
                    from teleagent_adapter.windows_stdin_wrap import current_wrap_handle

                    handle = current_wrap_handle()
                except Exception:
                    handle = None
                if handle is not None and getattr(handle, "owns_base_url", False):
                    self.base_url = handle.base_url.rstrip("/")
            return result

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
