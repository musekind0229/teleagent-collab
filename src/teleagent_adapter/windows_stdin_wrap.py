"""Controlled parent-process stdin wrap for Windows TeleAgent local-v1 creds.

Current GUI TeleAgent injects SECRET_ENV_KEYS via a stdin env payload
(uint32 BE length + JSON) and deliberately omits them from the OS environ.
There is no official named-pipe / exportCredentials API. This module spawns a
**parallel** kernel instance (default loopback **:4401**, not GUI :4398) using
the same stdin payload the GUI uses. Secrets stay in the parent process
memory (default: not written to disk) and are injected into *this* process
``os.environ`` at runtime so existing Basic + local-v1 glue/doctor keep working.

Never logs secret values. Never Credential Manager, never disable auth, never
SeDebugPrivilege, never scrape OAuth token.json. Do not guess Program Files
GUI ``TeleAgent.exe``. Optional GUI model reuse injects ``SUPER_AGENT_AUTH_STATE``
and ``OPENCODE_CONFIG_CONTENT`` via the same stdin channel (see
``windows_gui_model_reuse``).
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teleagent_adapter.windows_gui_model_reuse import (
    GuiModelAuthError,
    OPENCODE_CONFIG_DIR_ENV,
    TELEAGENT_CONFIG_DIR_ENV,
    prepare_gui_model_reuse,
)
from teleagent_adapter.windows_process_environ import (
    CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING,
    CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING,
    CREDS_BLOCKER_STDIN_WRAP_READY_TIMEOUT,
    CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
    CREDS_SOURCE_STDIN_WRAP,
    DEFAULT_BASIC_USER,
)

DEFAULT_WRAP_PORT = 4401
DEFAULT_READY_TIMEOUT_S = 15.0
DEFAULT_PORT_FREE_TIMEOUT_S = 10.0
CREDS_SOURCE_ENV = "TELEAGENT_CREDS_SOURCE"
KERNEL_BIN_ENV = "TELEAGENT_KERNEL_BIN"
WRAP_PORT_ENV = "TELEAGENT_WRAP_PORT"
WRAP_STATE_DIR_ENV = "TELEAGENT_WRAP_STATE_DIR"

_KERNEL_REL = Path("TeleAgent") / "runtimes" / "super-agent-code" / "bin" / "TeleAgent.exe"

# SECRET keys go in the stdin JSON payload only — never the child OS environ.
SECRET_STDIN_KEYS: frozenset[str] = frozenset(
    {
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_SERVER_USERNAME",
        "SUPER_AGENT_LOCAL_SESSION_KEY",
        "SUPER_AGENT_OPENCODE_PASSWORD",
        "SUPER_AGENT_OPENCODE_USERNAME",
        "SUPER_AGENT_SERVER_PASSWORD",
        "SUPER_AGENT_AUTH_STATE",
        "OPENCODE_CONFIG_CONTENT",
    }
)

# Non-secret OS environ copied into the child (whitelist). XDG_DATA_HOME is set
# by us to a collab-owned directory so wrap data does not collide with GUI.
_CHILD_OS_ENV_ALLOW: tuple[str, ...] = (
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "SystemDrive",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
    "USER",
    "LOCALAPPDATA",
    "APPDATA",
    "ProgramData",
    "PUBLIC",
    "ALLUSERSPROFILE",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "ProgramFiles",
    "ProgramFiles(x86)",
)

SpawnFn = Callable[..., Any]
ReadyFn = Callable[[str], bool]


class StdinWrapError(RuntimeError):
    """Fail-closed wrap error. ``blocker`` is a doctor extras code; never a secret."""

    def __init__(self, blocker: str, message: str) -> None:
        self.blocker = blocker
        super().__init__(message)


@dataclass
class WrapHandle:
    """In-memory handle for a live stdin-wrap kernel. Secret fields are redacted in repr."""

    base_url: str
    username: str
    password: str
    session_key: str
    pid: int
    source: str = CREDS_SOURCE_STDIN_WRAP
    port: int = DEFAULT_WRAP_PORT
    owns_base_url: bool = False

    def __repr__(self) -> str:
        return (
            f"WrapHandle(base_url={self.base_url!r}, username={self.username!r}, "
            f"pid={self.pid}, source={self.source!r}, port={self.port}, "
            f"owns_base_url={self.owns_base_url}, password=***, session_key=***)"
        )


_LIVE: WrapHandle | None = None
_LIVE_PROC: Any = None


def current_wrap_handle() -> WrapHandle | None:
    return _LIVE


def wrap_state_dir(*, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = (env.get(WRAP_STATE_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    temp = (env.get("TEMP") or env.get("TMP") or env.get("TMPDIR") or tempfile.gettempdir()).strip()
    return Path(temp) / "teleagent-collab-wrap"


def wrap_xdg_home(*, environ: Mapping[str, str] | None = None) -> Path:
    return wrap_state_dir(environ=environ) / "xdg"


def _pid_path(*, environ: Mapping[str, str] | None = None) -> Path:
    return wrap_state_dir(environ=environ) / "wrap.pid"


def build_env_payload(env: dict) -> bytes:
    """asar ``buildEnvPayload``: uint32 big-endian length + JSON UTF-8 body."""
    raw = json.dumps(env).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def parse_env_payload(payload: bytes) -> dict:
    """Inverse of ``build_env_payload`` (tests)."""
    if len(payload) < 4:
        return {}
    (n,) = struct.unpack(">I", payload[:4])
    body = payload[4 : 4 + n]
    data = json.loads(body.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def build_child_os_env(
    *,
    parent: Mapping[str, str] | None = None,
    xdg_data_home: str | None = None,
    extra_nonsecret: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """OS environ for the kernel child: non-secret whitelist only."""
    src = parent if parent is not None else os.environ
    out: dict[str, str] = {}
    for key in _CHILD_OS_ENV_ALLOW:
        val = src.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    if xdg_data_home:
        out["XDG_DATA_HOME"] = xdg_data_home
    if extra_nonsecret:
        for key, val in extra_nonsecret.items():
            if key in SECRET_STDIN_KEYS:
                continue
            if isinstance(val, str) and val:
                out[key] = val
    for secret in SECRET_STDIN_KEYS:
        out.pop(secret, None)
    return out


def _kernel_candidates(env: Mapping[str, str]) -> list[Path]:
    """Runtime kernel paths only — never Program Files GUI exe."""
    seen: set[str] = set()
    out: list[Path] = []

    def _add(root: str, *parts: str) -> None:
        root = (root or "").strip()
        if not root:
            return
        p = Path(root).joinpath(*parts)
        key = str(p).replace("\\", "/").lower()
        if key in seen:
            return
        seen.add(key)
        out.append(p)

    userprofile = (env.get("USERPROFILE") or env.get("HOME") or "").strip()
    localappdata = (env.get("LOCALAPPDATA") or "").strip()
    home = (env.get("HOME") or "").strip()

    _add(userprofile, ".local", "share", *_KERNEL_REL.parts)
    _add(localappdata, *_KERNEL_REL.parts)
    _add(localappdata, ".local", "share", *_KERNEL_REL.parts)
    if home and home != userprofile:
        _add(home, ".local", "share", *_KERNEL_REL.parts)
    return out


def resolve_kernel_bin(
    *,
    environ: Mapping[str, str] | None = None,
    is_file: Callable[[Path], bool] | None = None,
) -> Path:
    """Locate the runtime kernel ``TeleAgent.exe``.

    Order: ``TELEAGENT_KERNEL_BIN``, then
    ``{USERPROFILE}/.local/share/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe``
    and LOCALAPPDATA / HOME variants if those files exist. Does not search
    Program Files. Missing binary → ``stdin_wrap_bin_missing``.
    """
    env = environ if environ is not None else os.environ
    check = is_file or (lambda p: Path(p).is_file())
    explicit = (env.get(KERNEL_BIN_ENV) or "").strip()
    if explicit:
        path = Path(explicit)
        if check(path):
            return path
        raise StdinWrapError(
            CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING,
            f"{KERNEL_BIN_ENV} is set but is not a file. "
            "Point it at runtimes/super-agent-code/bin/TeleAgent.exe "
            "(not the Program Files GUI exe).",
        )
    for cand in _kernel_candidates(env):
        try:
            if check(cand):
                return cand
        except OSError:
            continue
    raise StdinWrapError(
        CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING,
        "TeleAgent kernel binary not found. Set TELEAGENT_KERNEL_BIN to "
        "{USERPROFILE}/.local/share/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe "
        "(or LOCALAPPDATA/HOME variant). Do not use the Program Files GUI TeleAgent.exe.",
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _read_pid_file(*, environ: Mapping[str, str] | None = None) -> tuple[int, int] | None:
    path = _pid_path(environ=environ)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid") or 0)
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or port <= 0:
        return None
    return pid, port


def _write_pid_file(pid: int, port: int, *, environ: Mapping[str, str] | None = None) -> None:
    path = _pid_path(environ=environ)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": int(pid), "port": int(port)}), encoding="utf-8")
    except OSError:
        pass


def _remove_pid_file(*, environ: Mapping[str, str] | None = None) -> None:
    path = _pid_path(environ=environ)
    try:
        path.unlink()
    except OSError:
        pass


def _http_ready(base_url: str, timeout: float = 1.0) -> bool:
    url = base_url.rstrip("/") + "/ready"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _wait_ready(base_url: str, *, ready_fn: ReadyFn, timeout: float, proc: Any) -> None:
    deadline = time.monotonic() + max(0.05, float(timeout))
    while time.monotonic() < deadline:
        try:
            if ready_fn(base_url):
                return
        except Exception:
            pass
        poll = getattr(proc, "poll", None)
        if callable(poll):
            try:
                rc = poll()
            except Exception:
                rc = None
            if rc is not None:
                raise StdinWrapError(
                    CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
                    f"stdin_wrap kernel exited before /ready (code={rc})",
                )
        time.sleep(0.05)
    raise StdinWrapError(
        CREDS_BLOCKER_STDIN_WRAP_READY_TIMEOUT,
        f"stdin_wrap timed out waiting for {base_url}/ready",
    )


def _creation_flags() -> int:
    if not sys.platform.lower().startswith("win"):
        return 0
    flags = 0
    flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) or 0)
    return flags


def _default_spawn(argv: list[str], *, stdin_payload: bytes, env: dict[str, str]) -> Any:
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=_creation_flags(),
        )
    except OSError as e:
        raise StdinWrapError(
            CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
            f"stdin_wrap spawn failed: {e.__class__.__name__}",
        ) from e
    if proc.stdin is None:
        _terminate_proc(proc)
        raise StdinWrapError(
            CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
            "stdin_wrap spawn failed: kernel stdin is not a pipe",
        )
    try:
        proc.stdin.write(stdin_payload)
        proc.stdin.close()
    except (BrokenPipeError, OSError) as e:
        _terminate_proc(proc)
        raise StdinWrapError(
            CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
            f"stdin_wrap failed writing env payload: {e.__class__.__name__}",
        ) from e
    return proc


def _terminate_proc(proc: Any) -> None:
    if proc is None:
        return
    try:
        poll = getattr(proc, "poll", None)
        if callable(poll) and poll() is not None:
            return
    except Exception:
        pass
    for meth in ("terminate", "kill"):
        fn = getattr(proc, meth, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                continue
    pid = int(getattr(proc, "pid", 0) or 0)
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _resolve_port(port: int | None, env: Mapping[str, str]) -> int:
    if port is not None:
        return int(port)
    raw = (env.get(WRAP_PORT_ENV) or "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_WRAP_PORT



def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            return sock.connect_ex((host, int(port))) == 0
        except OSError:
            return False


def _listener_pids_on_port(port: int) -> list[int]:
    """PIDs in TCP LISTEN on ``port`` (IPv4/IPv6). Empty on non-Windows or error."""
    if not sys.platform.lower().startswith("win"):
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    af_inet, af_inet6 = 2, 23
    table_listener = 3
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    get_table.restype = wintypes.DWORD

    class _Row4(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    class _Row6(ctypes.Structure):
        _fields_ = [
            ("ucLocalAddr", ctypes.c_ubyte * 16),
            ("dwLocalScopeId", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("ucRemoteAddr", ctypes.c_ubyte * 16),
            ("dwRemoteScopeId", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwState", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    def _rows(family: int, row_cls: type) -> list[int]:
        size = wintypes.DWORD(0)
        get_table(None, ctypes.byref(size), False, family, table_listener, 0)
        if size.value <= 4:
            return []
        buf = ctypes.create_string_buffer(size.value)
        rc = get_table(buf, ctypes.byref(size), False, family, table_listener, 0)
        if rc != 0:
            return []
        n = struct.unpack_from("<I", buf, 0)[0]
        off = 4
        row_size = ctypes.sizeof(row_cls)
        found: list[int] = []
        for _ in range(int(n)):
            if off + row_size > size.value:
                break
            row = row_cls.from_buffer_copy(buf, off)
            off += row_size
            local_port = socket.ntohs(int(row.dwLocalPort) & 0xFFFF)
            if local_port != int(port):
                continue
            pid = int(row.dwOwningPid or 0)
            if pid > 0:
                found.append(pid)
        return found

    pids: list[int] = []
    try:
        pids.extend(_rows(af_inet, _Row4))
    except Exception:
        pass
    try:
        pids.extend(_rows(af_inet6, _Row6))
    except Exception:
        pass
    # unique, skip self
    me = os.getpid()
    out: list[int] = []
    seen: set[int] = set()
    for pid in pids:
        if pid == me or pid in seen or pid <= 4:
            continue
        seen.add(pid)
        out.append(pid)
    return out


def _kill_listeners_on_port(port: int) -> None:
    """Best-effort terminate of processes LISTENING on wrap port. Never logs."""
    for pid in _listener_pids_on_port(port):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue


def _wait_port_free(
    port: int,
    *,
    timeout: float | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    listening_fn: Callable[[int], bool] | None = None,
) -> None:
    """Wait until wrap port is not LISTENING. Fail-closed on timeout.

    ``listening_fn is None`` skips the wait (tests that inject spawn_fn).
    """
    if listening_fn is None:
        return
    sleeper = sleep_fn or time.sleep
    limit = DEFAULT_PORT_FREE_TIMEOUT_S if timeout is None else float(timeout)
    deadline = time.monotonic() + max(0.0, limit)
    while True:
        try:
            busy = bool(listening_fn(int(port)))
        except Exception:
            busy = False
        if not busy:
            return
        if time.monotonic() >= deadline:
            raise StdinWrapError(
                CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
                f"stdin_wrap port {port} still LISTENING after kill wait",
            )
        sleeper(0.05)


def inject_wrap_creds_into_environ(
    handle: WrapHandle,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Runtime-only: put wrap creds in this process env for glue/doctor. Never logs values."""
    dest = environ if environ is not None else os.environ
    dest["OPENCODE_SERVER_USERNAME"] = handle.username
    dest["OPENCODE_SERVER_PASSWORD"] = handle.password
    dest["SUPER_AGENT_LOCAL_SESSION_KEY"] = handle.session_key
    dest[CREDS_SOURCE_ENV] = CREDS_SOURCE_STDIN_WRAP
    if not (dest.get("TELEAGENT_BASE_URL") or "").strip():
        dest["TELEAGENT_BASE_URL"] = handle.base_url
        handle.owns_base_url = True


def ensure_stdin_wrap(
    *,
    port: int | None = None,
    environ: Mapping[str, str] | None = None,
    spawn_fn: SpawnFn | None = None,
    ready_fn: ReadyFn | None = None,
    kernel_bin: str | Path | None = None,
    ready_timeout: float | None = None,
    is_file: Callable[[Path], bool] | None = None,
    listening_fn: Callable[[int], bool] | None = None,
    kill_listeners_fn: Callable[[int], None] | None = None,
    wait_port_free_timeout: float | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> WrapHandle:
    """Reuse a live wrap (pid file + /ready + in-memory secrets) or spawn one.

    Secrets are generated with ``secrets`` and kept in parent memory. Child OS
    environ is a non-secret whitelist. Tests may inject ``spawn_fn`` / ``ready_fn``
    and port-reclaim hooks (``listening_fn`` / ``kill_listeners_fn``).
    """
    global _LIVE, _LIVE_PROC
    env = environ if environ is not None else os.environ
    wrap_port = _resolve_port(port, env)
    base_url = f"http://127.0.0.1:{wrap_port}"
    check_ready: ReadyFn = ready_fn or (lambda url: _http_ready(url))

    if _LIVE is not None and _LIVE.port == wrap_port:
        alive = _pid_alive(_LIVE.pid)
        if not alive and _LIVE_PROC is not None:
            poll = getattr(_LIVE_PROC, "poll", None)
            alive = callable(poll) and poll() is None
        if alive:
            try:
                if check_ready(_LIVE.base_url):
                    return _LIVE
            except Exception:
                pass

    extra_pids: list[int] = []
    recorded = _read_pid_file(environ=env)
    if recorded is not None:
        rec_pid, rec_port = recorded
        if rec_port == wrap_port and _pid_alive(rec_pid):
            if _LIVE is not None and _LIVE.pid == rec_pid and _LIVE.password and _LIVE.session_key:
                try:
                    if check_ready(_LIVE.base_url):
                        return _LIVE
                except Exception:
                    pass
            extra_pids.append(rec_pid)
        _remove_pid_file(environ=env)

    # Never terminate an arbitrary process merely because it owns the desired
    # TCP port. We may stop a PID recorded in our own state file; callers may
    # inject a narrowly scoped reclaimer for tests or an external owner registry.
    # The live default waits and fails closed on an unknown owner.
    reclaim_os = spawn_fn is None or listening_fn is not None or kill_listeners_fn is not None
    if extra_pids or reclaim_os:
        for pid in extra_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if reclaim_os:
            killer = kill_listeners_fn
            if killer is not None:
                try:
                    killer(wrap_port)
                except Exception:
                    pass
            wait_listen = listening_fn
            if wait_listen is None and spawn_fn is None:
                wait_listen = _port_listening
            _wait_port_free(
                wrap_port,
                timeout=wait_port_free_timeout,
                sleep_fn=sleep_fn,
                listening_fn=wait_listen,
            )

    if kernel_bin is not None:
        bin_path = Path(kernel_bin)
        if spawn_fn is None:
            check = is_file or (lambda p: Path(p).is_file())
            if not check(bin_path):
                raise StdinWrapError(
                    CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING,
                    "TELEAGENT_KERNEL_BIN / kernel_bin is not a file. "
                    "Use runtimes/super-agent-code/bin/TeleAgent.exe "
                    "(not the Program Files GUI exe).",
                )
    else:
        bin_path = resolve_kernel_bin(environ=env, is_file=is_file)

    username = (env.get("OPENCODE_SERVER_USERNAME") or "").strip() or DEFAULT_BASIC_USER
    password = secrets.token_urlsafe(24)
    session_key = secrets.token_urlsafe(32)
    xdg = wrap_xdg_home(environ=env)
    try:
        xdg.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    payload_env = {
        "SERVER_PORT": str(wrap_port),
        "OPENCODE_SERVER_USERNAME": username,
        "OPENCODE_SERVER_PASSWORD": password,
        "SUPER_AGENT_LOCAL_SESSION_KEY": session_key,
        "SUPER_AGENT_SERVER_URL": base_url,
        "XDG_DATA_HOME": str(xdg),
    }
    child_extra: dict[str, str] = {}
    try:
        extras = prepare_gui_model_reuse(session_key, environ=env)
        if extras:
            payload_env.update(extras)
            for key in (OPENCODE_CONFIG_DIR_ENV, TELEAGENT_CONFIG_DIR_ENV):
                val = extras.get(key)
                if isinstance(val, str) and val:
                    child_extra[key] = val
    except GuiModelAuthError as e:
        raise StdinWrapError(
            CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING,
            "GUI TeleAgent model auth missing or unreadable; stdin_wrap not spawned",
        ) from e
    except Exception as e:
        raise StdinWrapError(
            CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING,
            "GUI TeleAgent model auth missing or unreadable; stdin_wrap not spawned",
        ) from e
    payload = build_env_payload(payload_env)
    child_env = build_child_os_env(
        parent=env,
        xdg_data_home=str(xdg),
        extra_nonsecret=child_extra or None,
    )

    spawn = spawn_fn or _default_spawn
    try:
        proc = spawn([str(bin_path)], stdin_payload=payload, env=child_env)
    except StdinWrapError:
        raise
    except Exception as e:
        raise StdinWrapError(
            CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
            f"stdin_wrap spawn failed: {e.__class__.__name__}",
        ) from e

    pid = int(getattr(proc, "pid", 0) or 0)
    if pid <= 0:
        _terminate_proc(proc)
        raise StdinWrapError(
            CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
            "stdin_wrap spawn failed: missing pid",
        )

    timeout = DEFAULT_READY_TIMEOUT_S if ready_timeout is None else float(ready_timeout)
    try:
        _wait_ready(base_url, ready_fn=check_ready, timeout=timeout, proc=proc)
    except StdinWrapError:
        _terminate_proc(proc)
        _remove_pid_file(environ=env)
        raise

    handle = WrapHandle(
        base_url=base_url,
        username=username,
        password=password,
        session_key=session_key,
        pid=pid,
        source=CREDS_SOURCE_STDIN_WRAP,
        port=wrap_port,
        owns_base_url=not bool((env.get("TELEAGENT_BASE_URL") or "").strip()),
    )
    _LIVE = handle
    _LIVE_PROC = proc
    _write_pid_file(pid, wrap_port, environ=env)
    return handle


def stop_stdin_wrap(*, handle: WrapHandle | None = None, environ: Mapping[str, str] | None = None) -> None:
    """Best-effort stop of the wrap kernel. For tests / cleanup. Never logs secrets."""
    global _LIVE, _LIVE_PROC
    target = handle or _LIVE
    proc = _LIVE_PROC if (handle is None or handle is _LIVE) else None
    if proc is not None:
        _terminate_proc(proc)
    elif target is not None and target.pid > 0:
        try:
            os.kill(target.pid, signal.SIGTERM)
        except OSError:
            pass
    _remove_pid_file(environ=environ)
    if handle is None or handle is _LIVE:
        _LIVE = None
        _LIVE_PROC = None


def reset_wrap_for_tests(*, environ: Mapping[str, str] | None = None) -> None:
    """Drop in-memory wrap state and pid file. Does not print secrets."""
    stop_stdin_wrap(environ=environ)


__all__ = [
    "CREDS_SOURCE_ENV",
    "DEFAULT_WRAP_PORT",
    "KERNEL_BIN_ENV",
    "SECRET_STDIN_KEYS",
    "StdinWrapError",
    "WRAP_PORT_ENV",
    "WrapHandle",
    "build_child_os_env",
    "build_env_payload",
    "current_wrap_handle",
    "ensure_stdin_wrap",
    "inject_wrap_creds_into_environ",
    "parse_env_payload",
    "reset_wrap_for_tests",
    "resolve_kernel_bin",
    "stop_stdin_wrap",
    "wrap_state_dir",
    "wrap_xdg_home",
]
