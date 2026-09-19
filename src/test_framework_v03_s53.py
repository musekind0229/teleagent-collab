#!/usr/bin/env python3
"""v0.3 §5.3: platform file lock (workdir_claim + persist_lock).

Linux host: cross-process mutex, distinct-dir parallelism, crash recovery
(flock released on process death; occupancy JSON is metadata).

Windows: LockFileEx adapter is implemented and unit-tested with mocks.
Windows import/lock semantics were NOT verified on a real Windows host.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.workdir_claim import (  # noqa: E402
    STATUS_BLOCKED,
    WorkdirClaimRegistry,
    occupancy_path,
)
from framework.persist_lock import persist_lock  # noqa: E402
from platform_services import (  # noqa: E402
    FileLockUnsupported,
    PlatformServices,
    get_file_lock,
    get_platform_services,
    reset_platform_services,
    set_platform_services_for_tests,
)
from platform_services.process import StdlibProcess  # noqa: E402

# Independent-process worker. sys.argv[1] is src/, then op + args.
_WORKER = r"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

src = sys.argv[1]
sys.path.insert(0, src)
op = sys.argv[2]

def dump(obj):
    print(json.dumps(obj, default=str), flush=True)

if op == "claim_hold":
    from execution_backend.workdir_claim import WorkdirClaimRegistry
    wd, stall = sys.argv[3], sys.argv[4]
    hid = sys.argv[5] if len(sys.argv) > 5 else "hold"
    reg = WorkdirClaimRegistry()
    out = reg.claim(wd, holder_id=hid, job_name=hid, mode="block")
    Path(stall).write_text("ready" if out.granted else "fail", encoding="utf-8")
    if not out.granted:
        dump({"granted": False, "status": out.status, "error": out.error})
        raise SystemExit(0)
    deadline = time.time() + 20
    while time.time() < deadline:
        if Path(stall).read_text(encoding="utf-8").strip() == "go":
            break
        time.sleep(0.02)
    else:
        raise SystemExit("stall timeout")
    reg.release(wd, hid)
    dump({"granted": True, "pid": os.getpid()})
elif op == "claim_try":
    from execution_backend.workdir_claim import WorkdirClaimRegistry
    wd = sys.argv[3]
    hid = sys.argv[4] if len(sys.argv) > 4 else "try"
    reg = WorkdirClaimRegistry()
    out = reg.claim(wd, holder_id=hid, job_name=hid, mode="block")
    dump({
        "granted": out.granted,
        "status": out.status,
        "holder": (out.occupancy.holder_id if out.occupancy else None),
        "pid": os.getpid(),
    })
    if out.granted:
        reg.release(wd, hid)
elif op == "claim_and_die":
    from execution_backend.workdir_claim import WorkdirClaimRegistry
    wd, stall = sys.argv[3], sys.argv[4]
    reg = WorkdirClaimRegistry()
    out = reg.claim(wd, holder_id="dead", job_name="dead")
    if not out.granted:
        raise SystemExit("claim failed")
    Path(stall).write_text("ready", encoding="utf-8")
    os._exit(1)
elif op == "persist_hold":
    from framework.persist_lock import persist_lock
    root, stall = sys.argv[3], sys.argv[4]
    with persist_lock(root):
        Path(stall).write_text("ready", encoding="utf-8")
        deadline = time.time() + 20
        while time.time() < deadline:
            if Path(stall).read_text(encoding="utf-8").strip() == "go":
                break
            time.sleep(0.02)
        else:
            raise SystemExit("stall timeout")
    dump({"ok": True})
elif op == "persist_enter":
    from framework.persist_lock import persist_lock
    root, stall = sys.argv[3], sys.argv[4]
    with persist_lock(root):
        Path(stall).write_text("entered", encoding="utf-8")
    dump({"ok": True})
else:
    raise SystemExit("unknown op " + op)
"""


def _spawn(args: list[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(SRC), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_text(path: Path, expected: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == expected:
            return
        time.sleep(0.02)
    have = path.read_text(encoding="utf-8") if path.is_file() else None
    raise AssertionError(f"timed out waiting for {path} == {expected!r} (have {have!r})")


def _finish(proc: subprocess.Popen, timeout: float = 20.0) -> dict:
    out, err = proc.communicate(timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"worker rc={proc.returncode} stdout={out!r} stderr={err!r}")
    line = (out or "").strip().splitlines()[-1]
    return json.loads(line)


def _top_level_imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class TestNoTopLevelFcntl(unittest.TestCase):
    def test_public_modules_do_not_import_fcntl_at_top_level(self):
        files = [
            SRC / "execution_backend" / "workdir_claim.py",
            SRC / "execution_backend" / "__init__.py",
            SRC / "framework" / "persist_lock.py",
            SRC / "platform_services" / "__init__.py",
            SRC / "platform_services" / "file_lock.py",
            SRC / "platform_services" / "process.py",
            SRC / "platform_services" / "windows.py",
        ]
        for path in files:
            names = _top_level_imported_roots(path)
            self.assertNotIn("fcntl", names, path)

    def test_posix_backend_is_the_only_fcntl_import(self):
        names = _top_level_imported_roots(SRC / "platform_services" / "posix.py")
        self.assertIn("fcntl", names)

    def test_import_execution_backend_without_fcntl(self):
        """Fresh interpreter: public imports succeed even if fcntl is blocked."""
        code = r"""
import builtins, sys
src = sys.argv[1]
real = builtins.__import__
def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl" or name.startswith("fcntl."):
        raise ImportError("fcntl blocked for §5.3 import test")
    return real(name, globals, locals, fromlist, level)
builtins.__import__ = blocked
sys.modules.pop("fcntl", None)
sys.path.insert(0, src)
import execution_backend
import execution_backend.workdir_claim as wc
import framework.persist_lock as pl
from execution_backend import InProcessExecutionBackend, get_execution_backend
assert "fcntl" not in vars(wc)
assert "fcntl" not in vars(pl)
be = get_execution_backend("inprocess")
assert isinstance(be, InProcessExecutionBackend)
print("imported")
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", code, str(SRC)],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("imported", proc.stdout)


class TestFactoryLoadsPerPlatform(unittest.TestCase):
    def tearDown(self):
        reset_platform_services()

    def test_this_host_is_not_a_noop(self):
        reset_platform_services()
        svc = get_platform_services()
        if sys.platform.startswith("win"):
            self.assertEqual(svc.kind, "windows")
            self.assertEqual(svc.file_lock.name, "windows.lockfileex")
        else:
            self.assertEqual(svc.kind, "posix")
            self.assertEqual(svc.file_lock.name, "posix.fcntl_flock")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.lock"
            held = svc.file_lock.acquire(path, blocking=True)
            try:
                with self.assertRaises(BlockingIOError):
                    svc.file_lock.acquire(path, blocking=False)
            finally:
                held.unlock_and_close()

    def test_factory_selects_windows_backend_on_win32(self):
        reset_platform_services()
        svc = get_platform_services(platform="win32")
        self.assertEqual(svc.kind, "windows")
        self.assertEqual(svc.file_lock.name, "windows.lockfileex")

    def test_unknown_platform_raises_not_noop(self):
        reset_platform_services()
        with self.assertRaises(FileLockUnsupported) as ctx:
            get_platform_services(platform="plan9")
        self.assertIn("no-op", str(ctx.exception).lower())


class TestWorkdirClaimCrossProcess(unittest.TestCase):
    def test_same_dir_two_processes_mutex(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "ws"
            wd.mkdir()
            stall = Path(td) / "stall"
            holder = _spawn(["claim_hold", str(wd), str(stall), "alpha"])
            _wait_text(stall, "ready")
            try_b = _spawn(["claim_try", str(wd), "beta"])
            rb = _finish(try_b)
            self.assertFalse(rb["granted"], rb)
            self.assertEqual(rb["status"], STATUS_BLOCKED)
            self.assertEqual(rb["holder"], "alpha")
            self.assertIsNone(holder.poll(), "holder must still be alive while B is refused")
            stall.write_text("go", encoding="utf-8")
            ra = _finish(holder)
            self.assertTrue(ra["granted"], ra)
            try_c = _spawn(["claim_try", str(wd), "gamma"])
            rc = _finish(try_c)
            self.assertTrue(rc["granted"], rc)

    def test_distinct_dirs_two_processes_hold_in_parallel(self):
        with tempfile.TemporaryDirectory() as td:
            d1 = Path(td) / "a"
            d2 = Path(td) / "b"
            d1.mkdir()
            d2.mkdir()
            s1 = Path(td) / "s1"
            s2 = Path(td) / "s2"
            p1 = _spawn(["claim_hold", str(d1), str(s1), "a"])
            p2 = _spawn(["claim_hold", str(d2), str(s2), "b"])
            _wait_text(s1, "ready")
            _wait_text(s2, "ready")
            self.assertIsNone(p1.poll())
            self.assertIsNone(p2.poll())
            s1.write_text("go", encoding="utf-8")
            s2.write_text("go", encoding="utf-8")
            r1 = _finish(p1)
            r2 = _finish(p2)
            self.assertTrue(r1["granted"], r1)
            self.assertTrue(r2["granted"], r2)

    def test_crash_releases_lock_stale_occupancy_does_not_block(self):
        """Existing semantics: kernel drops the file lock on process death.

        Occupancy JSON may remain; a new claimer still gets the lock and
        overwrites the occupancy record. No pid-liveness steal.
        """
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "ws"
            wd.mkdir()
            stall = Path(td) / "stall"
            dead = _spawn(["claim_and_die", str(wd), str(stall)])
            _wait_text(stall, "ready")
            out, err = dead.communicate(timeout=10)
            self.assertEqual(dead.returncode, 1, f"stdout={out!r} stderr={err!r}")
            occ = occupancy_path(wd)
            self.assertTrue(occ.is_file(), "stale occupancy JSON is left after crash")
            survivor = _spawn(["claim_try", str(wd), "survivor"])
            rs = _finish(survivor)
            self.assertTrue(rs["granted"], rs)


class TestPersistLockCrossProcess(unittest.TestCase):
    def test_second_process_blocks_until_first_releases(self):
        with tempfile.TemporaryDirectory() as td:
            stall = Path(td) / "stall"
            entered = Path(td) / "entered"
            a = _spawn(["persist_hold", td, str(stall)])
            _wait_text(stall, "ready")
            b = _spawn(["persist_enter", td, str(entered)])
            time.sleep(0.4)
            self.assertIsNone(b.poll(), "B must block while A holds persist_lock")
            self.assertFalse(entered.is_file())
            stall.write_text("go", encoding="utf-8")
            ra = _finish(a)
            rb = _finish(b)
            self.assertTrue(ra["ok"], ra)
            self.assertTrue(rb["ok"], rb)
            self.assertEqual(entered.read_text(encoding="utf-8").strip(), "entered")

    def test_nested_persist_lock_still_reuses_one_hold(self):
        with tempfile.TemporaryDirectory() as td:
            with persist_lock(td):
                with persist_lock(td):
                    (Path(td) / "x").write_text("ok", encoding="utf-8")
            self.assertEqual((Path(td) / "x").read_text(encoding="utf-8"), "ok")


class TestInprocessEntry(unittest.TestCase):
    def test_inprocess_backend_imports_and_start_run(self):
        from execution_backend import InProcessExecutionBackend, get_execution_backend

        be = get_execution_backend("inprocess")
        self.assertIsInstance(be, InProcessExecutionBackend)
        with tempfile.TemporaryDirectory() as td:
            out = be.start_run(
                title="hello",
                directory=td,
                artifacts=["hello-from-worker.txt"],
            )
            self.assertTrue(out["ok"], out)
            self.assertTrue((Path(td) / "hello-from-worker.txt").is_file())


class TestWindowsBackendUnit(unittest.TestCase):
    """Mock LockFileEx on this host. Not a real Windows lock-semantics acceptance."""

    def tearDown(self):
        reset_platform_services()
        from platform_services.windows import reset_kernel32_for_tests

        reset_kernel32_for_tests()

    def test_windows_module_imports_on_this_host(self):
        import platform_services.windows as win

        self.assertEqual(win.KIND, "windows")
        self.assertEqual(win.WindowsFileLock.name, "windows.lockfileex")

    def test_windows_acquire_without_kernel32_raises_not_succeeds(self):
        if os.name == "nt":
            self.skipTest("kernel32 is expected to be available on Windows")
        from platform_services.file_lock import FileLockUnsupported
        from platform_services.windows import WindowsFileLock, reset_kernel32_for_tests

        reset_kernel32_for_tests()
        lock = WindowsFileLock()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises((FileLockUnsupported, OSError)):
                lock.acquire(Path(td) / "lock", blocking=False)

    def test_lockfileex_exclusive_and_would_block(self):
        from platform_services.windows import (
            ERROR_LOCK_VIOLATION,
            FILE_SHARE_READ,
            FILE_SHARE_WRITE,
            LOCKFILE_EXCLUSIVE_LOCK,
            LOCKFILE_FAIL_IMMEDIATELY,
            WindowsFileLock,
        )

        class FakeK32:
            def __init__(self) -> None:
                self.handle_path: dict[int, str] = {}
                self.locked: set[str] = set()
                self.lock_calls: list[tuple[int, int]] = []
                self.create_share: list[int] = []
                self.next = 100
                self.last_error = 0
                self.unlocked: list[int] = []
                self.closed: list[int] = []

            def CreateFileW(self, path, access, share, sec, disp, attrs, tmpl):
                self.create_share.append(int(share))
                self.next += 1
                h = self.next
                self.handle_path[h] = str(path)
                return h

            def LockFileEx(self, handle, flags, reserved, low, high, ov):
                self.lock_calls.append((int(handle), int(flags)))
                path = self.handle_path[int(handle)]
                if path in self.locked:
                    self.last_error = ERROR_LOCK_VIOLATION
                    return 0
                self.locked.add(path)
                return 1

            def UnlockFileEx(self, handle, reserved, low, high, ov):
                path = self.handle_path.get(int(handle))
                if path:
                    self.locked.discard(path)
                self.unlocked.append(int(handle))
                return 1

            def CloseHandle(self, handle):
                self.closed.append(int(handle))
                return 1

        fake = FakeK32()
        lock = WindowsFileLock()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lock"
            with patch("platform_services.windows._kernel32", return_value=fake):
                with patch("platform_services.windows._last_error", side_effect=lambda: fake.last_error):
                    held = lock.acquire(path, blocking=False)
                    self.assertTrue(fake.lock_calls)
                    flags = fake.lock_calls[0][1]
                    self.assertTrue(flags & LOCKFILE_EXCLUSIVE_LOCK)
                    self.assertTrue(flags & LOCKFILE_FAIL_IMMEDIATELY)
                    self.assertTrue(fake.create_share)
                    self.assertEqual(
                        fake.create_share[0],
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                    )
                    with self.assertRaises(BlockingIOError):
                        lock.acquire(path, blocking=False)
                    held.unlock_and_close()
                    self.assertTrue(fake.unlocked)
                    self.assertTrue(fake.closed)

    def test_workdir_claim_uses_injected_backend_nonblocking(self):
        calls: list[tuple[str, bool]] = []

        class Held:
            def unlock_and_close(self) -> None:
                return None

            def close_without_unlock(self) -> None:
                return None

        class Spy:
            name = "spy.lock"

            def acquire(self, path, *, blocking: bool = True):
                calls.append((str(path), blocking))
                if not blocking and any(c[0] == str(path) for c in calls[:-1]):
                    raise BlockingIOError(11, "spy held")
                return Held()

        set_platform_services_for_tests(
            PlatformServices(kind="spy", file_lock=Spy(), process=StdlibProcess())
        )
        try:
            reg = WorkdirClaimRegistry()
            with tempfile.TemporaryDirectory() as td:
                a = reg.claim(td, holder_id="a", job_name="a")
                self.assertTrue(a.granted, a.to_dict())
                self.assertTrue(calls)
                self.assertFalse(calls[0][1], "workdir claim must use non-blocking lock")
                b = reg.claim(td, holder_id="b", job_name="b", mode="block")
                self.assertFalse(b.granted)
                self.assertEqual(b.status, STATUS_BLOCKED)
                self.assertTrue(reg.release(td, "a"))
        finally:
            reset_platform_services()


if __name__ == "__main__":
    unittest.main()
