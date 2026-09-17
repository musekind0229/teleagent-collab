"""Read local-v1 creds from other Windows processes' environment blocks.

Linux analog: ``linux_local_v1.default_find_creds`` scans ``/proc/*/environ``.
Windows has no /proc; this module uses Win32 (ctypes) ``OpenProcess`` +
``NtQueryInformationProcess`` (PEB → ``RTL_USER_PROCESS_PARAMETERS.Environment``)
+ ``ReadProcessMemory``.

Non-Windows: high-level finders no-op (return None / empty). The PEB reader
raises ``WindowsEnvironUnavailable`` so Linux callers are not silently broken.

Never scrapes Credential Manager, never disables auth, never logs secret values.
Session key lives in process memory (restart rotates it). No disk fallback for
password/HMAC — official layouts may hold OAuth ``token.json`` under
``%APPDATA%\\TeleAgent`` / ``%LOCALAPPDATA%\\TeleAgent`` (do not read).
"""
from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from teleagent_adapter.base import AdapterError, AdapterStatus

# Candidate filters (name or image path, case-insensitive).
_NAME_NEEDLES: tuple[str, ...] = (
    "teleagent",
    "opencode",
    "super-agent-code",
)
_PATH_NEEDLES: tuple[str, ...] = (
    "teleagent",
    "super-agent-code",
    "opencode",
    "super-agent",
)

USERNAME_KEYS: tuple[str, ...] = (
    "OPENCODE_SERVER_USERNAME",
    "SUPER_AGENT_OPENCODE_USERNAME",
)
PASSWORD_KEYS: tuple[str, ...] = (
    "OPENCODE_SERVER_PASSWORD",
    "SUPER_AGENT_OPENCODE_PASSWORD",
)
SESSION_KEY_KEYS: tuple[str, ...] = ("SUPER_AGENT_LOCAL_SESSION_KEY",)

DEFAULT_BASIC_USER = "super-agent"

# PEB / RTL_USER_PROCESS_PARAMETERS pointer offsets.
_PEB_PROCESS_PARAMETERS_X64 = 0x20
_PEB_PROCESS_PARAMETERS_X86 = 0x10
_PARAMS_ENVIRONMENT_X64 = 0x80
_PARAMS_ENVIRONMENT_X86 = 0x48

_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_BASIC_INFORMATION_CLASS = 0  # NtQuery ProcessInformationClass
_PROCESS_WOW64_INFORMATION = 26
_MAX_ENV_BYTES = 128 * 1024
_ENV_CHUNK = 4096

CREDS_SOURCE_PROCESS_ENV = "process_env"
CREDS_SOURCE_FOREIGN = "foreign_process_environ"
CREDS_SOURCE_MISSING = "missing"

MISSING_CREDS_MESSAGE = (
    "Windows TeleAgent local API creds not found in this process environment "
    "or in other TeleAgent/SAC process environ blocks (Win32 PEB read; "
    "not this-process env). "
    "Need OPENCODE_SERVER_PASSWORD + SUPER_AGENT_LOCAL_SESSION_KEY. "
    "Do not disable authentication."
)


class WindowsEnvironUnavailable(RuntimeError):
    """Win32 process-environ APIs are unavailable (non-Windows or missing DLL)."""


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    name: str
    image_path: str = ""


@dataclass(frozen=True)
class WindowsCredsPresence:
    """Host creds metadata for doctor extras — never includes secret values."""

    source: str
    password_present: bool
    session_key_present: bool
    username_present: bool = False
    foreign_candidates: int = 0


def _is_windows(platform: str | None = None) -> bool:
    plat = (platform or sys.platform).lower()
    return plat.startswith("win")


def _first_env(env: Mapping[str, str], names: Sequence[str]) -> str:
    for n in names:
        v = env.get(n)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def extract_local_v1_creds(env: Mapping[str, str]) -> tuple[str, str, str] | None:
    """Return (user, password, session_key) only when password and key are both set."""
    user = _first_env(env, USERNAME_KEYS) or DEFAULT_BASIC_USER
    pw = _first_env(env, PASSWORD_KEYS)
    key = _first_env(env, SESSION_KEY_KEYS)
    if not pw or not key:
        return None
    return user, pw, key


def encode_environ_block(mapping: Mapping[str, str], *, wide: bool = True) -> bytes:
    """NUL-separated KEY=VALUE block. UTF-16LE when ``wide`` (native Win32), else UTF-8."""
    items = [f"{k}={v}" for k, v in mapping.items() if k]
    if wide:
        return ("\x00".join(items) + "\x00\x00").encode("utf-16-le")
    return b"\x00".join(x.encode("utf-8") for x in items) + b"\x00\x00"


def _looks_utf16_le(data: bytes) -> bool:
    if len(data) >= 4 and data[1] == 0 and data[3] == 0:
        return True
    return b"=\x00" in data[:256]


def parse_environ_block(data: bytes | str) -> dict[str, str]:
    """Parse a NUL-separated KEY=VALUE environ block (UTF-16LE or UTF-8)."""
    if not data:
        return {}
    if isinstance(data, str):
        parts = data.split("\x00")
    elif _looks_utf16_le(data):
        parts = data.decode("utf-16-le", errors="replace").split("\x00")
    else:
        parts = []
        for item in data.split(b"\x00"):
            if not item:
                continue
            try:
                parts.append(item.decode("utf-8"))
            except UnicodeDecodeError:
                parts.append(item.decode("latin-1", errors="replace"))
    env: dict[str, str] = {}
    for item in parts:
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        if k:
            env[k] = v
    return env


def is_teleagent_candidate(name: str, image_path: str = "") -> bool:
    n = (name or "").lower()
    p = (image_path or "").lower().replace("/", "\\")
    if n.endswith("teleagent.exe") or n == "teleagent.exe":
        return True
    for needle in _NAME_NEEDLES:
        if needle in n:
            return True
    for needle in _PATH_NEEDLES:
        if needle in p:
            return True
    return False


def find_creds_in_environ_blocks(blocks: Iterable[bytes | str]) -> tuple[str, str, str] | None:
    """Parse injected environ blocks (tests / fake enumerator). First complete creds win."""
    for raw in blocks:
        try:
            env = parse_environ_block(raw)
        except Exception:
            continue
        creds = extract_local_v1_creds(env)
        if creds is not None:
            return creds
    return None


# --- Win32 (lazy); importable on Linux so tests can mock the enumerator ---

_KERNEL32: Any = None
_NTDLL: Any = None


def reset_win32_for_tests() -> None:
    global _KERNEL32, _NTDLL
    _KERNEL32 = None
    _NTDLL = None


def _win_dll(name: str) -> Any:
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        raise WindowsEnvironUnavailable("ctypes.WinDLL is unavailable on this host")
    try:
        return windll(name, use_last_error=True)
    except OSError as e:
        raise WindowsEnvironUnavailable(f"{name} is unavailable: {e}") from e


class _PROCESS_BASIC_INFORMATION_STRUCT(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _kernel32() -> Any:
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    k32 = _win_dll("kernel32")
    from ctypes import wintypes

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k32.IsWow64Process.restype = wintypes.BOOL
    _KERNEL32 = k32
    return k32


def _ntdll() -> Any:
    global _NTDLL
    if _NTDLL is not None:
        return _NTDLL
    ntdll = _win_dll("ntdll")
    from ctypes import wintypes

    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _NTDLL = ntdll
    return ntdll


def _valid_user_ptr(addr: int, ptr_size: int) -> bool:
    if addr == 0:
        return False
    if ptr_size == 8:
        return 0x10000 <= addr <= 0x7FFFFFFFFFFF
    return 0x10000 <= addr <= 0x7FFFFFFF


def _read_bytes(k32: Any, handle: Any, address: int, size: int) -> bytes | None:
    if size <= 0 or not address:
        return None
    buf = ctypes.create_string_buffer(size)
    nread = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(
        handle,
        ctypes.c_void_p(address),
        buf,
        size,
        ctypes.byref(nread),
    )
    if not ok or nread.value == 0:
        return None
    return buf.raw[: nread.value]


def _read_ptr(k32: Any, handle: Any, address: int, ptr_size: int) -> int | None:
    raw = _read_bytes(k32, handle, address, ptr_size)
    if not raw or len(raw) < ptr_size:
        return None
    val = int.from_bytes(raw[:ptr_size], "little")
    if not _valid_user_ptr(val, ptr_size):
        return None
    return val


def _is_wow64(k32: Any, handle: Any) -> bool:
    from ctypes import wintypes

    flag = wintypes.BOOL(0)
    try:
        if k32.IsWow64Process(handle, ctypes.byref(flag)) and flag.value:
            return True
    except OSError:
        return False
    return False


def _peb_base(ntdll: Any, handle: Any, *, wow64: bool, ptr_size: int) -> int | None:
    retlen = ctypes.c_ulong(0)
    if wow64 and ptr_size == 8:
        wow = ctypes.c_void_p()
        status = ntdll.NtQueryInformationProcess(
            handle,
            _PROCESS_WOW64_INFORMATION,
            ctypes.byref(wow),
            ctypes.sizeof(wow),
            ctypes.byref(retlen),
        )
        if status == 0 and wow.value:
            addr = int(wow.value)
            if _valid_user_ptr(addr, 4):
                return addr
        return None
    pbi = _PROCESS_BASIC_INFORMATION_STRUCT()
    status = ntdll.NtQueryInformationProcess(
        handle,
        _PROCESS_BASIC_INFORMATION_CLASS,
        ctypes.byref(pbi),
        ctypes.sizeof(pbi),
        ctypes.byref(retlen),
    )
    if status != 0 or not pbi.PebBaseAddress:
        return None
    addr = int(pbi.PebBaseAddress)
    native_ptr = ctypes.sizeof(ctypes.c_void_p)
    if not _valid_user_ptr(addr, native_ptr):
        return None
    return addr


def _read_environ_block(k32: Any, handle: Any, env_ptr: int) -> bytes | None:
    acc = bytearray()
    while len(acc) < _MAX_ENV_BYTES:
        chunk = _read_bytes(
            k32,
            handle,
            env_ptr + len(acc),
            min(_ENV_CHUNK, _MAX_ENV_BYTES - len(acc)),
        )
        if not chunk:
            break
        acc.extend(chunk)
        marker = bytes(acc).find(b"\x00\x00\x00\x00")
        if marker >= 0:
            return bytes(acc[: marker + 4])
        if len(chunk) < _ENV_CHUNK:
            break
    return bytes(acc) if acc else None


def read_process_environ_via_peb(pid: int) -> bytes:
    """Read a process environment block via PEB. Raises on non-Windows."""
    if not _is_windows():
        raise WindowsEnvironUnavailable(
            "read_process_environ_via_peb requires Win32 "
            "(OpenProcess + NtQueryInformationProcess + ReadProcessMemory); "
            "this host is not Windows"
        )
    if pid <= 4:
        raise WindowsEnvironUnavailable(f"refusing to read system pid {pid}")
    k32 = _kernel32()
    ntdll = _ntdll()
    access = _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ | _PROCESS_QUERY_LIMITED_INFORMATION
    handle = k32.OpenProcess(access, False, int(pid))
    hid = int(handle) if handle is not None else 0
    if handle is None or hid in (0, -1):
        raise WindowsEnvironUnavailable(f"OpenProcess failed for pid {pid}")
    try:
        wow64 = _is_wow64(k32, handle)
        native_ptr = ctypes.sizeof(ctypes.c_void_p)
        target_ptr = 4 if wow64 else native_ptr
        peb = _peb_base(ntdll, handle, wow64=wow64, ptr_size=native_ptr)
        if peb is None:
            raise WindowsEnvironUnavailable(f"PEB not readable for pid {pid}")
        peb_off = _PEB_PROCESS_PARAMETERS_X86 if target_ptr == 4 else _PEB_PROCESS_PARAMETERS_X64
        params = _read_ptr(k32, handle, peb + peb_off, target_ptr)
        if params is None:
            raise WindowsEnvironUnavailable(f"ProcessParameters not readable for pid {pid}")
        env_off = _PARAMS_ENVIRONMENT_X86 if target_ptr == 4 else _PARAMS_ENVIRONMENT_X64
        env_ptr = _read_ptr(k32, handle, params + env_off, target_ptr)
        if env_ptr is None:
            raise WindowsEnvironUnavailable(f"Environment pointer not readable for pid {pid}")
        block = _read_environ_block(k32, handle, env_ptr)
        if not block:
            raise WindowsEnvironUnavailable(f"empty environment block for pid {pid}")
        return block
    finally:
        try:
            k32.CloseHandle(handle)
        except OSError:
            pass


def _image_path_for_pid(k32: Any, pid: int) -> str:
    from ctypes import wintypes

    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    hid = int(handle) if handle is not None else 0
    if handle is None or hid in (0, -1):
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value or ""
        return ""
    except OSError:
        return ""
    finally:
        try:
            k32.CloseHandle(handle)
        except OSError:
            pass


def enumerate_windows_processes() -> list[ProcessSnapshot]:
    """Toolhelp snapshot. No-op (empty list) on non-Windows."""
    if not _is_windows():
        return []
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    hid = int(snap) if snap is not None else 0
    invalid = {0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}
    if snap is None or hid in invalid:
        return []
    out: list[ProcessSnapshot] = []
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(pe)):
            return []
        while True:
            pid = int(pe.th32ProcessID)
            name = pe.szExeFile or ""
            path = ""
            if pid > 4:
                try:
                    path = _image_path_for_pid(k32, pid)
                except OSError:
                    path = ""
            out.append(ProcessSnapshot(pid=pid, name=name, image_path=path))
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
        return out
    finally:
        try:
            k32.CloseHandle(snap)
        except OSError:
            pass


def _default_environ_reader(pid: int) -> bytes | None:
    try:
        return read_process_environ_via_peb(pid)
    except WindowsEnvironUnavailable:
        return None
    except OSError:
        return None


def find_creds_in_foreign_processes(
    *,
    enumerator: Callable[[], Iterable[ProcessSnapshot]] | None = None,
    environ_reader: Callable[[int], bytes | None] | None = None,
    environ_blocks: Iterable[bytes | str] | None = None,
    skip_pid: int | None = None,
) -> tuple[str, str, str] | None:
    """Scan TeleAgent/SAC/opencode processes for local-v1 creds.

    On non-Windows with no injected enumerator/blocks: no-op (None).
    """
    if environ_blocks is not None:
        return find_creds_in_environ_blocks(environ_blocks)

    if enumerator is None and not _is_windows():
        return None

    snaps = list((enumerator or enumerate_windows_processes)())
    skip = os.getpid() if skip_pid is None else skip_pid
    reader = environ_reader or _default_environ_reader
    for snap in snaps:
        if snap.pid == skip or snap.pid <= 4:
            continue
        if not is_teleagent_candidate(snap.name, snap.image_path):
            continue
        try:
            raw = reader(snap.pid)
        except Exception:
            continue
        if not raw:
            continue
        creds = extract_local_v1_creds(parse_environ_block(raw))
        if creds is not None:
            return creds
    return None


def count_foreign_candidates(
    *,
    enumerator: Callable[[], Iterable[ProcessSnapshot]] | None = None,
    skip_pid: int | None = None,
) -> int:
    if enumerator is None and not _is_windows():
        return 0
    skip = os.getpid() if skip_pid is None else skip_pid
    n = 0
    for snap in enumerator() if enumerator is not None else enumerate_windows_processes():
        if snap.pid == skip or snap.pid <= 4:
            continue
        if is_teleagent_candidate(snap.name, snap.image_path):
            n += 1
    return n


def resolve_windows_local_v1_creds(
    *,
    environ: Mapping[str, str] | None = None,
    foreign_finder: Callable[[], tuple[str, str, str] | None] | None = None,
    enumerator: Callable[[], Iterable[ProcessSnapshot]] | None = None,
    environ_reader: Callable[[int], bytes | None] | None = None,
    environ_blocks: Iterable[bytes | str] | None = None,
    skip_pid: int | None = None,
) -> tuple[tuple[str, str, str] | None, WindowsCredsPresence]:
    """This-process env first, then foreign TeleAgent/SAC process environ."""
    env = environ if environ is not None else os.environ
    pw_here = bool(_first_env(env, PASSWORD_KEYS))
    key_here = bool(_first_env(env, SESSION_KEY_KEYS))
    user_here = bool(_first_env(env, USERNAME_KEYS))
    here = extract_local_v1_creds(env)
    if here is not None:
        return here, WindowsCredsPresence(
            source=CREDS_SOURCE_PROCESS_ENV,
            password_present=True,
            session_key_present=True,
            username_present=True,
        )

    n_cand = 0
    try:
        n_cand = count_foreign_candidates(enumerator=enumerator, skip_pid=skip_pid)
    except Exception:
        n_cand = 0

    foreign: tuple[str, str, str] | None = None
    try:
        if foreign_finder is not None:
            foreign = foreign_finder()
        else:
            foreign = find_creds_in_foreign_processes(
                enumerator=enumerator,
                environ_reader=environ_reader,
                environ_blocks=environ_blocks,
                skip_pid=skip_pid,
            )
    except AdapterError:
        foreign = None
    except Exception:
        foreign = None

    if foreign is not None:
        user, pw, key = foreign
        if pw and key:
            return (user or DEFAULT_BASIC_USER, pw, key), WindowsCredsPresence(
                source=CREDS_SOURCE_FOREIGN,
                password_present=True,
                session_key_present=True,
                username_present=True,
                foreign_candidates=n_cand,
            )

    return None, WindowsCredsPresence(
        source=CREDS_SOURCE_MISSING,
        password_present=pw_here,
        session_key_present=key_here,
        username_present=user_here,
        foreign_candidates=n_cand,
    )


def probe_windows_creds_presence(
    *,
    environ: Mapping[str, str] | None = None,
    foreign_finder: Callable[[], tuple[str, str, str] | None] | None = None,
    enumerator: Callable[[], Iterable[ProcessSnapshot]] | None = None,
    environ_reader: Callable[[int], bytes | None] | None = None,
    environ_blocks: Iterable[bytes | str] | None = None,
    skip_pid: int | None = None,
) -> WindowsCredsPresence:
    """Doctor-facing probe: source + bool presence, never secret values."""
    _creds, presence = resolve_windows_local_v1_creds(
        environ=environ,
        foreign_finder=foreign_finder,
        enumerator=enumerator,
        environ_reader=environ_reader,
        environ_blocks=environ_blocks,
        skip_pid=skip_pid,
    )
    return presence


__all__ = [
    "CREDS_SOURCE_FOREIGN",
    "CREDS_SOURCE_MISSING",
    "CREDS_SOURCE_PROCESS_ENV",
    "DEFAULT_BASIC_USER",
    "MISSING_CREDS_MESSAGE",
    "PASSWORD_KEYS",
    "SESSION_KEY_KEYS",
    "USERNAME_KEYS",
    "ProcessSnapshot",
    "WindowsCredsPresence",
    "WindowsEnvironUnavailable",
    "count_foreign_candidates",
    "encode_environ_block",
    "enumerate_windows_processes",
    "extract_local_v1_creds",
    "find_creds_in_environ_blocks",
    "find_creds_in_foreign_processes",
    "is_teleagent_candidate",
    "parse_environ_block",
    "probe_windows_creds_presence",
    "read_process_environ_via_peb",
    "reset_win32_for_tests",
    "resolve_windows_local_v1_creds",
]
