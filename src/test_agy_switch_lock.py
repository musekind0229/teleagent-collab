#!/usr/bin/env python3
"""Account switch lock: cross-process mutex around keyring clear + HOME env + Popen.

Mock keyring / Popen only. Real flock (POSIX) / LockFileEx (Windows) and real
child processes for death / timeout recovery.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend import agy_account_pool as pool_mod  # noqa: E402
from execution_backend.agy_account_pool import (  # noqa: E402
    ENV_LEASE_ID,
    AccountPoolError,
    AccountSwitchLockTimeout,
    account_switch_lock,
    apply_job_result_to_pool,
    finish_account_lease,
    home_lease_lock_path,
    load_pool,
    prepare_antigravity_environ_from_pool,
    release_account_lease,
    switch_lock_held,
)
from execution_backend.antigravity_cli_v1 import AntigravityCliExecutionBackend  # noqa: E402


def _write_pool(td: str, ids=("A", "B"), **acct_extra) -> tuple[Path, dict]:
    accounts = []
    for i in ids:
        home = Path(td) / "homes" / i
        home.mkdir(parents=True, exist_ok=True)
        row = {"id": i, "home": str(home), "state": "available"}
        row.update(acct_extra.get(i, {}))
        accounts.append(row)
    p = Path(td) / "pool.json"
    p.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "COLLAB_AGY_LOCK_DIR": str(Path(td) / "locks")}
    return p, env


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


_CHILD_PREPARE = textwrap.dedent(
    """
    import json, os, sys, time
    from execution_backend import agy_account_pool as m
    pool_path, lock_dir, trace, gate, mode = sys.argv[1:6]
    def fake_clear():
        t0 = time.time(); time.sleep(0.3)
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "t0": t0, "t1": time.time()}) + "\\n")
    m.clear_windows_antigravity_keyring = fake_clear
    env = {"PATH": os.environ.get("PATH", ""), "COLLAB_AGY_LOCK_DIR": lock_dir}
    prep = m.prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True)
    print(json.dumps({"profile": prep["agy_profile"], "home": prep["environ"]["HOME"]}), flush=True)
    if mode == "die":
        os._exit(0)  # holder death: no release
    deadline = time.time() + 20
    while not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.05)
    """
)

_CHILD_HOLD_LOCK = textwrap.dedent(
    """
    import sys, time
    from execution_backend.agy_account_pool import account_switch_lock
    pool_path, lock_dir = sys.argv[1:3]
    with account_switch_lock(pool_path, environ={"COLLAB_AGY_LOCK_DIR": lock_dir}):
        print("held", flush=True)
        time.sleep(60)
    """
)


def _intervals_disjoint(rows: list[dict]) -> bool:
    rows = sorted(rows, key=lambda r: r["t0"])
    return all(rows[i]["t1"] <= rows[i + 1]["t0"] for i in range(len(rows) - 1))


class TestSwitchLockConcurrency(unittest.TestCase):
    def test_two_threads_prepare_serialized_and_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td)
            trace: list[dict] = []
            mu = threading.Lock()

            def fake_clear():
                t0 = time.time()
                time.sleep(0.2)
                with mu:
                    trace.append({"t0": t0, "t1": time.time()})

            barrier = threading.Barrier(2)
            got: list = []
            errs: list = []

            def worker():
                try:
                    barrier.wait()
                    got.append(prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True))
                except Exception as e:  # noqa: BLE001
                    errs.append(e)

            with patch.object(pool_mod, "clear_windows_antigravity_keyring", fake_clear):
                ts = [threading.Thread(target=worker) for _ in range(2)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(10)
            self.assertEqual(errs, [])
            self.assertEqual(sorted(p["agy_profile"] for p in got), ["A", "B"])
            self.assertEqual(len({p["environ"]["HOME"] for p in got}), 2)
            self.assertTrue(_intervals_disjoint(trace), trace)
            for p in got:
                release_account_lease(load_pool(pool_path), p["agy_profile"], lease_id=p["lease_id"])
            self.assertEqual({a.state for a in load_pool(pool_path).accounts}, {"available"})

    def test_two_processes_prepare_serialized_and_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td)
            trace = Path(td) / "trace.jsonl"
            gate = Path(td) / "gate"
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", _CHILD_PREPARE, str(pool_path), env["COLLAB_AGY_LOCK_DIR"],
                     str(trace), str(gate), "wait"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env(),
                )
                for _ in range(2)
            ]
            try:
                outs = [json.loads(p.stdout.readline()) for p in procs]
                gate.write_text("go", encoding="utf-8")
                for p in procs:
                    p.wait(20)
            finally:
                for p in procs:
                    if p.poll() is None:
                        p.kill()
                    p.stdout.close()
                    p.stderr.close()
            self.assertEqual(sorted(o["profile"] for o in outs), ["A", "B"])
            self.assertNotEqual(outs[0]["home"], outs[1]["home"])
            rows = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(_intervals_disjoint(rows), rows)

    def test_held_home_is_skipped_not_fatal(self):
        from platform_services import get_file_lock

        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td)
            held = get_file_lock().acquire(
                home_lease_lock_path(Path(td) / "homes" / "A", environ=env), blocking=False
            )
            try:
                with patch.object(pool_mod, "clear_windows_antigravity_keyring"):
                    prep = prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True)
                self.assertEqual(prep["agy_profile"], "B")
                release_account_lease(load_pool(pool_path), "B", lease_id=prep["lease_id"])
            finally:
                held.unlock_and_close()


class TestSwitchLockRecovery(unittest.TestCase):
    def test_acquire_timeout_is_clear_and_dead_holder_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td)
            child = subprocess.Popen(
                [sys.executable, "-c", _CHILD_HOLD_LOCK, str(pool_path), env["COLLAB_AGY_LOCK_DIR"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env(),
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "held")
                t0 = time.monotonic()
                with self.assertRaises(AccountSwitchLockTimeout) as cm:
                    with account_switch_lock(pool_path, environ=env, timeout_sec=0.3):
                        pass
                self.assertLess(time.monotonic() - t0, 5)
                msg = str(cm.exception)
                self.assertIn("switch lock busy", msg)
                self.assertIn(str(child.pid), msg)  # holder pid named
                self.assertIsInstance(cm.exception, AccountPoolError)
                # prepare surfaces the same timeout (no silent hang)
                with self.assertRaises(AccountSwitchLockTimeout):
                    prepare_antigravity_environ_from_pool(
                        pool_path, base_environ=env, persist=True, lock_timeout_sec=0.2
                    )
                child.kill()
                child.wait(10)
                with account_switch_lock(pool_path, environ=env, timeout_sec=5):
                    self.assertTrue(switch_lock_held(pool_path, environ=env))
            finally:
                if child.poll() is None:
                    child.kill()
                child.stdout.close()
                child.stderr.close()

    def test_dead_lease_holder_is_reclaimed_before_lease_until(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td, ids=("A",))
            r = subprocess.run(
                [sys.executable, "-c", _CHILD_PREPARE, str(pool_path), env["COLLAB_AGY_LOCK_DIR"],
                 str(Path(td) / "t.jsonl"), str(Path(td) / "gate"), "die"],
                capture_output=True, text=True, env=_child_env(), timeout=30,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            acc = load_pool(pool_path).by_id("A")
            self.assertEqual(acc.state, "busy")
            self.assertGreater(pool_mod._parse_iso(acc.lease_until), time.time() + 60)
            with patch.object(pool_mod, "clear_windows_antigravity_keyring"):
                prep = prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True)
            self.assertEqual(prep["agy_profile"], "A")
            self.assertEqual(prep["reclaimed"], ["A"])
            self.assertEqual(load_pool(pool_path).by_id("A").lease_pid, os.getpid())
            release_account_lease(load_pool(pool_path), "A", lease_id=prep["lease_id"])

    def test_live_foreign_lease_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td, ids=("A",))
            gate = Path(td) / "gate"
            child = subprocess.Popen(
                [sys.executable, "-c", _CHILD_PREPARE, str(pool_path), env["COLLAB_AGY_LOCK_DIR"],
                 str(Path(td) / "t.jsonl"), str(gate), "wait"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env(),
            )
            try:
                self.assertEqual(json.loads(child.stdout.readline())["profile"], "A")
                with patch.object(pool_mod, "clear_windows_antigravity_keyring"):
                    with self.assertRaises(AccountPoolError):
                        prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True)
                gate.write_text("go", encoding="utf-8")
                child.wait(20)
            finally:
                if child.poll() is None:
                    child.kill()
                child.stdout.close()
                child.stderr.close()

    def test_expired_lease_is_taken_over(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(
                td,
                ids=("A",),
                A={"state": "busy", "lease_pid": os.getpid(), "lease_until": "2000-01-01T00:00:00Z",
                   "lease_id": "old"},
            )
            with patch.object(pool_mod, "clear_windows_antigravity_keyring"):
                prep = prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True)
            self.assertEqual(prep["agy_profile"], "A")
            self.assertNotEqual(prep["lease_id"], "old")
            release_account_lease(load_pool(pool_path), "A", lease_id=prep["lease_id"])


class TestLeaseOwnership(unittest.TestCase):
    def test_late_writeback_does_not_free_other_lease(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td, ids=("A",))
            with patch.object(pool_mod, "clear_windows_antigravity_keyring"):
                prep = prepare_antigravity_environ_from_pool(pool_path, base_environ=env, persist=True)
            self.assertEqual(prep["environ"][ENV_LEASE_ID], prep["lease_id"])
            pool = load_pool(pool_path)
            release_account_lease(pool, "A", lease_id="someone-else")
            self.assertEqual(load_pool(pool_path).by_id("A").state, "busy")
            pool = load_pool(pool_path)
            apply_job_result_to_pool(pool, pool.by_id("A"), {"ok": True}, lease_id="someone-else")
            self.assertEqual(load_pool(pool_path).by_id("A").state, "busy")
            cls = finish_account_lease(pool_path, "A", {"ok": True}, lease_id=prep["lease_id"], environ=env)
            self.assertEqual(cls, "ok")
            acc = load_pool(pool_path).by_id("A")
            self.assertEqual((acc.state, acc.lease_id, acc.lease_pid), ("available", None, None))


class TestStartRunCriticalSection(unittest.TestCase):
    def test_keyring_clear_and_popen_run_inside_switch_lock(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path, env = _write_pool(td, ids=("A",))
            seen: dict = {}

            def other_thread_can_lock() -> bool:
                box = {}

                def probe():
                    try:
                        with account_switch_lock(pool_path, environ=env, timeout_sec=0.1):
                            box["ok"] = True
                    except AccountSwitchLockTimeout:
                        box["ok"] = False

                t = threading.Thread(target=probe)
                t.start()
                t.join(5)
                return box["ok"]

            def fake_clear():
                seen.setdefault("clear_locked", []).append(not other_thread_can_lock())

            def fake_popen(cmd, **kw):
                seen["popen_locked"] = not other_thread_can_lock()
                seen["home"] = kw["env"]["HOME"]
                raise OSError("stop")

            be = AntigravityCliExecutionBackend(
                bin_path="agy", environ=env, account_pool_path=str(pool_path)
            )
            with patch.object(pool_mod, "clear_windows_antigravity_keyring", fake_clear), patch(
                "execution_backend.antigravity_cli_v1.clear_windows_antigravity_keyring", fake_clear
            ), patch("execution_backend.antigravity_cli_v1.subprocess.Popen", fake_popen):
                started = be.start_run(title="t", directory=str(Path(td) / "w"), instruction="hi")
            self.assertFalse(started["ok"])
            self.assertEqual(seen["clear_locked"], [True, True])
            self.assertTrue(seen["popen_locked"])
            self.assertEqual(seen["home"], str(Path(td) / "homes" / "A"))
            # failed spawn released the lease (under the same re-entrant lock)
            self.assertEqual(load_pool(pool_path).by_id("A").state, "available")
            self.assertTrue(other_thread_can_lock())


if __name__ == "__main__":
    unittest.main()
