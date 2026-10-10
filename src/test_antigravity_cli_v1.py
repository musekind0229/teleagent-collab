#!/usr/bin/env python3
"""antigravity.cli_v1 ExecutionBackend — simulated subprocess; live agy skippable."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
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
    BackendError,
    BackendStatus,
    KIND_ANTIGRAVITY,
    get_execution_backend,
    resolve_run_job_backend,
    run_antigravity_charter,
)
from execution_backend.antigravity_cli_v1 import (  # noqa: E402
    DEFAULT_AGY_MODEL,
    SKIP_PERMISSIONS_FLAG,
    agy_auto_approve_enabled,
    build_agy_argv,
    parse_agy_json,
)
from charter import load_charter  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"

_FAKE_AGY = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

argv_path = os.environ.get("AGY_FAKE_ARGV", "")
if argv_path:
    Path(argv_path).write_text(json.dumps(sys.argv), encoding="utf-8")
sleep_s = float(os.environ.get("AGY_FAKE_SLEEP", "0") or "0")
if sleep_s > 0:
    time.sleep(sleep_s)
art = os.environ.get("AGY_FAKE_ARTIFACT", "")
if art:
    p = Path(art)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(os.environ.get("AGY_FAKE_ARTIFACT_BODY", "hello from agy fake\n"), encoding="utf-8")
payload = {
    "conversation_id": os.environ.get("AGY_FAKE_CONV", "conv_fake_1"),
    "status": os.environ.get("AGY_FAKE_STATUS", "ok"),
    "response": os.environ.get("AGY_FAKE_RESPONSE", "wrote artifact"),
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
sys.stdout.write(json.dumps(payload))
sys.stdout.write("\n")
sys.stderr.write(os.environ.get("AGY_FAKE_STDERR", ""))
raise SystemExit(int(os.environ.get("AGY_FAKE_EXIT", "0")))
'''


def _write_fake_agy(dirpath: str | Path) -> Path:
    """Return a spawnable fake agy. Windows cannot CreateProcess a shebang script."""
    root = Path(dirpath)
    if os.name == "nt":
        script = root / "fake-agy.py"
        script.write_text(_FAKE_AGY, encoding="utf-8")
        cmd = root / "fake-agy.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return cmd
    p = root / "fake-agy"
    p.write_text(_FAKE_AGY, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _hello_charter() -> dict:
    return load_charter(HELLO)


def _agy_live_ready() -> bool:
    return bool(shutil.which("agy") or Path("/home/box/.local/bin/agy").is_file())


def _agy_live_opt_in() -> bool:
    return str(os.environ.get("COLLAB_AGY_LIVE") or "").strip().lower() in ("1", "true", "yes")


def _load_run_job():
    path = REPO / "bin" / "run-job.py"
    spec = importlib.util.spec_from_file_location("run_job_cli_agy", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestArgvAndFlags(unittest.TestCase):
    def test_print_uses_equals(self):
        cmd = build_agy_argv(
            bin_path="agy",
            model=DEFAULT_AGY_MODEL,
            prompt="hello",
            skip_permissions=False,
        )
        self.assertIn("--output-format=json", cmd)
        self.assertIn(f"--model={DEFAULT_AGY_MODEL}", cmd)
        self.assertTrue(any(a.startswith("--print=") for a in cmd))
        self.assertNotIn("--print", cmd)
        self.assertNotIn("-p", cmd)
        self.assertNotIn(SKIP_PERMISSIONS_FLAG, cmd)

    def test_skip_permissions_only_when_requested(self):
        cmd = build_agy_argv(
            bin_path="agy",
            model="m",
            prompt="x",
            skip_permissions=True,
        )
        self.assertIn(SKIP_PERMISSIONS_FLAG, cmd)
        print_i = next(i for i, a in enumerate(cmd) if a.startswith("--print="))
        skip_i = cmd.index(SKIP_PERMISSIONS_FLAG)
        self.assertLess(skip_i, print_i)

    def test_auto_approve_default_false(self):
        self.assertFalse(agy_auto_approve_enabled(None, {}))
        self.assertFalse(agy_auto_approve_enabled({"agy_auto_approve": False}, {}))
        self.assertTrue(agy_auto_approve_enabled({"agy_auto_approve": True}, {}))
        self.assertTrue(agy_auto_approve_enabled(None, {"AGY_AUTO_APPROVE": "true"}))
        self.assertTrue(agy_auto_approve_enabled(None, {"COLLAB_AGY_AUTO_APPROVE": "1"}))

    def test_parse_agy_json(self):
        raw = json.dumps(
            {
                "conversation_id": "c1",
                "status": "SUCCESS",
                "response": "hi",
                "usage": {"input_tokens": 2},
            }
        )
        obj = parse_agy_json("noise\n" + raw + "\n")
        self.assertEqual(obj["conversation_id"], "c1")
        self.assertEqual(obj["status"], "SUCCESS")


class TestFactoryAndResolve(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._pool_patch = patch.dict(os.environ, {}, clear=False)
        self._pool_patch.start()
        os.environ.pop("COLLAB_AGY_ACCOUNT_POOL", None)

    def tearDown(self):
        self._pool_patch.stop()
        super().tearDown()

    def test_factory_aliases(self):
        for name in ("antigravity", "antigravity.cli_v1", "agy", "agy.cli_v1"):
            be = get_execution_backend(name)
            self.assertIsInstance(be, AntigravityCliExecutionBackend)
            self.assertEqual(be.backend_id, "antigravity.cli_v1")

    def test_factory_default_still_inprocess(self):
        be = get_execution_backend()
        self.assertEqual(be.backend_id, "inprocess.local_v1")

    def test_hermes_not_offered(self):
        with self.assertRaises(BackendError) as ctx:
            get_execution_backend("hermes")
        self.assertEqual(ctx.exception.status, BackendStatus.UNSUPPORTED)

    def test_resolve_aliases(self):
        for name in ("antigravity", "antigravity.cli_v1", "agy", "agy.cli_v1"):
            self.assertEqual(resolve_run_job_backend(name, {}), KIND_ANTIGRAVITY)
        self.assertEqual(
            resolve_run_job_backend(None, {"COLLAB_EXECUTION_BACKEND": "antigravity.cli_v1"}),
            KIND_ANTIGRAVITY,
        )

    def test_resolve_hermes_still_unsupported(self):
        with self.assertRaises(BackendError) as ctx:
            resolve_run_job_backend("hermes", {})
        self.assertEqual(ctx.exception.status, BackendStatus.UNSUPPORTED)
        self.assertIn("Hermes", str(ctx.exception))


class TestSimulatedBackend(unittest.TestCase):
    def _backend(self, fake: Path, env: dict | None = None, **kwargs):
        base = os.environ.copy()
        base.pop("AGY_AUTO_APPROVE", None)
        base.pop("COLLAB_AGY_AUTO_APPROVE", None)
        if env:
            base.update(env)
        return AntigravityCliExecutionBackend(bin_path=str(fake), environ=base, **kwargs)

    def test_hello_public_api_no_skip_permissions(self):
        charter = _hello_charter()
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            argv_path = str(Path(td) / "argv.json")
            ws = Path(td) / "ws"
            ws.mkdir()
            be = self._backend(
                fake,
                {
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ARGV": argv_path,
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                    "AGY_FAKE_CONV": "conv_hello",
                },
            )
            result = run_antigravity_charter(
                charter=charter,
                workdir=ws,
                instruction="write the hello file",
                name="hello",
                timeout_sec=10,
                backend=be,
            )
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["used_public_api_only"])
            self.assertEqual(result["backend"], "antigravity.cli_v1")
            self.assertEqual(result["pending_count"], 0)
            self.assertFalse(result.get("skip_permissions"))
            self.assertTrue((ws / "hello-from-worker.txt").is_file())
            argv = json.loads(Path(argv_path).read_text(encoding="utf-8"))
            self.assertTrue(any(a.startswith("--print=") for a in argv), argv)
            self.assertNotIn("--print", argv)
            self.assertNotIn(SKIP_PERMISSIONS_FLAG, argv)
            self.assertEqual(result.get("conversation_id"), "conv_hello")

    def _run_rel_workspace(self, charter: dict, fake_art: str):
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            be = self._backend(fake, {"AGY_FAKE_ARTIFACT": fake_art})
            old_cwd = os.getcwd()
            os.chdir(td)
            try:
                rel_ws = Path("rel-app") / "ws"
                rel_ws.mkdir(parents=True)
                result = run_antigravity_charter(
                    charter=charter,
                    workdir=rel_ws,
                    instruction="write the file",
                    name="rel",
                    timeout_sec=10,
                    backend=be,
                )
                exists = (Path(td) / "rel-app" / "ws" / fake_art).is_file()
            finally:
                os.chdir(old_cwd)
            return result, exists

    def test_relative_workspace_reports_present_artifact(self):
        """Regression: relative --workspace used to double-prefix -> artifacts=[]."""
        result, exists = self._run_rel_workspace(
            {"name": "rel", "goal": "g", "done_when": {"artifacts": ["hello-from-worker.txt"]}},
            "hello-from-worker.txt",
        )
        self.assertTrue(exists)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("missing"), [], result)
        self.assertEqual(len(result["artifacts"]), 1, result)
        self.assertTrue(result["artifacts"][0].endswith("hello-from-worker.txt"))

    def test_subdir_artifact_keeps_relative_path(self):
        """Regression: Path.name dropped subdirs (out/x.md -> x.md)."""
        result, exists = self._run_rel_workspace(
            {"name": "rel", "goal": "g", "done_when": {"artifacts": ["out/x.md"]}},
            "out/x.md",
        )
        self.assertTrue(exists)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result.get("missing"), [], result)
        self.assertTrue(Path(result["artifacts"][0]).as_posix().endswith("out/x.md"), result)

    def test_live_json_status_success_uppercase(self):
        """agy print JSON uses status=SUCCESS (verified live)."""
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            be = self._backend(
                fake,
                {
                    "AGY_FAKE_ARTIFACT": "out.txt",
                    "AGY_FAKE_CONV": "conv_success",
                    "AGY_FAKE_STATUS": "SUCCESS",
                },
            )
            started = be.start_run(
                title="t",
                directory=td,
                instruction="go",
                artifacts=["out.txt"],
            )
            deadline = time.time() + 5
            while be.observe_run(started["run_id"]).get("busy") and time.time() < deadline:
                time.sleep(0.05)
            collected = be.collect_result(started["run_id"])
            self.assertTrue(collected["ok"], collected)
            self.assertEqual(collected["conversation_id"], "conv_success")

    def test_observe_maps_conversation_id(self):
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            be = self._backend(
                fake,
                {
                    "AGY_FAKE_ARTIFACT": "out.txt",
                    "AGY_FAKE_CONV": "conv_map_9",
                    "AGY_FAKE_STATUS": "ok",
                },
            )
            started = be.start_run(
                title="t",
                directory=td,
                instruction="go",
                artifacts=["out.txt"],
            )
            self.assertTrue(started["ok"])
            self.assertFalse(started["skip_permissions"])
            run_id = started["run_id"]
            deadline = time.time() + 5
            obs = be.observe_run(run_id)
            while obs.get("busy") and time.time() < deadline:
                time.sleep(0.05)
                obs = be.observe_run(run_id)
            self.assertFalse(obs["busy"], obs)
            self.assertTrue(obs["finish_successful"], obs)
            self.assertEqual(obs["conversation_id"], "conv_map_9")
            by_conv = be.observe_run("conv_map_9")
            self.assertEqual(by_conv["session_id"], "conv_map_9")
            collected = be.collect_result(run_id)
            self.assertTrue(collected["ok"], collected)
            self.assertEqual(collected["conversation_id"], "conv_map_9")
            self.assertFalse(collected["skip_permissions"])

    def test_permissions_fail_closed(self):
        be = AntigravityCliExecutionBackend(bin_path="agy")
        code, items = be.list_pending_actions()
        self.assertEqual(code, 200)
        self.assertEqual(items, [])
        code, body = be.reply_permission("per_x", "once")
        self.assertEqual(code, 501)
        self.assertEqual(body["status"], "unsupported")
        self.assertIsNone(body["reply"])
        self.assertFalse(body["ok"])

    def test_auto_approve_adds_skip_flag_but_reply_still_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            argv_path = str(Path(td) / "argv.json")
            be = self._backend(
                fake,
                {
                    "AGY_FAKE_ARGV": argv_path,
                    "AGY_FAKE_ARTIFACT": "a.txt",
                    "AGY_AUTO_APPROVE": "true",
                },
            )
            started = be.start_run(
                title="t",
                directory=td,
                instruction="go",
                artifacts=["a.txt"],
                charter={"agy_auto_approve": True},
            )
            self.assertTrue(started["skip_permissions"])
            deadline = time.time() + 5
            while be.observe_run(started["run_id"]).get("busy") and time.time() < deadline:
                time.sleep(0.05)
            argv = json.loads(Path(argv_path).read_text(encoding="utf-8"))
            self.assertIn(SKIP_PERMISSIONS_FLAG, argv)
            code, body = be.reply_permission("per_y", "once")
            self.assertEqual(code, 501)
            self.assertIsNone(body["reply"])

    def test_cancel_kills_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            be = self._backend(fake, {"AGY_FAKE_SLEEP": "30"})
            started = be.start_run(
                title="slow",
                directory=td,
                instruction="sleep",
                artifacts=[],
            )
            run_id = started["run_id"]
            obs = be.observe_run(run_id)
            self.assertTrue(obs["busy"], obs)
            code, body = be.cancel(run_id)
            self.assertEqual(code, 200, body)
            self.assertEqual(body["state"], "cancelled")
            obs2 = be.observe_run(run_id)
            self.assertTrue(obs2["cancelled"])
            self.assertFalse(obs2["busy"])
            collected = be.collect_result(run_id)
            self.assertFalse(collected["ok"])
            self.assertEqual(collected["state"], "cancelled")

    def test_missing_bin_failed_start(self):
        be = AntigravityCliExecutionBackend(bin_path=str(Path("/no/such/agy-bin-xyz")))
        with tempfile.TemporaryDirectory() as td:
            started = be.start_run(title="x", directory=td, instruction="hi", artifacts=["a.txt"])
            self.assertFalse(started["ok"])
            self.assertEqual(started["state"], "failed")
            obs = be.observe_run(started["run_id"])
            self.assertTrue(obs["errored"])
            self.assertFalse(obs["finish_successful"])


class TestRunJobCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_run_job()

    def _run(self, argv, env_extra=None):
        env = os.environ.copy()
        env.pop("COLLAB_EXECUTION_BACKEND", None)
        env.pop("AGY_AUTO_APPROVE", None)
        env.pop("COLLAB_AGY_AUTO_APPROVE", None)
        env.pop("COLLAB_AGY_ACCOUNT_POOL", None)
        if env_extra:
            env.update(env_extra)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                rc = self.mod.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_flag_antigravity_hello(self):
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                [
                    "--backend",
                    "antigravity",
                    "--workspace",
                    ws,
                    "--runs-dir",
                    runs,
                    str(HELLO),
                ],
                env_extra={
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                    "AGY_FAKE_CONV": "conv_cli",
                },
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(summary["ok"], summary)
            self.assertEqual(summary["backend"], "antigravity")
            self.assertEqual(summary.get("backend_id"), "antigravity.cli_v1")
            self.assertTrue((Path(ws) / "hello-from-worker.txt").is_file())
            status = json.loads(Path(summary["status"]).read_text(encoding="utf-8"))
            self.assertEqual(status.get("backend"), "antigravity.cli_v1")
            self.assertFalse(status.get("skip_permissions"))

    def test_flag_agy_alias(self):
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            ws = str(Path(td) / "ws")
            runs = str(Path(td) / "runs")
            rc, out, err = self._run(
                ["--backend", "agy", "--workspace", ws, "--runs-dir", runs, str(HELLO)],
                env_extra={
                    "AGY_BIN": str(fake),
                    "AGY_FAKE_ARTIFACT": "hello-from-worker.txt",
                },
            )
            self.assertEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertEqual(summary["backend"], "antigravity")


@unittest.skipUnless(_agy_live_ready(), "agy not on PATH")
@unittest.skipUnless(_agy_live_opt_in(), "set COLLAB_AGY_LIVE=1 for live smoke")
class TestLiveAgySmoke(unittest.TestCase):
    def test_print_json_no_skip_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(timeout_sec=120)
            started = be.start_run(
                title="live-pong",
                directory=td,
                instruction="Reply with exactly the word pong. Do not use tools. Do not write files.",
                artifacts=[],
            )
            self.assertFalse(started["skip_permissions"], started)
            if not started["ok"]:
                self.skipTest(f"agy spawn failed: {started}")
            deadline = time.time() + 120
            obs = be.observe_run(started["run_id"])
            while obs.get("busy") and time.time() < deadline:
                time.sleep(0.5)
                obs = be.observe_run(started["run_id"])
            collected = be.collect_result(started["run_id"])
            self.assertFalse(collected.get("skip_permissions"))
            self.assertTrue(
                collected.get("conversation_id") or obs.get("conversation_id"),
                collected,
            )
            # Live model output is not a contract; JSON shape / spawn is.
            self.assertIn(collected.get("finish"), ("stop", "error", "cancelled"))



class TestP1PipeAcceptanceTimeout(unittest.TestCase):
    """Codex P1: pipe drain, force_lead_review acceptance, observe timeout."""

    def test_large_stdout_does_not_deadlock(self):
        real_popen = __import__("subprocess").Popen
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}

        def factory(code):
            def spawn(argv, **kwargs):
                return real_popen([sys.executable, "-c", code], **kwargs)

            return spawn

        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(environ=env, timeout_sec=8, poll_sec=0.05)
            code = "import json; print(json.dumps({'status':'ok','response':'x'*200000}))"
            with patch("execution_backend.antigravity_cli_v1.subprocess.Popen", factory(code)):
                run = be.start_run(title="large", directory=td)
            deadline = time.time() + 5
            obs = be.observe_run(run["run_id"])
            while obs.get("busy") and time.time() < deadline:
                time.sleep(0.05)
                obs = be.observe_run(run["run_id"])
            self.assertFalse(obs.get("busy"), obs)
            collected = be.collect_result(run["run_id"])
            self.assertTrue(collected.get("ok"), collected)
            self.assertGreaterEqual(len(collected.get("response") or ""), 200000)

    def test_observe_enforces_timeout_sec(self):
        real_popen = __import__("subprocess").Popen
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}

        def factory(code):
            def spawn(argv, **kwargs):
                return real_popen([sys.executable, "-c", code], **kwargs)

            return spawn

        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(environ=env, timeout_sec=0.1, poll_sec=0.05)
            with patch(
                "execution_backend.antigravity_cli_v1.subprocess.Popen",
                factory("import time; time.sleep(30)"),
            ):
                run = be.start_run(title="deadline", directory=td)
            time.sleep(0.35)
            obs = be.observe_run(run["run_id"])
            self.assertFalse(obs.get("busy"), obs)
            self.assertTrue(obs.get("errored") or not obs.get("finish_successful"), obs)
            collected = be.collect_result(run["run_id"])
            self.assertFalse(collected.get("ok"))
            self.assertIn("timeout", (collected.get("error") or "").lower())

    def test_force_lead_review_wrong_content_not_ok(self):
        real_popen = __import__("subprocess").Popen
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}

        def factory(code):
            def spawn(argv, **kwargs):
                return real_popen([sys.executable, "-c", code], **kwargs)

            return spawn

        code = (
            "from pathlib import Path; import json; "
            "Path('answer.txt').write_text('WRONG'); "
            "print(json.dumps({'status':'ok','response':'done'}))"
        )
        charter = {
            "name": "review-probe",
            "goal": "Write answer.txt",
            "done_when": {"artifacts": ["answer.txt"]},
            "acceptance": "answer.txt must contain exactly RIGHT",
            "force_lead_review": True,
            "timeout_sec": 5,
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("execution_backend.antigravity_cli_v1.subprocess.Popen", factory(code)):
                result = run_antigravity_charter(charter=charter, workdir=root, environ=env)
            self.assertEqual((root / "answer.txt").read_text(encoding="utf-8"), "WRONG")
            self.assertFalse(result.get("ok"), result)
            self.assertNotEqual(result.get("state"), "ok")
            self.assertTrue(
                result.get("acceptance_failed")
                or "acceptance" in str(result.get("error") or "").lower(),
                result,
            )

    def test_exact_acceptance_right_content_ok(self):
        real_popen = __import__("subprocess").Popen
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}

        def factory(code):
            def spawn(argv, **kwargs):
                return real_popen([sys.executable, "-c", code], **kwargs)

            return spawn

        code = (
            "from pathlib import Path; import json; "
            "Path('answer.txt').write_text('RIGHT'); "
            "print(json.dumps({'status':'ok','response':'done'}))"
        )
        charter = {
            "name": "review-probe-ok",
            "goal": "Write answer.txt",
            "done_when": {"artifacts": ["answer.txt"]},
            "acceptance": "answer.txt must contain exactly RIGHT",
            "force_lead_review": True,
            "timeout_sec": 5,
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("execution_backend.antigravity_cli_v1.subprocess.Popen", factory(code)):
                result = run_antigravity_charter(charter=charter, workdir=root, environ=env)
            self.assertTrue(result.get("ok"), result)


class TestStreamDrainShortOutput(unittest.TestCase):
    def test_short_output_visible_while_child_alive(self):
        code = (
            "import sys, time\n"
            "sys.stdout.write('SHORT_MARKER\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(1.0)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(proc.stdout, chunks, done),
        )
        t.start()
        try:
            deadline = time.time() + 0.5
            while time.time() < deadline:
                if any("SHORT_MARKER" in c for c in chunks):
                    break
                time.sleep(0.02)
            self.assertTrue(proc.poll() is None, "child exited prematurely")
            self.assertTrue(
                any("SHORT_MARKER" in c for c in chunks),
                f"SHORT_MARKER not drained while child alive: chunks={chunks!r}",
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                proc.kill()
                proc.wait(timeout=2.0)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
            t.join(timeout=2.0)

    def test_short_stderr_visible_while_child_alive(self):
        code = (
            "import sys, time\n"
            "sys.stderr.write('SHORT_STDERR_MARKER\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(1.0)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(proc.stderr, chunks, done),
        )
        t.start()
        try:
            deadline = time.time() + 0.5
            while time.time() < deadline:
                if any("SHORT_STDERR_MARKER" in c for c in chunks):
                    break
                time.sleep(0.02)
            self.assertTrue(proc.poll() is None, "child exited prematurely")
            self.assertTrue(
                any("SHORT_STDERR_MARKER" in c for c in chunks),
                f"SHORT_STDERR_MARKER not drained while child alive: chunks={chunks!r}",
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                proc.kill()
                proc.wait(timeout=2.0)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
            t.join(timeout=2.0)

    def test_universal_newlines_crlf_and_cr(self):
        code = (
            "import sys\n"
            "sys.stdout.buffer.write(b'line1\\r\\nline2\\rline3\\n')\n"
            "sys.stdout.buffer.flush()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(proc.stdout, chunks, done),
        )
        t.start()
        try:
            done.wait(timeout=3.0)
            self.assertTrue(done.is_set(), "drainer did not signal natural done/EOF")
            combined = "".join(chunks)
            self.assertEqual(combined, "line1\nline2\nline3\n")
            self.assertNotIn("\r", combined)
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
            t.join(timeout=2.0)

    def test_universal_newlines_trailing_cr_at_eof(self):
        code = (
            "import sys\n"
            "sys.stdout.buffer.write(b'trailing_cr\\r')\n"
            "sys.stdout.buffer.flush()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(proc.stdout, chunks, done),
        )
        t.start()
        try:
            done.wait(timeout=3.0)
            self.assertTrue(done.is_set(), "drainer did not signal natural done/EOF")
            combined = "".join(chunks)
            self.assertEqual(combined, "trailing_cr\n")
            self.assertNotIn("\r", combined)
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
            t.join(timeout=2.0)

    def test_fake_stream_crlf_split_and_utf8_multibyte_split(self):
        class ChunkedRaw:
            def __init__(self, raw_chunks: list[bytes]):
                self._chunks = list(raw_chunks)
            def read1(self, n=65536):
                if not self._chunks:
                    return b""
                return self._chunks.pop(0)
            read = read1

        class FakeStreamWithBuffer:
            def __init__(self, raw_chunks: list[bytes]):
                self.buffer = ChunkedRaw(raw_chunks)

        # Split CRLF across chunks: b'part1\r', then b'\npart2'
        # Split UTF-8 multibyte across chunks: '你好' -> \xe4\xbd\xa0 \xe5\xa5\xbd
        raw_chunks = [
            b"part1\r",
            b"\npart2\r\n\xe4\xbd",
            b"\xa0\xe5",
            b"\xa5\xbd\r",
        ]
        stream = FakeStreamWithBuffer(raw_chunks)
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(stream, chunks, done),
        )
        t.start()
        try:
            done.wait(timeout=2.0)
            self.assertTrue(done.is_set(), "drainer did not set done event")
            combined = "".join(chunks)
            self.assertEqual(combined, "part1\npart2\n你好\n")
            self.assertNotIn("\r", combined)
        finally:
            t.join(timeout=2.0)

    def test_fake_stream_no_buffer_universal_newlines(self):
        class FakeStreamTextOnly:
            def __init__(self, text: str):
                self._sio = io.StringIO(text)
            def read(self, n=1):
                return self._sio.read(n)

        stream = FakeStreamTextOnly("alpha\r\nbeta\rgamma\r")
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(stream, chunks, done),
        )
        t.start()
        try:
            done.wait(timeout=2.0)
            self.assertTrue(done.is_set(), "drainer did not set done event")
            combined = "".join(chunks)
            self.assertEqual(combined, "alpha\nbeta\ngamma\n")
            self.assertNotIn("\r", combined)
        finally:
            t.join(timeout=2.0)

    def test_drain_stream_handles_read_error_and_closed_stream(self):
        class FailingRaw:
            def __init__(self):
                self.calls = 0
            def read1(self, n=65536):
                self.calls += 1
                if self.calls == 1:
                    return b"first_chunk\n"
                raise OSError("simulated broken pipe")
            read = read1

        class FailingStream:
            def __init__(self):
                self.buffer = FailingRaw()

        stream = FailingStream()
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(stream, chunks, done),
        )
        t.start()
        try:
            done.wait(timeout=2.0)
            self.assertTrue(done.is_set(), "done event must be set on exception")
            self.assertEqual("".join(chunks), "first_chunk\n")
        finally:
            t.join(timeout=2.0)


class TestStreamLifecycleCoverage(unittest.TestCase):
    """Deterministic closed-stream and error lifecycle coverage for _drain_stream."""

    def test_genuine_closed_textio_wrapper_raises_value_error(self):
        """Check (1): Genuine closed TextIOWrapper backed by BytesIO.

        Invoking drainer after close exercises ValueError, asserts done set and thread ended.
        """
        bio = io.BytesIO(b"prior_content\n")
        text_io = io.TextIOWrapper(bio, encoding="utf-8")
        text_io.close()
        self.assertTrue(text_io.closed)
        self.assertTrue(text_io.buffer.closed)

        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(text_io, chunks, done),
        )
        t.start()
        try:
            done.wait(timeout=2.0)
            self.assertTrue(done.is_set(), "done event must be set when closed stream raises ValueError")
            self.assertEqual(chunks, [], "no chunks expected from closed stream")
        finally:
            t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "drainer thread must not be alive")

    def test_raw_byte_pending_cr_then_oserror_and_valueerror_flushes_lf(self):
        """Check (2): Raw byte path pending CR then OSError/ValueError flushes LF."""
        class PendingCRRaw:
            def __init__(self, err_cls: type[Exception]):
                self.calls = 0
                self.err_cls = err_cls

            def read1(self, n=65536):
                self.calls += 1
                if self.calls == 1:
                    return b"first_line\r"
                raise self.err_cls("simulated read error after CR")

            read = read1

        class StreamWithRaw:
            def __init__(self, raw):
                self.buffer = raw

        for err_cls in (OSError, ValueError):
            with self.subTest(err_cls=err_cls):
                raw = PendingCRRaw(err_cls)
                stream = StreamWithRaw(raw)
                chunks: list[str] = []
                done = threading.Event()
                t = threading.Thread(
                    target=AntigravityCliExecutionBackend._drain_stream,
                    args=(stream, chunks, done),
                )
                t.start()
                try:
                    done.wait(timeout=2.0)
                    self.assertTrue(done.is_set(), f"done event must be set on {err_cls.__name__}")
                    combined = "".join(chunks)
                    self.assertEqual(
                        combined,
                        "first_line\n",
                        f"pending CR followed by {err_cls.__name__} must flush LF",
                    )
                    self.assertNotIn("\r", combined)
                finally:
                    t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"thread must not leak on {err_cls.__name__}")

    def test_incomplete_utf8_then_oserror_and_valueerror_flushes_replacement(self):
        """Check (3): Incomplete UTF-8 bytes then OSError/ValueError emits replacement char at final flush."""
        class IncompleteUtf8Raw:
            def __init__(self, err_cls: type[Exception]):
                self.calls = 0
                self.err_cls = err_cls

            def read1(self, n=65536):
                self.calls += 1
                if self.calls == 1:
                    # Incomplete 3-byte UTF-8 sequence prefix for '你' (\xe4\xbd\xa0)
                    return b"valid_prefix_\xe4\xbd"
                raise self.err_cls("simulated read error on incomplete utf8 tail")

            read = read1

        class StreamWithRaw:
            def __init__(self, raw):
                self.buffer = raw

        for err_cls in (OSError, ValueError):
            with self.subTest(err_cls=err_cls):
                raw = IncompleteUtf8Raw(err_cls)
                stream = StreamWithRaw(raw)
                chunks: list[str] = []
                done = threading.Event()
                t = threading.Thread(
                    target=AntigravityCliExecutionBackend._drain_stream,
                    args=(stream, chunks, done),
                )
                t.start()
                try:
                    done.wait(timeout=2.0)
                    self.assertTrue(done.is_set(), f"done event must be set on {err_cls.__name__}")
                    combined = "".join(chunks)
                    self.assertEqual(
                        combined,
                        "valid_prefix_\ufffd",
                        f"incomplete UTF-8 tail followed by {err_cls.__name__} must emit replacement char",
                    )
                finally:
                    t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"thread must not leak on {err_cls.__name__}")

    def test_text_only_fallback_read_valueerror_with_pending_cr_flushes_lf(self):
        """Check (4): Text-only fallback read ValueError/OSError with pending CR flushes LF."""
        class TextOnlyErrorStream:
            def __init__(self, text: str, err_cls: type[Exception]):
                self._chars = list(text)
                self.err_cls = err_cls

            def read(self, n=1):
                if self._chars:
                    return self._chars.pop(0)
                raise self.err_cls("simulated fallback error after CR")

        for err_cls in (ValueError, OSError):
            with self.subTest(err_cls=err_cls):
                stream = TextOnlyErrorStream("text_fallback\r", err_cls)
                chunks: list[str] = []
                done = threading.Event()
                t = threading.Thread(
                    target=AntigravityCliExecutionBackend._drain_stream,
                    args=(stream, chunks, done),
                )
                t.start()
                try:
                    done.wait(timeout=2.0)
                    self.assertTrue(done.is_set(), f"done event must be set on {err_cls.__name__}")
                    combined = "".join(chunks)
                    self.assertEqual(
                        combined,
                        "text_fallback\n",
                        f"text fallback pending CR followed by {err_cls.__name__} must flush LF",
                    )
                    self.assertNotIn("\r", combined)
                finally:
                    t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"thread must not leak on {err_cls.__name__}")

    def test_scripted_deterministic_close_error_during_read_path(self):
        """Check (5): Scripted deterministic close/error while read path in progress.

        Asserts truthful done and no leaked drainer thread.
        """
        class ScriptedBlockingRaw:
            def __init__(self):
                self.read_started = threading.Event()
                self.unblock = threading.Event()
                self.closed = False
                self.calls = 0

            def read1(self, n=65536):
                self.calls += 1
                if self.calls == 1:
                    return b"streaming_chunk_1\n"
                if self.closed:
                    raise ValueError("I/O operation on closed file.")
                self.read_started.set()
                self.unblock.wait(timeout=2.0)
                if self.closed:
                    raise ValueError("I/O operation on closed file.")
                return b""

            read = read1

            def close(self):
                self.closed = True
                self.unblock.set()

        class ScriptedStream:
            def __init__(self, raw):
                self.buffer = raw

            def close(self):
                self.buffer.close()

        raw = ScriptedBlockingRaw()
        stream = ScriptedStream(raw)
        chunks: list[str] = []
        done = threading.Event()
        t = threading.Thread(
            target=AntigravityCliExecutionBackend._drain_stream,
            args=(stream, chunks, done),
        )
        t.start()
        try:
            started = raw.read_started.wait(timeout=2.0)
            self.assertTrue(started, "drainer must enter read1 in child thread")
            stream.close()
            done.wait(timeout=2.0)
            self.assertTrue(done.is_set(), "done event must be set after close during read")
            self.assertEqual(chunks, ["streaming_chunk_1\n"])
        finally:
            raw.unblock.set()
            t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "drainer thread must have exited cleanly after close")


if __name__ == "__main__":
    unittest.main()
