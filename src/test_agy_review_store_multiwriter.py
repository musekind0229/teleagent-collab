#!/usr/bin/env python3
"""Two AgyReviewStore instances on one path must not lose each other's writes.

A path store used to read the file once in ``__init__`` and guard it with a
``threading.Lock`` only. ``a.put(a); b.put(b)`` then left just ``b`` on disk.
Ordering uses ``threading.Event`` and two instances. Timeouts only fail a stuck
lock; they do not order the operations.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import execution_backend.agy_review_store as store_mod
from execution_backend.agy_review_store import AgyReviewStore

# Bound so a PID-in-file lock cannot hang the suite. Not used to order steps.
_STUCK_SEC = 5
_RUN_A = "agy_a5i5000000a1"
_RUN_B = "agy_a5i5000000b1"


def _disk_runs(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
        raise AssertionError(f"review store file is not a versioned runs document: {data!r}")
    return data["runs"]


def _record(marker: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"marker": marker, "state": "awaiting_review"}
    body.update(extra)
    return body


def _os_lock_available(lock_path: Path) -> bool:
    """True when this process can take the exclusive OS lock and then drop it.

    POSIX locks the whole file with ``flock``. Windows locks 1 byte at offset 0
    with ``msvcrt.locking``, the same region the store holds.
    """
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(os.fspath(lock_path), flags, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            try:
                return True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        try:
            return True
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


class AgyReviewStoreMultiWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-review-multi-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "agy-reviews.json"
        # Sidecar is "<path>.lock", not Path.with_suffix (that would drop .json).
        self.lock_path = Path(str(self.path) + ".lock")

    def _start(self, target: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def _join(self, threads: list[threading.Thread]) -> None:
        for thread in threads:
            thread.join(_STUCK_SEC)
            if thread.is_alive():
                self.fail("review store operation deadlocked")

    def _race(self, first: Callable[[], None], second: Callable[[], None]) -> None:
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def run(fn: Callable[[], None]) -> None:
            try:
                barrier.wait(_STUCK_SEC)
                fn()
            except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
                errors.append(exc)
                try:
                    barrier.abort()
                except Exception:
                    pass

        threads = [self._start(lambda: run(first)), self._start(lambda: run(second))]
        self._join(threads)
        if errors:
            raise errors[0]

    def test_two_instances_interleaved_put_update_forget_keep_union(self) -> None:
        """asserts a.put(a); b.put(b) keeps {a,b} on disk and in a fresh store; a.update sees b; b.forget(a) drops a for both after their next op"""
        a = AgyReviewStore(self.path)
        b = AgyReviewStore(self.path)
        errors: list[BaseException] = []
        error_lock = threading.Lock()
        a_put = threading.Event()
        b_put = threading.Event()
        a_upd = threading.Event()
        b_forget = threading.Event()
        a_next = threading.Event()
        events = (a_put, b_put, a_upd, b_forget, a_next)

        def fail_set(exc: BaseException) -> None:
            with error_lock:
                errors.append(exc)
            for event in events:
                event.set()

        def wait(event: threading.Event, what: str) -> None:
            if not event.wait(_STUCK_SEC):
                raise TimeoutError(what)

        def thread_a() -> None:
            try:
                a.put(_RUN_A, _record("from-a"))
                a_put.set()
                wait(b_put, "b.put")
                # a has not written since its own put, so b's record is not in memory yet.
                if set(a.load()) != {_RUN_A}:
                    raise AssertionError(f"a saw b before its next op: {sorted(a.load())}")
                updated = a.update(_RUN_B, x=1)
                if updated.get("marker") != "from-b" or updated.get("x") != 1:
                    raise AssertionError(f"a.update did not see b: {updated!r}")
                runs = _disk_runs(self.path)
                if set(runs) != {_RUN_A, _RUN_B}:
                    raise AssertionError(f"update dropped a key: {sorted(runs)}")
                if runs[_RUN_B].get("marker") != "from-b" or runs[_RUN_B].get("x") != 1:
                    raise AssertionError(f"disk record b lost fields: {runs[_RUN_B]!r}")
                if set(a.load()) != {_RUN_A, _RUN_B} or a.get(_RUN_B) != runs[_RUN_B]:
                    raise AssertionError("a memory did not refresh from the update write")
                a_upd.set()
                wait(b_forget, "b.forget")
                if _RUN_A not in a.load():
                    raise AssertionError("a dropped a before its next op")
                if _RUN_A in _disk_runs(self.path):
                    raise AssertionError("forget left a on disk")
                fresh = AgyReviewStore(self.path)
                if _RUN_A in fresh.load():
                    raise AssertionError("fresh instance still has a after forget")
                a.update(_RUN_B, y=2)
                if _RUN_A in a.load() or _RUN_A in _disk_runs(self.path):
                    raise AssertionError("a's next op resurrected a")
                got = a.get(_RUN_B)
                if got is None or got.get("y") != 2 or got.get("x") != 1 or got.get("marker") != "from-b":
                    raise AssertionError(f"a's next op lost b's fields: {got!r}")
                a_next.set()
            except BaseException as exc:  # noqa: BLE001 — captured for the main thread
                fail_set(exc)

        def thread_b() -> None:
            try:
                wait(a_put, "a.put")
                b.put(_RUN_B, _record("from-b"))
                runs = _disk_runs(self.path)
                if set(runs) != {_RUN_A, _RUN_B}:
                    raise AssertionError(f"disk after a.put+b.put is {sorted(runs)}")
                if set(b.load()) != {_RUN_A, _RUN_B}:
                    raise AssertionError(f"b memory after put is {sorted(b.load())}")
                fresh = AgyReviewStore(self.path)
                if set(fresh.load()) != {_RUN_A, _RUN_B}:
                    raise AssertionError(f"fresh instance after puts is {sorted(fresh.load())}")
                b_put.set()
                wait(a_upd, "a.update")
                stale = b.get(_RUN_B)
                if stale is not None and stale.get("x") == 1:
                    raise AssertionError("b saw a's update before b's next op")
                b.forget(_RUN_A)
                if _RUN_A in b.load() or _RUN_A in _disk_runs(self.path):
                    raise AssertionError("b.forget did not remove a from b or disk")
                got = b.get(_RUN_B)
                if got is None or got.get("x") != 1 or got.get("marker") != "from-b":
                    raise AssertionError(f"b.forget rewrote b without a's update: {got!r}")
                b_forget.set()
                wait(a_next, "a next op")
                b.update(_RUN_B, z=3)
                if _RUN_A in b.load():
                    raise AssertionError("b's next op resurrected a")
                got = b.get(_RUN_B)
                if (
                    got is None
                    or got.get("x") != 1
                    or got.get("y") != 2
                    or got.get("z") != 3
                    or got.get("marker") != "from-b"
                ):
                    raise AssertionError(f"b's next op lost fields: {got!r}")
            except BaseException as exc:  # noqa: BLE001 — captured for the main thread
                fail_set(exc)

        threads = [self._start(thread_a), self._start(thread_b)]
        try:
            self._join(threads)
        finally:
            for event in events:
                event.set()
        if errors:
            raise errors[0]

        disk = _disk_runs(self.path)
        fresh = AgyReviewStore(self.path)
        self.assertEqual(set(disk), {_RUN_B})
        self.assertEqual(disk[_RUN_B]["marker"], "from-b")
        self.assertEqual(disk[_RUN_B]["x"], 1)
        self.assertEqual(disk[_RUN_B]["y"], 2)
        self.assertEqual(disk[_RUN_B]["z"], 3)
        self.assertEqual(fresh.load(), disk)
        self.assertNotIn(_RUN_A, a.load())
        self.assertNotIn(_RUN_A, b.load())
        self.assertEqual(b.get(_RUN_B), disk[_RUN_B])
        self.assertEqual(fresh.get(_RUN_B), disk[_RUN_B])

    def test_interleaved_threads_keep_every_key(self) -> None:
        """asserts concurrent puts, updates, and forgets from two instances keep the on-disk union"""
        a = AgyReviewStore(self.path)
        b = AgyReviewStore(self.path)
        a_keys = [f"agy_a5i5a{i:02d}" for i in range(8)]
        b_keys = [f"agy_a5i5b{i:02d}" for i in range(8)]

        def put_all(store: AgyReviewStore, keys: list[str], marker: str) -> None:
            for key in keys:
                store.put(key, _record(marker))

        self._race(
            lambda: put_all(a, a_keys, "a"),
            lambda: put_all(b, b_keys, "b"),
        )
        disk = _disk_runs(self.path)
        fresh = AgyReviewStore(self.path)
        self.assertEqual(set(disk), set(a_keys + b_keys))
        self.assertEqual(set(fresh.load()), set(a_keys + b_keys))

        def mark(store: AgyReviewStore, keys: list[str], seen: str) -> None:
            for key in keys:
                store.update(key, seen=seen)

        self._race(
            lambda: mark(a, b_keys, "by-a"),
            lambda: mark(b, a_keys, "by-b"),
        )
        disk = _disk_runs(self.path)
        for key in a_keys:
            self.assertEqual(disk[key]["marker"], "a")
            self.assertEqual(disk[key]["seen"], "by-b")
        for key in b_keys:
            self.assertEqual(disk[key]["marker"], "b")
            self.assertEqual(disk[key]["seen"], "by-a")
        self.assertEqual(AgyReviewStore(self.path).load(), disk)

        def drop(store: AgyReviewStore, keys: list[str]) -> None:
            for key in keys:
                store.forget(key)

        self._race(
            lambda: drop(a, a_keys),
            lambda: drop(b, b_keys),
        )
        disk = _disk_runs(self.path)
        self.assertEqual(disk, {})
        self.assertEqual(AgyReviewStore(self.path).load(), {})

    def test_write_stays_atomic_temp_replace_and_mode_0600(self) -> None:
        """asserts a path write still replaces a temp file and keeps mode 0600 on POSIX"""
        store = AgyReviewStore(self.path)
        mkstemp_calls: list[str] = []
        replace_calls: list[tuple[str, str]] = []
        real_mkstemp = tempfile.mkstemp
        real_replace = os.replace

        def spy_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            fd, name = real_mkstemp(*args, **kwargs)
            mkstemp_calls.append(name)
            return fd, name

        def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            replace_calls.append((os.fspath(src), os.fspath(dst)))
            if os.name == "posix":
                mode = os.stat(src).st_mode & 0o777
                self.assertEqual(mode, 0o600)
            real_replace(src, dst)

        run_id = "agy_a5i5000000c1"
        with (
            mock.patch.object(store_mod.tempfile, "mkstemp", spy_mkstemp),
            mock.patch.object(store_mod.os, "replace", spy_replace),
        ):
            store.put(run_id, _record("atomic"))

        self.assertEqual(len(mkstemp_calls), 1)
        self.assertEqual(len(replace_calls), 1)
        src, dst = replace_calls[0]
        self.assertEqual(src, mkstemp_calls[0])
        self.assertEqual(Path(dst), self.path)
        self.assertTrue(Path(src).name.startswith(".agy-reviews-"))
        self.assertFalse(Path(src).exists())
        self.assertEqual(list(self.root.glob(".agy-reviews-*")), [])
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("version"), 1)
        self.assertEqual(data["runs"][run_id]["marker"], "atomic")
        self.assertEqual(store.get(run_id), data["runs"][run_id])

    def test_stale_lock_file_does_not_deadlock(self) -> None:
        """asserts a leftover lock file with a live PID does not block; the OS lock is held during replace and released with the fd"""
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")
        self.assertTrue(
            _os_lock_available(self.lock_path),
            "precondition: a leftover lock file is not held by a dead process",
        )
        store = AgyReviewStore(self.path)
        saw: dict[str, bool] = {}
        errors: list[BaseException] = []
        real_replace = os.replace

        def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            if Path(dst) == self.path:
                # Same thread, other fd. flock/msvcrt exclude it; a PID file does not.
                saw["held"] = not _os_lock_available(self.lock_path)
            real_replace(src, dst)

        def worker() -> None:
            try:
                with mock.patch.object(store_mod.os, "replace", spy_replace):
                    store.put(_RUN_A, _record("after-stale-lock"))
            except BaseException as exc:  # noqa: BLE001 — captured for the main thread
                errors.append(exc)

        thread = self._start(worker)
        self._join([thread])
        if errors:
            raise errors[0]
        self.assertTrue(saw.get("held"), "OS lock was not held on <path>.lock during replace")
        self.assertTrue(
            _os_lock_available(self.lock_path),
            "OS lock was still held after put returned",
        )
        self.assertEqual(_disk_runs(self.path)[_RUN_A]["marker"], "after-stale-lock")

        other = AgyReviewStore(self.path)
        other.put(_RUN_B, _record("second-writer"))
        disk = _disk_runs(self.path)
        fresh = AgyReviewStore(self.path)
        self.assertEqual(set(disk), {_RUN_A, _RUN_B})
        self.assertEqual(set(fresh.load()), {_RUN_A, _RUN_B})
        self.assertTrue(
            _os_lock_available(self.lock_path),
            "OS lock was still held after the second writer",
        )

    def test_memory_store_path_none_unchanged(self) -> None:
        """asserts path None keeps put, update, forget, and prune in memory and writes no file"""
        before = set(self.root.iterdir())
        mem = AgyReviewStore(None)
        other = AgyReviewStore(None)
        self.assertIsNone(mem.path)
        run_id = "agy_a5i5000000m1"
        kept = "agy_a5i5000000m2"
        mem.put(run_id, _record("mem"))
        updated = mem.update(run_id, x=1)
        self.assertEqual(updated["marker"], "mem")
        self.assertEqual(updated["x"], 1)
        self.assertEqual(mem.get(run_id), mem.load()[run_id])
        self.assertIsNone(other.get(run_id))
        self.assertEqual(other.load(), {})
        mem.forget(run_id)
        self.assertIsNone(mem.get(run_id))
        mem.forget(run_id)
        with self.assertRaises(KeyError):
            mem.update(run_id, x=2)
        mem.put(run_id, {**_record("old"), "collected": True, "updated_at": 1})
        mem.put(kept, _record("kept"))
        dropped = mem.prune(collected_older_than_sec=10, now=100)
        self.assertEqual(dropped, 1)
        self.assertIsNone(mem.get(run_id))
        self.assertEqual(mem.get(kept)["marker"], "kept")
        self.assertEqual(mem.prune(collected_older_than_sec=10, now=100), 0)
        self.assertEqual(set(self.root.iterdir()), before)

        ids = ["agy_a5i5000000t1", "agy_a5i5000000t2"]

        def put_one(key: str) -> None:
            mem.put(key, _record(key))

        self._race(lambda: put_one(ids[0]), lambda: put_one(ids[1]))
        self.assertEqual(mem.get(ids[0])["marker"], ids[0])
        self.assertEqual(mem.get(ids[1])["marker"], ids[1])
        self.assertEqual(set(self.root.iterdir()), before)



class AgyReviewStoreDiskLossTests(unittest.TestCase):
    """Hand-written guard (a5 I5 review): a missing or corrupt file must not wipe memory on the next write."""

    def test_corrupt_or_missing_file_keeps_memory_records(self) -> None:
        """asserts a corrupt store file (renamed aside) or a deleted one does not make the next write drop known records."""
        for case in ("corrupt", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "store.json"
                store = AgyReviewStore(path)
                store.put("keep", {"state": "awaiting_review"})
                if case == "corrupt":
                    path.write_text("{not json", encoding="utf-8")
                else:
                    path.unlink()
                store.put("new", {"state": "awaiting_review"})
                self.assertEqual(set(AgyReviewStore(path).load()), {"keep", "new"})

if __name__ == "__main__":
    unittest.main()
