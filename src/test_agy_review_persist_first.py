#!/usr/bin/env python3
"""A failed review-store write must not publish the verdict.

The first durable write is the commit. If it raises, the controller review
(shared dict), the store memory, and the disk file all stay awaiting_review,
and a retry of the same verdict raises again. Rework is not spawned.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from execution_backend.agy_review_controller import AgyReviewController
from execution_backend.agy_review_store import AgyReviewStore

_ART_HASH = hashlib.sha256(b"").hexdigest()


def _request_id(run_id: str) -> str:
    return f"agyrev:{run_id}:r1"


def _disk_runs(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
        raise AssertionError("review store file is not a versioned runs document")
    return data["runs"]


@contextmanager
def _disk_full(store: AgyReviewStore):
    def _boom(*_args, **_kwargs):
        raise OSError("synthetic disk full")

    with mock.patch.object(store, "_write_locked", _boom):
        yield


class _Proc:
    pid = 424242

    def poll(self):
        return None


class _Session:
    def baseline(self, _env):
        return {"synthetic": True}


class _Backend:
    """In-process spawn fake. Counts _spawn_locked calls and never starts agy."""

    def __init__(self) -> None:
        self._runs: dict = {}
        self._lead_review = True
        self._account_pool_path = None
        self._registry = None
        self.bin_path = "synthetic-agy"
        self._session = _Session()
        self.spawns = 0

    def _base_env(self):
        return {}

    def _bind_pool_environ_for_dispatch(self):
        return {"SYNTHETIC_POOL": "1"}

    def _spawn_locked(self, rec, _cmd, _spawn_env, _path_errors):
        self.spawns += 1
        rec["proc"] = _Proc()
        return None

    def _start_pipe_drainers(self, _rec, _proc):
        return None

    def _release_pool_after_failed_spawn(self, _spawn_env):
        return None


def _review(run_id: str, deadline: float) -> dict:
    return {
        "state": "awaiting_review",
        "round": 1,
        "redos": 0,
        "max_redos": 1,
        "base_prompt": "synthetic prompt",
        "deadline": deadline,
        "acceptance_text": "synthetic acceptance",
        "history": [],
        "usage_rounds": [],
        "resolutions": {},
        "request_id": _request_id(run_id),
        "pool_done_round": None,
        "collected": False,
        "artifact_hash": _ART_HASH,
        "payload": {"synthetic": True},
        "unavailable_reason": None,
        "error_source": None,
        "last_result": None,
        "agy_err_class": None,
        "skip_reason": None,
        "context_hash": None,
        "waiting_since": None,
        "pending_rework": None,
    }


def _rec(run_id: str, review: dict, directory: Path, started: float) -> dict:
    return {
        "run_id": run_id,
        "native_handle": f"agy_native_{run_id}",
        "directory": str(directory),
        "artifacts": [],
        "review": review,
        "state": "succeeded",
        "finish": "stop",
        "activity": "idle",
        "assistant_error": "",
        "timed_out": False,
        "cancelled": False,
        "harvested": True,
        "proc": None,
        "response": "synthetic-worker-ok",
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "usage": None,
        "conversation_id": "synthetic-conv",
        "spawn_environ": None,
        "model": "synthetic-model",
        "skip_permissions": False,
        "timeout_sec": 3600,
        "started_at": started,
        "title": "synthetic",
        "path_errors": [],
        "contract_sha256": "",
        "contract_fields": [],
    }


def _stored(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "state": "awaiting_review",
        "round": 1,
        "redos": 0,
        "history": [],
        "resolutions": {},
        "request_id": _request_id(run_id),
        "collected": False,
        "updated_at": 1700000000,
    }


class AgyReviewResolvePersistFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-persist-first-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _arm(self, run_id: str):
        started = time.time()
        review = _review(run_id, started + 3600.0)
        rec = _rec(run_id, review, self.root, started)
        backend = _Backend()
        backend._runs[run_id] = rec
        store = AgyReviewStore(self.root / "agy-reviews.json")
        store.put(run_id, _stored(run_id))
        controller = AgyReviewController(backend, store=store)
        heard: list = []
        controller.add_listener(lambda *args: heard.append(args))
        return controller, store, backend, rec, review, heard

    def _resolve(self, controller, request_id: str, verdict: str):
        return controller.resolve_decision(
            request_id,
            verdict=verdict,
            reason=f"synthetic {verdict}",
            answers=[{"by": "synthetic-lead"}],
        )

    def _assert_raises_oserror(self, controller, request_id: str, verdict: str) -> None:
        try:
            body = self._resolve(controller, request_id, verdict)
        except OSError:
            return
        self.fail(f"resolve({verdict}) returned {body!r} instead of raising OSError")

    def _assert_uncommitted(self, store, rec, review, history, resolutions, heard, backend) -> None:
        disk = _disk_runs(store.path)
        run_id = rec["run_id"]
        self.assertIs(rec["review"], review)
        self.assertIs(review["history"], history)
        self.assertIs(review["resolutions"], resolutions)
        self.assertEqual(review["state"], "awaiting_review")
        self.assertEqual(review["round"], 1)
        self.assertEqual(history, [])
        self.assertEqual(resolutions, {})
        self.assertEqual(disk[run_id]["state"], "awaiting_review")
        self.assertEqual(store.get(run_id), disk[run_id])
        self.assertEqual(store.load(), disk)
        self.assertEqual(heard, [])
        self.assertEqual(backend.spawns, 0)

    def _exercise(self, verdict: str, *, committed_state: str, expect_spawns: int) -> None:
        run_id = {
            "pass": "agy_a5a11a000001",
            "fail": "agy_a5a11a000002",
            "unavailable": "agy_a5a11a000003",
        }[verdict]
        request_id = _request_id(run_id)
        controller, store, backend, rec, review, heard = self._arm(run_id)
        history = review["history"]
        resolutions = review["resolutions"]
        before_review = copy.deepcopy(review)
        before_fields = copy.deepcopy({key: value for key, value in rec.items() if key != "review"})
        disk_before = store.path.read_bytes()
        self.assertEqual(_disk_runs(store.path)[run_id]["state"], "awaiting_review")

        with _disk_full(store):
            self._assert_raises_oserror(controller, request_id, verdict)
            self._assert_uncommitted(store, rec, review, history, resolutions, heard, backend)
            self.assertEqual(review, before_review)
            fields = {key: value for key, value in rec.items() if key != "review"}
            self.assertEqual(fields, before_fields)
            self.assertEqual(store.path.read_bytes(), disk_before)
            self._assert_raises_oserror(controller, request_id, verdict)
            self._assert_uncommitted(store, rec, review, history, resolutions, heard, backend)
            self.assertEqual(store.path.read_bytes(), disk_before)

        ok = self._resolve(controller, request_id, verdict)
        self.assertTrue(ok.get("ok"))
        self.assertNotIn("idempotent", ok)
        self.assertEqual(ok.get("controller_state"), committed_state)
        saved = _disk_runs(store.path)[run_id]
        self.assertEqual(saved["state"], committed_state)
        self.assertEqual(saved["resolutions"][request_id]["verdict"], verdict)
        if verdict == "fail":
            self.assertEqual(saved["round"], 2)
            self.assertEqual(saved["redos"], 1)
            self.assertIsNone(saved["request_id"])
            self.assertEqual(review["round"], 2)
            self.assertEqual(review["redos"], 1)
        self.assertEqual(store.get(run_id), saved)
        self.assertEqual(store.load()[run_id], saved)
        self.assertEqual(backend.spawns, expect_spawns)
        self.assertEqual(len(heard), 1)
        self.assertEqual(heard[0][0], request_id)
        self.assertEqual(heard[0][1], verdict)
        self.assertEqual(heard[0][2], committed_state)
        self.assertIs(rec["review"], review)
        self.assertIs(review["history"], history)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["verdict"], verdict)
        committed = store.path.read_bytes()

        again = self._resolve(controller, request_id, verdict)
        self.assertTrue(again.get("ok"))
        self.assertTrue(again.get("idempotent"))
        self.assertEqual(again.get("controller_state"), committed_state)
        self.assertEqual(store.path.read_bytes(), committed)
        self.assertEqual(backend.spawns, expect_spawns)
        self.assertEqual(len(heard), 1)
        self.assertEqual(_disk_runs(store.path)[run_id]["state"], committed_state)

    def test_pass_write_failure_does_not_publish_verdict(self) -> None:
        """asserts a failed pass write leaves awaiting_review, the retry raises, then one accept is idempotent"""
        self._exercise("pass", committed_state="accepted", expect_spawns=0)

    def test_fail_write_failure_does_not_spawn_or_publish(self) -> None:
        """asserts a failed fail write spawns nothing and is not idempotent; the later commit spawns once"""
        self._exercise("fail", committed_state="running", expect_spawns=1)

    def test_unavailable_write_failure_does_not_publish_verdict(self) -> None:
        """asserts a failed unavailable write leaves awaiting_review, the retry raises, then one commit sticks"""
        self._exercise("unavailable", committed_state="review_unavailable", expect_spawns=0)


class AgyReviewStorePersistFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-store-persist-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "agy-reviews.json"
        self.store = AgyReviewStore(self.path)

    def _assert_matches_disk(self) -> None:
        disk = _disk_runs(self.path)
        self.assertEqual(self.store.load(), disk)
        for run_id, record in disk.items():
            self.assertEqual(self.store.get(run_id), record)

    def test_store_update_failure_keeps_memory_on_disk(self) -> None:
        """asserts update that fails to write leaves load() and get() equal to the disk record"""
        run_id = "agy_a5a11a000011"
        self.store.put(run_id, _stored(run_id))
        self._assert_matches_disk()
        with _disk_full(self.store):
            with self.assertRaises(OSError):
                self.store.update(run_id, state="accepted", synthetic_marker="nope")
        self._assert_matches_disk()
        self.assertEqual(self.store.get(run_id)["state"], "awaiting_review")
        self.assertNotIn("synthetic_marker", self.store.get(run_id))

    def test_store_put_failure_keeps_memory_on_disk(self) -> None:
        """asserts put that fails to write leaves load() and get() equal to the disk document"""
        kept = "agy_a5a11a000012"
        extra = "agy_a5a11a000013"
        self.store.put(kept, _stored(kept))
        self._assert_matches_disk()
        with _disk_full(self.store):
            with self.assertRaises(OSError):
                self.store.put(kept, {**_stored(kept), "state": "accepted"})
            with self.assertRaises(OSError):
                self.store.put(extra, _stored(extra))
        self._assert_matches_disk()
        self.assertEqual(self.store.get(kept)["state"], "awaiting_review")
        self.assertIsNone(self.store.get(extra))
        self.assertNotIn(extra, _disk_runs(self.path))

    def test_store_forget_failure_keeps_memory_on_disk(self) -> None:
        """asserts forget that fails to write leaves load() and get() equal to the disk document"""
        run_id = "agy_a5a11a000014"
        other = "agy_a5a11a000015"
        self.store.put(run_id, _stored(run_id))
        self.store.put(other, _stored(other))
        self._assert_matches_disk()
        with _disk_full(self.store):
            with self.assertRaises(OSError):
                self.store.forget(run_id)
        self._assert_matches_disk()
        self.assertEqual(self.store.get(run_id)["state"], "awaiting_review")
        self.assertIn(run_id, _disk_runs(self.path))
        self.assertIn(other, self.store.load())

    def test_store_prune_failure_keeps_memory_on_disk(self) -> None:
        """asserts prune that fails to write leaves load() and get() equal to the disk document"""
        old = "agy_a5a11a000016"
        fresh = "agy_a5a11a000017"
        self.store.put(old, {**_stored(old), "state": "accepted", "collected": True, "updated_at": 1})
        self.store.put(fresh, _stored(fresh))
        self._assert_matches_disk()
        with _disk_full(self.store):
            with self.assertRaises(OSError):
                self.store.prune(collected_older_than_sec=10, now=1_700_000_000)
        self._assert_matches_disk()
        self.assertEqual(self.store.get(old)["collected"], True)
        self.assertIn(old, _disk_runs(self.path))
        self.assertIn(fresh, self.store.load())


if __name__ == "__main__":
    unittest.main()
