"""PlatformServices: OS lock and process primitives.

Public kernel (framework, execution_backend) must not import fcntl,
msvcrt, or win32 APIs at module top. Implementations load per host.

macOS may load the POSIX flock backend (fcntl exists) but is not a
claimed/tested host for this knife.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

from platform_services.file_lock import (
    FileLockBackend,
    FileLockUnsupported,
    HeldFileLock,
)
from platform_services.process import ProcessBackend, StdlibProcess

_MU = threading.Lock()
_CACHE: dict[str, PlatformServices] = {}


@dataclass(frozen=True)
class PlatformServices:
    kind: str
    file_lock: FileLockBackend
    process: ProcessBackend


def _normalize_platform(platform: str | None) -> str:
    return (platform or sys.platform).lower()


_POSIX_PREFIXES = (
    "linux",
    "darwin",
    "freebsd",
    "netbsd",
    "openbsd",
    "cygwin",
)


def _load(key: str) -> PlatformServices:
    if key.startswith("win"):
        from platform_services import windows as impl

        return PlatformServices(
            kind=impl.KIND,
            file_lock=impl.WindowsFileLock(),
            process=impl.WindowsProcess(),
        )
    if not key.startswith(_POSIX_PREFIXES):
        raise FileLockUnsupported(
            f"no exclusive file-lock implementation for platform {key!r}; "
            "refusing a no-op lock"
        )
    # POSIX hosts. fcntl is required — no no-op if the module cannot load.
    try:
        from platform_services import posix as impl
    except ImportError as e:
        raise FileLockUnsupported(
            f"POSIX file lock requires fcntl; unavailable on platform {key!r}: {e}"
        ) from e
    return PlatformServices(
        kind=impl.KIND,
        file_lock=impl.PosixFileLock(),
        process=impl.PosixProcess(),
    )


def get_platform_services(*, platform: str | None = None) -> PlatformServices:
    """Lazy per-platform load. Unknown hosts raise; never a lock no-op."""
    key = _normalize_platform(platform)
    with _MU:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        loaded = _load(key)
        _CACHE[key] = loaded
        return loaded


def get_file_lock(*, platform: str | None = None) -> FileLockBackend:
    return get_platform_services(platform=platform).file_lock


def reset_platform_services() -> None:
    """Test helper: drop cached backends."""
    with _MU:
        _CACHE.clear()


def set_platform_services_for_tests(
    svc: PlatformServices | None,
    *,
    platform: str | None = None,
) -> None:
    """Test helper: inject a backend for this platform key. Not a host acceptance."""
    key = _normalize_platform(platform)
    with _MU:
        if svc is None:
            _CACHE.pop(key, None)
        else:
            _CACHE[key] = svc


def current_pid() -> int:
    return get_platform_services().process.current_pid()


def current_thread_id() -> int:
    return get_platform_services().process.current_thread_id()


def get_cmd_wrapper(*, platform: str | None = None):
    """Windows .cmd/.bat wrapper (owned by windows_cli). None on POSIX.

    Raises PlatformWrapperRequired on Windows when the module is absent;
    never a silent fallback to shell=True.
    """
    from platform_services.cli import PlatformWrapperRequired

    key = _normalize_platform(platform)
    if not key.startswith("win"):
        return None
    try:
        from platform_services import windows_cli
    except ImportError as e:
        raise PlatformWrapperRequired(
            "platform_services.windows_cli not available on this build"
        ) from e
    return windows_cli.CmdWrapper()


def capability_report(*, platform: str | None = None) -> dict[str, str]:
    """What this host provides. Unknown hosts raise via the factory."""
    key = _normalize_platform(platform)
    svc = get_platform_services(platform=platform)
    cmd = "n/a"
    if key.startswith("win"):
        try:
            get_cmd_wrapper(platform=platform)
            cmd = "available"
        except NotImplementedError:
            cmd = "missing"
    return {
        "platform": key,
        "kind": svc.kind,
        "file_lock": svc.file_lock.name,
        "process": svc.process.name,
        "cli_cmd_wrapper": cmd,
        "path_inspect": "lstat_walk",
    }


def try_acquire_exclusive(path: str, *, blocking: bool = False) -> HeldFileLock:
    return get_file_lock().acquire(path, blocking=blocking)


__all__ = [
    "FileLockBackend",
    "FileLockUnsupported",
    "HeldFileLock",
    "PlatformServices",
    "ProcessBackend",
    "StdlibProcess",
    "capability_report",
    "current_pid",
    "current_thread_id",
    "get_cmd_wrapper",
    "get_file_lock",
    "get_platform_services",
    "reset_platform_services",
    "set_platform_services_for_tests",
    "try_acquire_exclusive",
]
