"""Exclusive file-lock contract. Implementations load per host (see factory)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol


class FileLockUnsupported(RuntimeError):
    """Raised when this host cannot take a cross-process exclusive file lock."""


class HeldFileLock(ABC):
    """One exclusive hold on a lock file. Kernel releases it if the process dies."""

    path: str

    @abstractmethod
    def unlock_and_close(self) -> None:
        """Release the exclusive lock and close the descriptor."""

    @abstractmethod
    def close_without_unlock(self) -> None:
        """Close this descriptor without an explicit unlock syscall.

        Used when a same-process holder already owns the lock and unlocking
        this extra descriptor would drop that holder's lock (POSIX flock).
        """


class FileLockBackend(Protocol):
    """Open a path and take an exclusive lock. Never a no-op."""

    name: str

    def acquire(self, path: str | Path, *, blocking: bool = True) -> HeldFileLock:
        """Create/open ``path`` and lock it exclusively.

        ``blocking=False`` raises ``BlockingIOError`` if another holder has it.
        Failures other than contention raise ``OSError`` / ``FileLockUnsupported``.
        """
        ...
