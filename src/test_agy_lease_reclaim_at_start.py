#!/usr/bin/env python3
"""Dead agy pool leases are reclaimed when the backend starts.

RED on 23cc991: a busy lease whose holder was SIGKILLed (e.g. ``kill -9`` of
collab-service) stayed busy until ``lease_until`` because nothing reclaimed it
at start; a reused pid number was mistaken for the live holder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend import agy_account_pool as pool_mod  # noqa: E402
from execution_backend.agy_account_pool import load_pool, reserve_account, save_pool  # noqa: E402
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}
    env.update(extra)
    return env


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@unittest.skipIf(os.name == "nt", "POSIX pid / flock semantics")
class LeaseReclaimAtStartTests(unittest.TestCase):
    def _pool(self, td: str, *, lease_pid: int, token: str | None = None) -> Path:
        home = Path(td) / "homeA"
        home.mkdir()
        acc = {
            "id": "primary",
            "home": str(home),
            "state": "busy",
            "lease_pid": lease_pid,
            "lease_until": "2999-01-01T00:00:00Z",
            "lease_id": "deadbeef",
        }
        if token is not None:
            acc["lease_proc_start"] = token
        path = Path(td) / "pool.json"
        path.write_text(json.dumps({"accounts": [acc]}), encoding="utf-8")
        return path

    def _backend(self, pool: Path, td: str) -> AntigravityCliExecutionBackend:
        env = _clean_env(COLLAB_AGY_LOCK_DIR=str(Path(td) / "locks"))
        return AntigravityCliExecutionBackend(bin_path="agy-test", environ=env, account_pool_path=str(pool))

    def test_dead_holder_lease_reclaimed_at_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pool = self._pool(td, lease_pid=_dead_pid())
            be = self._backend(pool, td)
            acc = load_pool(pool).accounts[0]
            self.assertEqual(acc.state, "available")
            self.assertIsNone(acc.lease_pid)
            self.assertEqual([r["id"] for r in be.reclaimed_leases_at_start], ["primary"])

    def test_live_holder_lease_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                from execution_backend.agy_run_registry import process_start_token

                pool = self._pool(td, lease_pid=live.pid, token=process_start_token(live.pid))
                self._backend(pool, td)
                acc = load_pool(pool).accounts[0]
                self.assertEqual(acc.state, "busy")
                self.assertEqual(acc.lease_pid, live.pid)
            finally:
                live.kill()
                live.wait()

    def test_reused_pid_lease_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                # Same pid number, but the recorded holder started at another time.
                pool = self._pool(td, lease_pid=live.pid, token="linux:1")
                self._backend(pool, td)
                self.assertEqual(load_pool(pool).accounts[0].state, "available")
            finally:
                live.kill()
                live.wait()

    def test_reserve_records_start_token_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pool_path = self._pool(td, lease_pid=_dead_pid())
            pool = load_pool(pool_path)
            acc = pool.accounts[0]
            reserve_account(acc)
            self.assertTrue(getattr(acc, "lease_proc_start", None))
            save_pool(pool_path, pool)
            again = load_pool(pool_path).accounts[0]
            self.assertEqual(again.lease_proc_start, acc.lease_proc_start)

    def test_held_home_lock_keeps_lease(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pool = self._pool(td, lease_pid=_dead_pid())
            env = _clean_env(COLLAB_AGY_LOCK_DIR=str(Path(td) / "locks"))
            from platform_services import get_file_lock

            home = load_pool(pool).accounts[0].home
            lock = pool_mod.home_lease_lock_path(home, environ=env)
            lock.parent.mkdir(parents=True, exist_ok=True)
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX); "
                    "print('locked', flush=True); time.sleep(30)",
                    str(lock),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                AntigravityCliExecutionBackend(bin_path="agy-test", environ=env, account_pool_path=str(pool))
                self.assertEqual(load_pool(pool).accounts[0].state, "busy")
            finally:
                holder.kill()
                holder.wait()
            del get_file_lock


if __name__ == "__main__":
    unittest.main()
