#!/usr/bin/env python3
"""Peripheral agy account-pool scheduler — mock subprocess + fake pool."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend import (  # noqa: E402
    AntigravityCliExecutionBackend,
    get_execution_backend,
    run_antigravity_charter,
)
from execution_backend.agy_account_pool import (  # noqa: E402
    ENV_POOL,
    FORCE_FILE_STORAGE,
    apply_class_to_state,
    load_pool,
    mark_account,
    prepare_antigravity_environ_from_pool,
    save_pool,
    select_account,
)
from execution_backend.agy_error_classify import classify  # noqa: E402
from execution_backend.antigravity_cli_v1 import SKIP_PERMISSIONS_FLAG  # noqa: E402
from charter import load_charter  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"
FIXTURE = SRC / "fixtures" / "agy_fake_errors.jsonl"
EXAMPLE_POOL = REPO / "jobs/examples/agy_account_pool.example.json"

_FAKE_AGY = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv_path = os.environ.get("AGY_FAKE_ARGV", "")
if argv_path:
    Path(argv_path).write_text(json.dumps(sys.argv), encoding="utf-8")
env_path = os.environ.get("AGY_FAKE_ENV", "")
if env_path:
    keys = [
        "HOME",
        "AGY_PROFILE",
        "GEMINI_FORCE_FILE_STORAGE",
        "AGY_BIN",
        "AGY_MODEL",
        "AGY_AUTO_APPROVE",
        "COLLAB_AGY_AUTO_APPROVE",
        "COLLAB_AGY_ACCOUNT_POOL",
    ]
    Path(env_path).write_text(json.dumps({k: os.environ.get(k) for k in keys}), encoding="utf-8")
art = os.environ.get("AGY_FAKE_ARTIFACT", "")
if art:
    p = Path(art)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(os.environ.get("AGY_FAKE_ARTIFACT_BODY", "hello from agy fake\n"), encoding="utf-8")
payload = {
    "conversation_id": os.environ.get("AGY_FAKE_CONV", "conv_pool_1"),
    "status": os.environ.get("AGY_FAKE_STATUS", "SUCCESS"),
    "response": os.environ.get("AGY_FAKE_RESPONSE", "ok"),
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
sys.stdout.write(json.dumps(payload))
sys.stdout.write("\n")
sys.stderr.write(os.environ.get("AGY_FAKE_STDERR", ""))
raise SystemExit(int(os.environ.get("AGY_FAKE_EXIT", "0")))
'''


def _write_fake_agy(dirpath: str | Path) -> Path:
    p = Path(dirpath) / "fake-agy"
    p.write_text(_FAKE_AGY, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _pool_dict(
    *,
    a_state: str = "available",
    b_state: str = "unavailable",
    c_state: str = "available",
    home_root: str = "/path/to/profiles",
) -> dict:
    return {
        "accounts": [
            {
                "id": "A",
                "home": f"{home_root}/homeA",
                "state": a_state,
                "email_mask": "a***@example.com",
                "notes": "fake primary",
            },
            {
                "id": "B",
                "home": f"{home_root}/homeB",
                "state": b_state,
                "email_mask": "b***@example.com",
                "notes": "eligibility_blocked",
            },
            {
                "id": "C",
                "home": f"{home_root}/homeC",
                "state": c_state,
                "email_mask": "c***@example.com",
                "notes": "fake standby",
            },
        ]
    }


def _write_pool_file(dirpath: str | Path, payload: dict | None = None) -> Path:
    p = Path(dirpath) / "pool.json"
    p.write_text(json.dumps(payload or _pool_dict(), indent=2) + "\n", encoding="utf-8")
    return p


def _clean_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    for k in (
        "AGY_AUTO_APPROVE",
        "COLLAB_AGY_AUTO_APPROVE",
        ENV_POOL,
        "COLLAB_AGY_POOL_PRECHECK",
        "COLLAB_AGY_POOL_LIVE",
        "COLLAB_EXECUTION_BACKEND",
        "AGY_PROFILE",
    ):
        env.pop(k, None)
    if extra:
        env.update(extra)
    return env


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_agy_pool", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pool_live_opt_in() -> bool:
    return str(os.environ.get("COLLAB_AGY_POOL_LIVE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class TestClassifierFixture(unittest.TestCase):
    def test_fixture_all_green_including_eligibility(self):
        self.assertTrue(FIXTURE.is_file(), FIXTURE)
        rows = [
            json.loads(line)
            for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreaterEqual(len(rows), 10)
        elig = [r for r in rows if r["expect"] == "eligibility_blocked"]
        self.assertGreaterEqual(len(elig), 2, "must include both eligibility fixtures")
        failed = []
        for r in rows:
            got = classify(r.get("stdout", ""), r.get("stderr", ""))
            if got != r["expect"]:
                failed.append({"id": r["id"], "expect": r["expect"], "got": got})
        self.assertEqual(failed, [], failed)

    def test_model_503_no_capacity_is_quota_not_eligibility(self):
        blob = (
            '{"status":"ERROR","error":"API error (attempt 3): '
            'UNAVAILABLE (code 503): No capacity available for model '
            'gemini-3.8-flash-low on the server"}'
        )
        got = classify(blob, "")
        self.assertEqual(got, "quota_exhausted")
        self.assertNotEqual(got, "eligibility_blocked")


class TestPoolSelectHandoff(unittest.TestCase):
    def test_example_json_has_fake_paths_no_tokens(self):
        raw = EXAMPLE_POOL.read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"(access_token|refresh_token|id_token)", raw)
        obj = json.loads(raw)
        homes = [a["home"] for a in obj["accounts"]]
        self.assertEqual(
            homes,
            [
                "/path/to/profiles/homeA",
                "/path/to/profiles/homeB",
                "/path/to/profiles/homeC",
            ],
        )
        self.assertEqual(obj["accounts"][1]["state"], "unavailable")

    def test_load_save_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_pool_file(td)
            pool = load_pool(path)
            self.assertEqual([a.id for a in pool.accounts], ["A", "B", "C"])
            mark_account(pool, "A", "exhausted")
            again = load_pool(path)
            self.assertEqual(again.by_id("A").state, "exhausted")
            dumped = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("token", json.dumps(dumped))

    def test_serial_handoff_A_then_exhausted_then_C_never_B(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_pool_file(
                td,
                _pool_dict(a_state="available", b_state="unavailable", c_state="available"),
            )
            pool = load_pool(path)
            first = select_account(pool)
            self.assertIsNotNone(first)
            self.assertEqual(first.id, "A")
            mark_account(pool, "A", "exhausted")
            second = select_account(pool)
            self.assertIsNotNone(second)
            self.assertEqual(second.id, "C")
            self.assertNotEqual(second.id, "B")
            ids = []
            for _ in range(5):
                picked = select_account(pool)
                self.assertIsNotNone(picked)
                ids.append(picked.id)
            self.assertTrue(all(i == "C" for i in ids), ids)
            self.assertNotIn("B", ids)
            self.assertEqual(pool.by_id("B").state, "unavailable")

    def test_precheck_eligibility_skips_B(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_pool_file(
                td,
                _pool_dict(a_state="exhausted", b_state="available", c_state="available"),
            )
            pool = load_pool(path)
            seen: list[str] = []

            def precheck(acc):
                seen.append(acc.id)
                if acc.id == "B":
                    return {
                        "stdout": json.dumps(
                            {
                                "status": "ERROR",
                                "error": (
                                    "Eligibility check failed: Your current account "
                                    "is not eligible for Antigravity, because it is "
                                    "not currently available in your location."
                                ),
                            }
                        ),
                        "stderr": "Eligibility check failed: not currently available in your location.\n",
                        "rc": 1,
                    }
                return "ok"

            picked = select_account(pool, precheck=precheck)
            self.assertEqual(picked.id, "C")
            self.assertEqual(pool.by_id("B").state, "unavailable")
            self.assertIn("B", seen)
            self.assertNotIn("B", [picked.id])
            again = select_account(pool, precheck=precheck)
            self.assertEqual(again.id, "C")
            self.assertNotIn("B", [a.id for a in pool.available()])

    def test_precheck_quota_and_rate_limit(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_pool_file(
                td,
                _pool_dict(a_state="available", b_state="available", c_state="available"),
            )
            pool = load_pool(path)

            def precheck(acc):
                if acc.id == "A":
                    return "quota_exhausted"
                if acc.id == "B":
                    return "rate_limit"
                return "ok"

            picked = select_account(pool, precheck=precheck)
            self.assertEqual(picked.id, "C")
            self.assertEqual(pool.by_id("A").state, "exhausted")
            self.assertEqual(pool.by_id("B").state, "cooldown")

    def test_apply_class_eligibility_not_quota(self):
        with tempfile.TemporaryDirectory() as td:
            pool = load_pool(_write_pool_file(td))
            acc = pool.by_id("A")
            apply_class_to_state(acc, "eligibility_blocked")
            self.assertEqual(acc.state, "unavailable")
            acc2 = pool.by_id("C")
            apply_class_to_state(acc2, "quota_exhausted")
            self.assertEqual(acc2.state, "exhausted")


class TestFactoryAndRunJobPool(unittest.TestCase):
    def test_factory_injects_home_profile_file_storage_no_skip_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path = _write_pool_file(td)
            fake = _write_fake_agy(td)
            argv_path = str(Path(td) / "argv.json")
            env_path = str(Path(td) / "env.json")
            ws = Path(td) / "ws"
            ws.mkdir()
            base = _clean_env(
                {
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ARGV": argv_path,
                    "AGY_FAKE_ENV": env_path,
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                    "PATH": os.environ.get("PATH", "/usr/bin"),
                }
            )
            be = get_execution_backend(
                "antigravity",
                account_pool_path=str(pool_path),
                environ=base,
                persist_pool=False,
            )
            self.assertIsInstance(be, AntigravityCliExecutionBackend)
            injected = dict(be._env())
            self.assertEqual(injected["HOME"], "/path/to/profiles/homeA")
            self.assertEqual(injected["AGY_PROFILE"], "A")
            self.assertEqual(injected[FORCE_FILE_STORAGE], "true")
            self.assertEqual(injected["AGY_BIN"], str(fake))
            started = be.start_run(
                title="hello",
                directory=str(ws),
                instruction="write the hello file",
                artifacts=["hello-from-worker.txt"],
            )
            self.assertFalse(started.get("skip_permissions"), started)
            self.assertEqual(started.get("agy_profile"), "A")
            deadline = time.time() + 5
            while be.observe_run(started["run_id"]).get("busy") and time.time() < deadline:
                time.sleep(0.05)
            collected = be.collect_result(started["run_id"])
            self.assertTrue(collected["ok"], collected)
            self.assertFalse(collected.get("skip_permissions"))
            self.assertEqual(collected.get("agy_profile"), "A")
            argv = json.loads(Path(argv_path).read_text(encoding="utf-8"))
            self.assertNotIn(SKIP_PERMISSIONS_FLAG, argv)
            dumped = json.loads(Path(env_path).read_text(encoding="utf-8"))
            self.assertEqual(dumped["HOME"], "/path/to/profiles/homeA")
            self.assertEqual(dumped["AGY_PROFILE"], "A")
            self.assertEqual(dumped["GEMINI_FORCE_FILE_STORAGE"], "true")

    def test_run_antigravity_charter_uses_pool_and_skips_permissions(self):
        charter = load_charter(HELLO)
        with tempfile.TemporaryDirectory() as td:
            pool_path = _write_pool_file(td)
            fake = _write_fake_agy(td)
            argv_path = str(Path(td) / "argv.json")
            ws = Path(td) / "ws"
            ws.mkdir()
            base = _clean_env(
                {
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ARGV": argv_path,
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                    "PATH": os.environ.get("PATH", "/usr/bin"),
                }
            )
            result = run_antigravity_charter(
                charter=charter,
                workdir=ws,
                instruction="write the hello file",
                name="hello",
                timeout_sec=10,
                environ=base,
                account_pool_path=str(pool_path),
            )
            self.assertTrue(result["ok"], result)
            self.assertFalse(result.get("skip_permissions"))
            self.assertEqual(result.get("agy_profile"), "A")
            argv = json.loads(Path(argv_path).read_text(encoding="utf-8"))
            self.assertNotIn(
                SKIP_PERMISSIONS_FLAG,
                argv,
                "default skip-permissions must stay off unless explicitly approved",
            )

    def test_prepare_helper_preserves_agy_bin_model(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_pool_file(td)
            prepared = prepare_antigravity_environ_from_pool(
                path,
                base_environ={"AGY_BIN": "/opt/agy", "AGY_MODEL": "gemini-x", "PATH": "/bin"},
                persist=False,
            )
            env = prepared["environ"]
            self.assertEqual(env["HOME"], "/path/to/profiles/homeA")
            self.assertEqual(env["AGY_PROFILE"], "A")
            self.assertEqual(env[FORCE_FILE_STORAGE], "true")
            self.assertEqual(env["AGY_BIN"], "/opt/agy")
            self.assertEqual(env["AGY_MODEL"], "gemini-x")
            self.assertNotIn("AGY_AUTO_APPROVE", env)


class TestRunJobCliPool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_run_job()

    def _run(self, argv, env_extra=None):
        env = _clean_env(env_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = self.mod.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_flag_agy_account_pool_injects_env(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path = _write_pool_file(td)
            fake = _write_fake_agy(td)
            argv_path = str(Path(td) / "argv.json")
            env_path = str(Path(td) / "env.json")
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                [
                    "--backend",
                    "antigravity",
                    "--agy-account-pool",
                    str(pool_path),
                    "--workspace",
                    ws,
                    "--runs-dir",
                    runs,
                    str(HELLO),
                ],
                env_extra={
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ARGV": argv_path,
                    "AGY_FAKE_ENV": env_path,
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                    "PATH": os.environ.get("PATH", "/usr/bin"),
                },
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"], summary)
            self.assertEqual(summary.get("agy_profile"), "A")
            self.assertFalse(summary.get("skip_permissions"))
            argv = json.loads(Path(argv_path).read_text(encoding="utf-8"))
            self.assertNotIn(SKIP_PERMISSIONS_FLAG, argv)
            dumped = json.loads(Path(env_path).read_text(encoding="utf-8"))
            self.assertEqual(dumped["HOME"], "/path/to/profiles/homeA")
            self.assertEqual(dumped["AGY_PROFILE"], "A")
            self.assertEqual(dumped["GEMINI_FORCE_FILE_STORAGE"], "true")

    def test_env_collab_agy_account_pool(self):
        with tempfile.TemporaryDirectory() as td:
            pool_path = _write_pool_file(td)
            fake = _write_fake_agy(td)
            env_path = str(Path(td) / "env.json")
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                [
                    "--backend",
                    "agy",
                    "--workspace",
                    ws,
                    "--runs-dir",
                    runs,
                    str(HELLO),
                ],
                env_extra={
                    ENV_POOL: str(pool_path),
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ENV": env_path,
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                    "PATH": os.environ.get("PATH", "/usr/bin"),
                },
            )
            self.assertEqual(rc, 0, err + out)
            dumped = json.loads(Path(env_path).read_text(encoding="utf-8"))
            self.assertEqual(dumped["HOME"], "/path/to/profiles/homeA")
            self.assertEqual(dumped["AGY_PROFILE"], "A")


@unittest.skipUnless(_pool_live_opt_in(), "set COLLAB_AGY_POOL_LIVE=1 for live pool precheck")
class TestLiveAgyPool(unittest.TestCase):
    def test_live_optional_gated(self):
        path = str(os.environ.get(ENV_POOL) or "").strip()
        if not path:
            self.skipTest("COLLAB_AGY_ACCOUNT_POOL not set")
        pool = load_pool(path)
        picked = select_account(pool, precheck=None, persist=False)
        self.assertTrue(picked is None or picked.state == "available")


class TestNoTeleagentAdapterImportSideEffect(unittest.TestCase):
    def test_pool_module_does_not_import_teleagent_adapter(self):
        import execution_backend.agy_account_pool as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+teleagent_adapter")
        self.assertNotIn("agy-multi-account-probe", src)


if __name__ == "__main__":
    unittest.main()
