"""Keep test desktop claims off the real LOCALAPPDATA lock directory.

``lock_root()`` honors ``TELEAGENT_DESKTOP_LOCK_DIR``. Call
``install_desktop_lock_isolation`` from a test ``setUp``. The previous
environment value is restored after the test, including when ``setUp`` fails
after the call. In-process holds are released first so Windows can delete
the temp lock files.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

ENV = "TELEAGENT_DESKTOP_LOCK_DIR"


def real_desktop_lock_root() -> Path:
    """Default lock directory, ignoring ``TELEAGENT_DESKTOP_LOCK_DIR``."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "teleagent-collab" / "desktop-locks"
    return Path.home() / ".local" / "share" / "teleagent-collab" / "desktop-locks"


def lock_dir_snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Relative path, size, and mtime for every file under ``root``."""
    if not root.exists():
        return ()
    if not root.is_dir():
        st = root.stat()
        return ((".", int(st.st_size), int(st.st_mtime_ns)),)
    rows: list[tuple[str, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            try:
                st = path.stat()
            except OSError:
                rows.append((rel, -1, -1))
            else:
                rows.append((rel, int(st.st_size), int(st.st_mtime_ns)))
    return tuple(rows)


def install_desktop_lock_isolation(test: unittest.TestCase) -> Path:
    """Point desktop locks at a per-test temp dir until the test cleans up."""
    previous = os.environ.get(ENV)
    tmp = tempfile.TemporaryDirectory(prefix="teleagent-desktop-lock-")
    os.environ[ENV] = tmp.name
    path = Path(tmp.name)
    test.desktop_lock_dir = path

    def restore() -> None:
        try:
            from win_collab.desktop_lock import reset_desktop_locks_for_tests

            reset_desktop_locks_for_tests()
        finally:
            if previous is None:
                os.environ.pop(ENV, None)
            else:
                os.environ[ENV] = previous
            tmp.cleanup()

    test.addCleanup(restore)
    return path
