#!/usr/bin/env python3
"""Unreadable artifacts must fail closed. Red until snapshot and pass refuse a read error."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from execution_backend.agy_review import _PREVIEW_BYTES_CAP, snapshot_artifacts
from execution_backend.agy_review_controller import AgyReviewController
from execution_backend.agy_review_store import AgyReviewStore

_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_BODY = b"synthetic-artifact-body\n"
_NAME = "report.md"
_RUN = "agy_0123456789ab"


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


@contextmanager
def _deny_artifact_io(path: Path):
    """Path.read_bytes and Path.open raise PermissionError for this artifact only."""
    wanted = _path_key(path)
    real_read = Path.read_bytes
    real_open = Path.open

    def _hit(self: Path) -> bool:
        try:
            return _path_key(self) == wanted
        except (OSError, TypeError, ValueError):
            return False

    def read_bytes(self: Path) -> bytes:
        if _hit(self):
            raise PermissionError("synthetic unreadable artifact")
        return real_read(self)

    def open_path(self: Path, mode="r", *args, **kwargs):
        if _hit(self):
            raise PermissionError("synthetic unreadable artifact")
        return real_open(self, mode, *args, **kwargs)

    with (
        mock.patch.object(Path, "read_bytes", read_bytes),
        mock.patch.object(Path, "open", open_path),
    ):
        yield


class _Backend:
    def __init__(self) -> None:
        self._runs: dict = {}
        self._lead_review = True

    def _persist_pool_after_collect(self, tmp, rec) -> None:
        return None


def _controller(directory: Path, name: str, *, review_state: str):
    review = {
        "state": review_state,
        "round": 1,
        "redos": 0,
        "max_redos": 1,
        "base_prompt": "synthetic prompt",
        "deadline": None,
        "acceptance_text": "prose acceptance",
        "history": [],
        "usage_rounds": [],
        "resolutions": {},
        "request_id": f"agyrev:{_RUN}:r1",
        "pool_done_round": None,
        "collected": False,
        "artifact_hash": None,
        "payload": None,
        "unavailable_reason": None,
        "error_source": None,
        "last_result": None,
        "agy_err_class": None,
        "skip_reason": None,
        "context_hash": None,
    }
    rec = {
        "run_id": _RUN,
        "directory": str(directory),
        "artifacts": [name],
        "review": review,
        "state": "succeeded",
        "finish": "stop",
        "assistant_error": "",
        "timed_out": False,
        "cancelled": False,
        "harvested": True,
        "proc": None,
        "response": "worker-ok",
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "usage": None,
        "conversation_id": "",
        "spawn_environ": None,
        "model": "",
        "skip_permissions": False,
        "timeout_sec": 60,
        "started_at": 0,
        "title": "synthetic",
        "native_handle": f"agy_native_{_RUN}",
    }
    controller = AgyReviewController(_Backend(), store=AgyReviewStore(None))
    return controller, rec, review


class AgyReviewUnreadableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-unreadable-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / _NAME

    def _reject_empty_standin(self, snap: dict, name: str, *, where: str) -> None:
        arts = snap.get("artifacts") if isinstance(snap, dict) else None
        entry = arts.get(name) if isinstance(arts, dict) else None
        if not isinstance(entry, dict):
            return
        hashed_empty = entry.get("bytes") == 0 or entry.get("sha256") == _EMPTY_SHA
        self.assertFalse(
            hashed_empty,
            f"{where} reported {name} as an empty artifact: {entry!r}",
        )

    def test_small_read_error_is_unreadable_not_empty(self) -> None:
        """asserts a small-file PermissionError is listed unreadable and not hashed as empty"""
        self.path.write_bytes(_BODY)
        self.assertGreater(self.path.stat().st_size, 0)
        self.assertLessEqual(self.path.stat().st_size, _PREVIEW_BYTES_CAP)
        with _deny_artifact_io(self.path):
            try:
                snap = snapshot_artifacts(self.root, [_NAME])
            except OSError as exc:
                self.fail(f"snapshot_artifacts raised {type(exc).__name__} for a small artifact")
        self._reject_empty_standin(snap, _NAME, where="snapshot_artifacts")
        self.assertNotIn(_NAME, snap.get("artifacts") or {})
        unread = snap.get("unreadable")
        self.assertIsInstance(unread, list)
        self.assertIn(_NAME, unread)
        bogus = hashlib.sha256(f"{_NAME}:{_EMPTY_SHA}".encode("utf-8")).hexdigest()
        self.assertNotEqual(snap.get("artifact_hash"), bogus)

    def test_large_read_error_does_not_escape(self) -> None:
        """asserts an over-cap PermissionError stays inside snapshot_artifacts as unreadable"""
        size = _PREVIEW_BYTES_CAP + 1
        with self.path.open("wb") as fh:
            fh.seek(size - 1)
            fh.write(b"x")
        self.assertGreater(self.path.stat().st_size, _PREVIEW_BYTES_CAP)
        raised: BaseException | None = None
        with _deny_artifact_io(self.path):
            try:
                snap = snapshot_artifacts(self.root, [_NAME])
            except OSError as exc:
                raised = exc
                snap = {}
        self.assertIsNone(
            raised,
            f"snapshot_artifacts let {type(raised).__name__ if raised else ''} escape the large-file branch",
        )
        self._reject_empty_standin(snap, _NAME, where="large-file snapshot")
        self.assertNotIn(_NAME, snap.get("artifacts") or {})
        unread = snap.get("unreadable")
        self.assertIsInstance(unread, list)
        self.assertIn(_NAME, unread)

    def test_apply_pass_locked_stays_closed(self) -> None:
        """asserts _apply_pass_locked stays review_unavailable while the same read failure persists"""
        self.path.write_bytes(_BODY)
        controller, rec, review = _controller(self.root, _NAME, review_state="awaiting_review")
        with _deny_artifact_io(self.path):
            reviewed = snapshot_artifacts(self.root, [_NAME])
            review["artifact_hash"] = reviewed.get("artifact_hash")
            controller._apply_pass_locked(rec, review)
            self.assertNotEqual(
                review.get("state"),
                "accepted",
                f"pass accepted an unreadable artifact: {review.get('unavailable_reason')!r}",
            )
            self.assertEqual(review.get("state"), "review_unavailable")
            reason = str(review.get("unavailable_reason") or "")
            self.assertIn("unreadable", reason)
            self.assertIn("artifact", reason)
            controller._apply_pass_locked(rec, review)
            self.assertNotEqual(review.get("state"), "accepted")
            self.assertEqual(review.get("state"), "review_unavailable")
            again = str(review.get("unavailable_reason") or "")
            self.assertIn("unreadable", again)
            self.assertIn("artifact", again)

    def test_readable_artifact_still_accepted(self) -> None:
        """asserts a readable artifact with a matching hash is still accepted"""
        self.path.write_bytes(_BODY)
        snap = snapshot_artifacts(self.root, [_NAME])
        self.assertNotIn(_NAME, snap.get("unreadable") or [])
        entry = snap["artifacts"][_NAME]
        self.assertEqual(entry["bytes"], len(_BODY))
        self.assertEqual(entry["sha256"], hashlib.sha256(_BODY).hexdigest())
        self.assertNotEqual(entry["sha256"], _EMPTY_SHA)
        controller, rec, review = _controller(self.root, _NAME, review_state="awaiting_review")
        review["artifact_hash"] = snap["artifact_hash"]
        controller._apply_pass_locked(rec, review)
        self.assertEqual(review.get("state"), "accepted")

    def test_lead_review_payload_marks_unreadable(self) -> None:
        """asserts the lead-review payload and snapshot name the unreadable artifact"""
        self.path.write_bytes(_BODY)
        controller, rec, review = _controller(self.root, _NAME, review_state="running")
        with _deny_artifact_io(self.path):
            snap = snapshot_artifacts(self.root, [_NAME])
            controller._review_on_exit(rec)
        payload = review.get("payload") if isinstance(review.get("payload"), dict) else {}
        self._reject_empty_standin(payload, _NAME, where="lead payload")
        self.assertNotIn(_NAME, payload.get("artifacts") or {})
        marked = list(payload.get("unreadable") or [])
        error = payload.get("artifact_error")
        self.assertIn(_NAME, marked, f"lead payload did not list unreadable artifacts: {payload!r}")
        self.assertIsInstance(error, str)
        self.assertIn("unreadable", error)
        self.assertIn(_NAME, error)
        self.assertIn(_NAME, list(snap.get("unreadable") or []))
        self._reject_empty_standin(snap, _NAME, where="lead snapshot")
        self.assertNotEqual(review.get("state"), "accepted")


if __name__ == "__main__":
    unittest.main()
