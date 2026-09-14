"""Windows exclusive file lock via LockFileEx (ctypes).

Loaded only by the platform factory on win32. This module is importable on
non-Windows hosts (kernel32 is resolved on first acquire) so unit tests can
exercise the adapter with mocks. That is not a real Windows host acceptance.

Share mode is FILE_SHARE_READ|FILE_SHARE_WRITE so a waiter can open the same
path and block/fail on LockFileEx (flock-equivalent), not on CreateFile.
"""
from __future__ import annotations

import ctypes
import errno
import os
from ctypes import wintypes
from pathlib import Path

from platform_services.file_lock import FileLockUnsupported, HeldFileLock
from platform_services.process import StdlibProcess

KIND = "windows"

LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80
ERROR_LOCK_VIOLATION = 33
ERROR_SHARING_VIOLATION = 32
ERROR_BUSY = 170

_ULONG_PTR = ctypes.c_size_t
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", _ULONG_PTR),
        ("InternalHigh", _ULONG_PTR),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def _overlapped() -> OVERLAPPED:
    ov = OVERLAPPED()
    ov.Internal = 0
    ov.InternalHigh = 0
    ov.Offset = 0
    ov.OffsetHigh = 0
    ov.hEvent = None
    return ov


_KERNEL32 = None


def _last_error() -> int:
    """GetLastError via ctypes. Patch in unit tests (Linux has no get_last_error)."""
    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        raise FileLockUnsupported("ctypes.get_last_error is unavailable")
    return int(getter())


def _kernel32():
    """Load kernel32 on first use. Patch this in unit tests."""
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        raise FileLockUnsupported("ctypes.WinDLL is unavailable on this host")
    try:
        k32 = windll("kernel32", use_last_error=True)
    except OSError as e:
        raise FileLockUnsupported(f"kernel32 is unavailable: {e}") from e
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]
    k32.LockFileEx.restype = wintypes.BOOL
    k32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]
    k32.UnlockFileEx.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32 = k32
    return k32


def reset_kernel32_for_tests() -> None:
    global _KERNEL32
    _KERNEL32 = None


class WindowsHeldLock(HeldFileLock):
    def __init__(self, handle: int, path: str) -> None:
        self.handle = handle
        self.path = path
        self._closed = False

    def unlock_and_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        k32 = _kernel32()
        try:
            ov = _overlapped()
            k32.UnlockFileEx(self.handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
        except OSError:
            pass
        try:
            k32.CloseHandle(self.handle)
        except OSError:
            pass

    def close_without_unlock(self) -> None:
        # Closing the handle drops this handle's LockFileEx; another handle
        # in this process (the in-memory holder) keeps its own lock.
        if self._closed:
            return
        self._closed = True
        try:
            _kernel32().CloseHandle(self.handle)
        except OSError:
            pass


class WindowsFileLock:
    """LockFileEx exclusive lock. Kernel drops it when the process exits."""

    name = "windows.lockfileex"

    def acquire(self, path: str | Path, *, blocking: bool = True) -> WindowsHeldLock:
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        k32 = _kernel32()
        handle = k32.CreateFileW(
            os.fspath(lock_path),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        hid = int(handle) if handle is not None else 0
        if handle is None or hid == _INVALID_HANDLE_VALUE or hid == -1:
            err = _last_error()
            raise OSError(err, f"CreateFileW failed for lock file: {lock_path}")
        flags = LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= LOCKFILE_FAIL_IMMEDIATELY
        ov = _overlapped()
        try:
            ok = k32.LockFileEx(handle, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
            if ok:
                return WindowsHeldLock(handle=hid, path=str(lock_path))
            err = _last_error()
            contended = err in (
                ERROR_LOCK_VIOLATION,
                ERROR_SHARING_VIOLATION,
                ERROR_BUSY,
            ) or (not blocking and err == 0)
            if contended:
                raise BlockingIOError(errno.EWOULDBLOCK, "LockFileEx would block")
            raise OSError(err, f"LockFileEx failed: {err}")
        except BaseException:
            try:
                k32.CloseHandle(handle)
            except OSError:
                pass
            raise


class WindowsProcess(StdlibProcess):
    name = "windows.process"
