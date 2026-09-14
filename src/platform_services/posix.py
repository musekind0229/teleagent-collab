"""POSIX exclusive file lock via fcntl.flock.

Loaded only by the platform factory on non-Windows hosts. Public kernel
modules must not import this module (or fcntl) at their top level.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from platform_services.file_lock import HeldFileLock
from platform_services.process import StdlibProcess

KIND = "posix"


class PosixHeldLock(HeldFileLock):
    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path
        self._closed = False

    def unlock_and_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def close_without_unlock(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass


class PosixFileLock:
    """fcntl.flock(LOCK_EX). Kernel drops the lock when the process exits."""

    name = "posix.fcntl_flock"

    def acquire(self, path: str | Path, *, blocking: bool = True) -> PosixHeldLock:
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return PosixHeldLock(fd=fd, path=str(lock_path))


class PosixProcess(StdlibProcess):
    name = "posix.process"
