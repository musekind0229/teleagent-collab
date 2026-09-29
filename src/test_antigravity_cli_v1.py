#!/usr/bin/env python3
"""antigravity.cli_v1 ExecutionBackend — simulated subprocess; live agy skippable."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
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



if __name__ == "__main__":
    unittest.main()
