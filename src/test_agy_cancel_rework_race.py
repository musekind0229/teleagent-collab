"""Cancel vs rework-start: the controller must re-check under its lock.

Deterministic interleaving only (threading.Event / a lock gate). No sleep.
"""
from __future__ import annotations

import threading
import unittest
from typing import Any

from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend


class _LockGate:
    """Sets ``arrived`` then acquires ``underlying`` (an RLock the test holds)."""

    def __init__(self, underlying: threading.RLock, arrived: threading.Event) -> None:
        self._underlying = underlying
        self._arrived = arrived

    def __enter__(self) -> bool:
        self._arrived.set()
        return self._underlying.__enter__()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        return self._underlying.__exit__(exc_type, exc, tb)


class _LiveProc:
    """Popen-shaped fake: kill() signals, wait() reaps, poll() is the exit code."""

    def __init__(self, *, reap_on_wait: bool = True) -> None:
        self.pid = 424242
        self.returncode: int | None = None
        self.killed = False
        self.stdout = None
        self.stderr = None
        self.reap_on_wait = reap_on_wait

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int | None:
        del timeout
        if self.reap_on_wait and self.killed and self.returncode is None:
            self.returncode = -15
        return self.returncode


class CancelReworkRaceTests(unittest.TestCase):
    def _backend(self) -> AntigravityCliExecutionBackend:
        return AntigravityCliExecutionBackend(
            bin_path="/opt/synthetic/agy",
            environ={"PATH": "/opt/synthetic", "AGY_BIN": "/opt/synthetic/agy"},
        )

    def _parked(self, be: AntigravityCliExecutionBackend, run_id: str) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "run_id": run_id,
            "native_handle": f"agy_native_{run_id}",
            "state": "succeeded",
            "activity": "idle",
            "finish": "stop",
            "cancelled": False,
            "harvested": True,
            "proc": None,
            "stdout_chunks": [],
            "stderr_chunks": [],
            "_children_seen": [],
            "review": {
                "state": "awaiting_review",
                "round": 1,
                "request_id": f"agyrev:{run_id}:r1",
            },
        }
        be._runs[run_id] = rec
        be._runs[rec["native_handle"]] = rec
        be._review._store.put(run_id, {"run_id": run_id, "state": "awaiting_review"})
        return rec

    def test_cancel_blocks_on_lock_then_kills_rework_before_lease_release(self) -> None:
        """asserts a cancel blocked on the controller lock, after a rework proc is installed, kills that proc and releases the lease only after it has exited."""
        be = self._backend()
        run_id = "agy_cancel_rework_race"
        rec = self._parked(be, run_id)
        proc = _LiveProc()
        effects: list[str] = []

        def fake_kill(live: Any, rec_arg: Any = None) -> bool:
            del rec_arg
            live.kill()
            effects.append("kill")
            live.wait()
            effects.append("exited")
            return live.poll() is not None

        def fake_release(rec_arg: Any) -> None:
            del rec_arg
            effects.append("lease released")

        be._kill_proc = fake_kill  # type: ignore[method-assign]
        be._release_pool_lease_after_cancel = fake_release  # type: ignore[method-assign]

        ctrl = be._review
        underlying = ctrl._lock
        self.assertIsInstance(underlying, type(threading.RLock()))
        arrived = threading.Event()
        holder: dict[str, Any] = {}

        def run_cancel() -> None:
            holder["result"] = be.cancel(run_id)

        worker = threading.Thread(target=run_cancel, name="cancel-rework-race", daemon=True)
        underlying.acquire()
        try:
            ctrl._lock = _LockGate(underlying, arrived)
            worker.start()
            self.assertTrue(arrived.wait(10), "cancel did not reach the controller lock")
            self.assertTrue(worker.is_alive(), "cancel finished before the rework transition")
            # Legal rework transition while cancel is blocked on the controller lock.
            review = rec["review"]
            review["state"] = "running"
            review["request_id"] = None
            rec["proc"] = proc
            rec["state"] = "running"
            rec["activity"] = "busy"
            rec["harvested"] = False
            rec["cancelled"] = False
            rec["stdout_chunks"] = []
            rec["stderr_chunks"] = []
            rec["_children_seen"] = []
        finally:
            underlying.release()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "cancel did not finish after the lock was released")

        code, body = holder["result"]
        self.assertEqual(
            effects,
            ["kill", "exited", "lease released"],
            f"effect order; proc.killed={proc.killed} poll={proc.poll()}",
        )
        self.assertTrue(proc.killed, "rework proc was not killed")
        self.assertIsNotNone(proc.poll(), "rework proc has not exited")
        self.assertEqual(code, 200, body)
        self.assertEqual(body.get("state"), "cancelled", body)
        self.assertEqual(rec["review"]["state"], "cancelled")
        self.assertIsNone(be._review._store.get(run_id), "review store record was not forgotten")

    def test_parked_cancel_releases_lease_without_kill(self) -> None:
        """asserts a truly parked awaiting_review run with no proc cancels, releases the lease, and does not kill."""
        be = self._backend()
        run_id = "agy_cancel_parked_only"
        rec = self._parked(be, run_id)
        calls: list[str] = []

        def fake_kill(live: Any, rec_arg: Any = None) -> bool:
            del live, rec_arg
            calls.append("kill")
            return True

        def fake_release(rec_arg: Any) -> None:
            del rec_arg
            calls.append("lease released")

        be._kill_proc = fake_kill  # type: ignore[method-assign]
        be._release_pool_lease_after_cancel = fake_release  # type: ignore[method-assign]

        code, body = be.cancel(run_id)
        self.assertEqual(code, 200, body)
        self.assertEqual(body.get("state"), "cancelled", body)
        self.assertEqual(calls, ["lease released"])
        self.assertEqual(rec["review"]["state"], "cancelled")
        self.assertIsNone(be._review._store.get(run_id))

    def test_kill_failure_does_not_release_lease_or_succeed(self) -> None:
        """asserts a live review proc that is still alive after a failed kill returns 501 and does not release the lease."""
        be = self._backend()
        run_id = "agy_cancel_kill_fails"
        rec = self._parked(be, run_id)
        proc = _LiveProc(reap_on_wait=False)
        rec["review"]["state"] = "running"
        rec["proc"] = proc
        rec["state"] = "running"
        rec["activity"] = "busy"
        rec["harvested"] = False
        released: list[str] = []

        def fake_kill(live: Any, rec_arg: Any = None) -> bool:
            del rec_arg
            live.kill()
            live.wait()
            return False

        def fake_release(rec_arg: Any) -> None:
            del rec_arg
            released.append("lease released")

        be._kill_proc = fake_kill  # type: ignore[method-assign]
        be._release_pool_lease_after_cancel = fake_release  # type: ignore[method-assign]

        code, body = be.cancel(run_id)
        self.assertEqual(code, 501, body)
        self.assertFalse(body.get("ok"), body)
        self.assertEqual(released, [])
        self.assertIsNone(proc.poll(), "stubborn proc should still be alive")
        self.assertTrue(proc.killed)
        self.assertNotEqual(rec["review"]["state"], "cancelled")
        self.assertIsNotNone(be._review._store.get(run_id), "failed kill forgot the review record")
        self.assertNotEqual(body.get("state"), "cancelled")


if __name__ == "__main__":
    unittest.main()
