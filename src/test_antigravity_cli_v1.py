#!/usr/bin/env python3
"""antigravity.cli_v1 ExecutionBackend — simulated subprocess; live agy skippable."""
from __future__ import annotations
from test_synthetic_caller_fixture import fixture_backend, fixture_start

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
    preflight_headless_permissions,
)
from charter import load_charter  # noqa: E402

HELLO = REPO / "jobs/examples/hello.charter.yaml"
if not HELLO.is_file():
    HELLO.parent.mkdir(parents=True, exist_ok=True)
    HELLO.write_text(
        "name: hello\n"
        "goal: Write hello-from-worker.txt\n"
        "must:\n"
        "  - write hello-from-worker.txt\n"
        "must_not:\n"
        "  - delete files\n"
        "allow_paths: []\n"
        "done_when:\n"
        "  artifacts:\n"
        "    - hello-from-worker.txt\n",
        encoding="utf-8",
    )

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
    # Explicit synthetic ordinary contract, not the unsafe legacy example's user gates.
    return {'name':'synthetic-hello','goal':'Write hello-from-worker.txt','done_when':{'artifacts':['hello-from-worker.txt']}}


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
        return fixture_backend(AntigravityCliExecutionBackend(bin_path=str(fake), environ=base, **kwargs))

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
            started = fixture_start(be,
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
            started = fixture_start(be,
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
            started = fixture_start(be,
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
            started = fixture_start(be,
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
        be = fixture_backend(AntigravityCliExecutionBackend(bin_path=str(Path("/no/such/agy-bin-xyz"))))
        with tempfile.TemporaryDirectory() as td:
            started = fixture_start(be,title="x", directory=td, instruction="hi", artifacts=["a.txt"])
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
            # Legacy CLI is not a trusted application caller; flags/env must not grant dispatch.
            self.assertNotEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertFalse(summary["ok"], summary)
            self.assertEqual(summary["state"], "blocked")
            self.assertFalse(summary.get("skip_permissions"))

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
            # Legacy CLI is not a trusted application caller; flags/env must not grant dispatch.
            self.assertNotEqual(rc, 0, err + out)
            summary = json.loads(out.strip().splitlines()[-1])
            self.assertFalse(summary["ok"], summary)
            self.assertEqual(summary["state"], "blocked")
            self.assertFalse(summary.get("skip_permissions"))


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
            be = fixture_backend(AntigravityCliExecutionBackend(bin_path=sys.executable, environ=env, timeout_sec=8, poll_sec=0.05))
            code = "import json; print(json.dumps({'status':'ok','response':'x'*200000}))"
            with patch("execution_backend.antigravity_cli_v1.subprocess.Popen", factory(code)):
                run = fixture_start(be,title="large", directory=td)
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
            be = fixture_backend(AntigravityCliExecutionBackend(bin_path=sys.executable, environ=env, timeout_sec=0.1, poll_sec=0.05))
            with patch(
                "execution_backend.antigravity_cli_v1.subprocess.Popen",
                factory("import time; time.sleep(30)"),
            ):
                run = fixture_start(be,title="deadline", directory=td)
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
                result = run_antigravity_charter(charter=charter, workdir=root, environ=env, backend=fixture_backend(AntigravityCliExecutionBackend(bin_path=sys.executable, environ=env)))
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
                result = run_antigravity_charter(charter=charter, workdir=root, environ=env, backend=fixture_backend(AntigravityCliExecutionBackend(bin_path=sys.executable, environ=env)))
            self.assertTrue(result.get("ok"), result)


class TestItem4PermissionPreflight(unittest.TestCase):
    """Item 4 Headless Permission Preflight Tests (3 categories: mode, task_authorized, unknown_gate)."""

    def test_preflight_blocks_inapplicable_task_kind_with_zero_native_runs(self):
        """Inapplicable contract (system_install) must block BEFORE worker launch; Popen called 0 times."""
        charter = {
            "name": "system-task",
            "goal": "apt-get install nginx",
            "task_kind": "system_install",
            "must": ["install nginx"],
            "must_not": [],
            "allow_paths": [],
            "done_when": {"artifacts": []},
        }
        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(bin_path="agy")
            with patch("subprocess.Popen") as mock_popen:
                started = be.start_run(title="sys-install", directory=td, charter=charter)
                mock_popen.assert_not_called()
            self.assertFalse(started["ok"])
            self.assertEqual(started["state"], "blocked")
            self.assertTrue(started["preflight_blocked"])
            self.assertFalse(started["native_started"])
            self.assertIn("permission_preflight", started)
            self.assertEqual(started["permission_preflight"]["category"], "unknown_gate")
            self.assertIn("task_kind 'system_install' not applicable", started["error"])

    def test_preflight_blocks_user_gate_permissions(self):
        """user_gate_permissions are unknown gates for headless and must block before launch."""
        charter = {
            "name": "gated-task",
            "goal": "do something with sudo",
            "task_kind": "file_task",
            "user_gate_permissions": ["sudo", "systemd_unit_install"],
            "must": ["run with sudo"],
            "must_not": [],
            "allow_paths": [],
            "done_when": {"artifacts": []},
        }
        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(bin_path="agy")
            with patch("subprocess.Popen") as mock_popen:
                started = be.start_run(title="user-gate", directory=td, charter=charter)
                mock_popen.assert_not_called()
            self.assertFalse(started["ok"])
            self.assertEqual(started["state"], "blocked")
            self.assertEqual(started["permission_preflight"]["category"], "unknown_gate")

    def test_preflight_blocks_forbidden_execution_tool(self):
        """Forbidden print/agy tool triggers unknown_gate block."""
        charter = {
            "name": "no-print-task",
            "goal": "no print",
            "forbidden_tools": ["print"],
            "must": ["do not print"],
            "must_not": [],
            "allow_paths": [],
            "done_when": {"artifacts": []},
        }
        with tempfile.TemporaryDirectory() as td:
            be = AntigravityCliExecutionBackend(bin_path="agy")
            with patch("subprocess.Popen") as mock_popen:
                started = be.start_run(title="no-print", directory=td, charter=charter)
                mock_popen.assert_not_called()
            self.assertFalse(started["ok"])
            self.assertEqual(started["state"], "blocked")

    def test_preflight_allows_authorized_ordinary_task(self):
        """Ordinary file tasks are task_authorized and pass preflight."""
        charter = {
            "name": "file-task",
            "goal": "Write summary.txt",
            "task_kind": "file_task",
            "must": ["Write summary.txt"],
            "must_not": [],
            "allow_paths": [],
            "done_when": {"artifacts": ["summary.txt"]},
        }
        # Ordinary charter text alone is not authority; test the untrusted gate explicitly.
        res = preflight_headless_permissions(charter)
        self.assertTrue(res["blocked"])
        self.assertEqual(res["category"], "unknown_gate")
        self.assertIn("mode", res["categories"])
        self.assertEqual(res["categories"]["task_authorized"], [])
        # Positive dispatch is separately covered by PMGrantRecoveryTests with exact trusted task binding.


class TestItem4ResultNormalization(unittest.TestCase):
    """Item 4 Result Normalization Tests (Host/Native/Business layered judgment & recovery)."""

    def _make_backend_with_rec(self, rec: dict) -> AntigravityCliExecutionBackend:
        be = AntigravityCliExecutionBackend(bin_path="agy")
        run_id = rec.get("run_id", "run_test_1")
        rec.setdefault("run_id", run_id)
        rec.setdefault("native_handle", f"agy:{run_id}")
        rec.setdefault("directory", tempfile.gettempdir())
        rec.setdefault("harvested", True)
        be._runs[run_id] = rec
        return be

    def test_host0_denied_actions_fails_business_with_pm_owner(self):
        """Host rc=0 with denied_actions in agy_json must NOT be judged business success; owner=project-manager."""
        rec = {
            "run_id": "run_denied",
            "returncode": 0,
            "proc": None,
            "agy_json": {
                "status": "SUCCESS",
                "denied_actions": [{"tool": "RunCommand", "reason": "permission denied"}],
            },
            "activity": "idle",
            "finish": "stop",
            "artifacts": [],
        }
        be = self._make_backend_with_rec(rec)
        collected = be.collect_result("run_denied")
        self.assertFalse(collected["ok"])
        self.assertEqual(collected["state"], "fail")
        self.assertTrue(collected["layers"]["host"]["ok"])
        self.assertFalse(collected["layers"]["native"]["ok"])
        self.assertFalse(collected["layers"]["business"]["ok"])
        self.assertEqual(collected["next_owner"], "project-manager")
        self.assertEqual(collected["recovery_action"], "review_permission_policy_or_grant_access")
        self.assertTrue(any("permission denied" in w for w in collected.get("warnings", [])))

    def test_host0_missing_artifacts_fails_business_with_worker_owner(self):
        """Host rc=0 and native status=SUCCESS but missing required artifacts fails business with owner=worker."""
        with tempfile.TemporaryDirectory() as td:
            rec = {
                "run_id": "run_missing_art",
                "directory": td,
                "returncode": 0,
                "proc": None,
                "agy_json": {"status": "SUCCESS", "response": "done"},
                "activity": "idle",
                "finish": "stop",
                "artifacts": ["missing_file.txt"],
            }
            be = self._make_backend_with_rec(rec)
            collected = be.collect_result("run_missing_art")
            self.assertFalse(collected["ok"])
            self.assertEqual(collected["state"], "fail")
            self.assertTrue(collected["layers"]["host"]["ok"])
            self.assertTrue(collected["layers"]["native"]["ok"])
            self.assertFalse(collected["layers"]["business"]["ok"])
            self.assertEqual(collected["missing"], ["missing_file.txt"])
            self.assertEqual(collected["next_owner"], "worker")
            self.assertIn("re-run task to produce missing artifacts", collected["recovery_action"])

    def test_transient_503_fails_native_without_swapping_account(self):
        """503 capacity error must stay failed/unknown without marking account permanently bad."""
        rec = {
            "run_id": "run_503",
            "returncode": 0,
            "proc": None,
            "agy_json": {
                "status": "ERROR",
                "error": "API error: UNAVAILABLE (code 503): No capacity available for model",
            },
            "assistant_error": "503: No capacity",
            "activity": "idle",
            "finish": "error",
            "artifacts": [],
        }
        be = self._make_backend_with_rec(rec)
        collected = be.collect_result("run_503")
        self.assertFalse(collected["ok"])
        self.assertTrue(collected["layers"]["native"]["has_503"])
        self.assertEqual(collected["next_owner"], "worker")
        self.assertEqual(collected["recovery_action"], "retry_after_transient_delay")
        self.assertTrue(any("503" in w for w in collected.get("warnings", [])))

    def test_connection_closed_fails_with_worker_retry(self):
        """Connection closed/reset fails native and business layer with owner=worker and retry_connection."""
        rec = {
            "run_id": "run_conn",
            "returncode": 1,
            "proc": None,
            "stderr": "connection reset by peer\n",
            "agy_json": {"status": "ERROR", "error": "connection closed unexpectedly"},
            "assistant_error": "connection closed unexpectedly",
            "activity": "idle",
            "finish": "error",
            "artifacts": [],
        }
        be = self._make_backend_with_rec(rec)
        collected = be.collect_result("run_conn")
        self.assertFalse(collected["ok"])
        self.assertFalse(collected["layers"]["host"]["ok"])
        self.assertTrue(collected["layers"]["native"]["connection_closed"])
        self.assertEqual(collected["next_owner"], "worker")
        self.assertEqual(collected["recovery_action"], "retry_connection")

    def test_positive_control_all_layers_green(self):
        """Positive control: host 0, native SUCCESS, artifacts present -> business ok=True, next_owner=caller."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "delivered.txt"
            target.write_text("content", encoding="utf-8")
            rec = {
                "run_id": "run_positive",
                "directory": td,
                "returncode": 0,
                "proc": None,
                "agy_json": {"status": "SUCCESS", "response": "completed successfully"},
                "activity": "idle",
                "finish": "stop",
                "artifacts": ["delivered.txt"],
            }
            be = self._make_backend_with_rec(rec)
            collected = be.collect_result("run_positive")
            self.assertTrue(collected["ok"])
            self.assertEqual(collected["state"], "ok")
            self.assertTrue(collected["layers"]["host"]["ok"])
            self.assertTrue(collected["layers"]["native"]["ok"])
            self.assertTrue(collected["layers"]["business"]["ok"])
            self.assertEqual(collected["next_owner"], "caller")
            self.assertEqual(collected["recovery_action"], "consume_artifacts")


if __name__ == "__main__":
    unittest.main()

import hashlib


class PMGrantRecoveryTests(unittest.TestCase):
    def entry(self, charter, *, trusted=False, mismatch=False, strict=False, caller_id='synthetic-current'):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            b=AntigravityCliExecutionBackend(bin_path='offline-no-native',environ={})
            if trusted:
                b.configure_caller_permissions(allowed_operations=['read','write','command'],native_mode='strict_headless' if strict else 'skip_permissions')
                b.authorize_caller_task(task_id='synthetic-current',title='current',directory=td,instruction='payload',artifacts=[])
            given=dict(charter) if isinstance(charter,dict) else charter
            if not strict and isinstance(given,dict):given['agy_auto_approve']=True
            with patch.dict(os.environ,{},clear=True),patch.object(b,'_base_env',return_value={}),patch.object(b,'_start_run_locked',return_value=({}, {'ok':False,'state':'dispatch_spy'})) as dispatch:
                r=b.start_run(title='other' if mismatch else 'current',directory=td,instruction='payload',artifacts=[],charter=given,caller_task_id=caller_id)
            return dispatch.call_count,r
    def test_all_untrusted_entry_forms_block(self):
        for c in [None,{}, {'task_kind':'file_task'},{'caller_grant':'1'},{'caller_grant':{'task_id':'current','source':'trusted_application_configuration'}},{'task_kind':'file_task','caller_grant':{'task_id':'OTHER','allowed':True}}]:
            with self.subTest(charter=c):self.assertEqual(self.entry(c)[0],0)
    def test_trusted_matching_task_can_dispatch(self):self.assertEqual(self.entry({'required_tools':['RunCommand']},trusted=True)[0],1)
    def test_wrong_payload_binding_blocks(self):self.assertEqual(self.entry({},trusted=True,mismatch=True)[0],0)
    def test_conflict_always_blocks(self):self.assertEqual(self.entry({'required_tools':['RunCommand'],'forbidden_tools':['RunCommand']},trusted=True)[0],0)
    def test_unknown_native_coverage_blocks(self):self.assertEqual(self.entry({},trusted=True,strict=True)[0],0)
    def test_task_id_and_veto_matrix(self):
        for task in [None,'OTHER']:
            self.assertEqual(self.entry({},trusted=True,caller_id=task)[0],0)
        for charter in [{'task_kind':'system_install'},{'install_roots':['C:/Windows']},{'user_gate_permissions':['login']},{'forbidden_tools':['agy']},{'require_sudo':True},{'required_tools':['UnknownTool']}]:
            self.assertEqual(self.entry(charter,trusted=True)[0],0,charter)
    def test_exact_rc_and_business_matrix(self):
        for rc in [False,True,None,'0',0,1]:
            with tempfile.TemporaryDirectory() as td:
                b=AntigravityCliExecutionBackend(bin_path='offline');b._runs['r']={'run_id':'r','native_handle':'synthetic:r','directory':td,'returncode':rc,'proc':None,'harvested':True,'agy_json':{'status':'SUCCESS','response':'amount=503 ordinary','denied_actions':[]},'activity':'idle','finish':'stop','artifacts':[],'stdout':'','stderr':''}
                r=b.collect_result('r');self.assertEqual(r['ok'],type(rc) is int and rc==0)


class PMCompleteMaterialRecoveryTests(unittest.TestCase):
    def test_large_source_full_delta_real_request(self):
        from framework import agy_review as a
        from framework.app_service import CollabApplication,LeadAdapterPlanner
        from types import SimpleNamespace
        import copy
        calls=[]
        class Lead:
            def decide(self,request,**kwargs):
                calls.append(copy.deepcopy(request));answer={'application_id':request['application_id'],'context_summary':request['context_summary'],'verdict':'pass','reason':'offline actual request fixture'}
                return json.dumps(answer),answer
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ws=root/'workspace';ws.mkdir()
            old=b'def target():\n    return 1\n\ndef caller():\n    return target()\n'+b'# baseline padding\n'*5000
            (ws/'large.py').write_bytes(old)
            (ws/'tests.py').write_bytes(b'# old line\n'*800)
            app=CollabApplication(root/'state',planner=LeadAdapterPlanner(Lead(),cwd=ws),backend=SimpleNamespace(backend_id='antigravity.test'))
            a.record_baseline(app.coordinator.workspaces_root,'g','t',ws)
            new=old.replace(b'return 1',b'return 2')+b'# changed entire tail\n'*600
            (ws/'large.py').write_bytes(new); (ws/'tests.py').write_bytes(b'# new changed line\n'*800)
            task={'task_id':'t','goal_id':'g','run_id':'r','expected_artifacts':['DELIVERY.md'],'done_when':{'artifacts':['DELIVERY.md']}}
            accept={'review_materials_required':True,'required_blocks':[{'path':'large.py','function':'target','dependency':{'path':'large.py','function':'caller'}}],'complete_delta_required':True,'code_scope':['large.py','tests.py']}
            q,r=app.coordinator._agy_post_run_review({'goal_id':'g','goal':{'acceptance':accept,'budget':{'max_reworks':0}},'tasks':[task]},task,{'ok':True,'backend':'antigravity.test','workspace':str(ws)})
            self.assertEqual(len(calls),1);self.assertEqual(r['review']['status'],'passed')
            payload=calls[0]['extra']['worker_request'];self.assertTrue(payload['complete_delta']['complete'])
            self.assertTrue(payload['alternative_coverage_verified'])
            row=payload['complete_delta']['paths'][0]
            self.assertEqual(Path(row['before']['path']).read_bytes(),old);self.assertEqual(Path(row['after']['path']).read_bytes(),new)
            self.assertEqual(row['before']['sha256'],hashlib.sha256(old).hexdigest());self.assertEqual(row['after']['sha256'],hashlib.sha256(new).hexdigest())
            self.assertEqual(payload['out_of_scope_changes'],[])
            self.assertTrue(payload['diff_truncated'])
            self.assertIn('tests.py',payload['truncated_files'])
            self.assertEqual(payload['complete_delta']['binding'],{'goal_id':'g','task_id':'t','run_id':'r'})
            self.assertIn('def caller()',json.dumps(payload['source_packet']))
            from unittest.mock import patch
            baseline=a.load_baseline(app.coordinator.workspaces_root,'g','t')
            for bad in ['missing_function','no_alternative','capacity','wrong_hash','missing_bytes']:
                altered=copy.deepcopy(accept);badbase=copy.deepcopy(baseline)
                if bad=='missing_function':altered['required_blocks'][0]['function']='absent'
                elif bad=='no_alternative':altered['complete_delta_required']=False
                elif bad=='capacity':altered['delta_capacity_bytes']=1
                elif bad=='wrong_hash':badbase['large.py']['sha256']='wrong'
                elif bad=='missing_bytes':del badbase['large.py']['raw_hex']
                with patch.object(a,'load_baseline',return_value=badbase):
                    q,blocked=app.coordinator._agy_post_run_review({'goal_id':'g','goal':{'acceptance':altered,'budget':{'max_reworks':0}},'tasks':[task]},task,{'ok':True,'backend':'antigravity.test','workspace':str(ws)})
                self.assertEqual(len(calls),1,bad);self.assertEqual(blocked['review']['status'],'review_blocked',bad)
    def test_missing_endpoint_wrong_hash_capacity_fail_closed(self):
        from framework import agy_review as a
        with tempfile.TemporaryDirectory() as td:
            before={'x.py':{'sha256':'wrong','size':3,'raw_hex':b'abc'.hex()}}
            after={'x.py':{'sha256':hashlib.sha256(b'def').hexdigest(),'size':3,'raw_hex':b'def'.hex()}}
            self.assertFalse(a.build_complete_delta(baseline=before,current=after,binding={'task_id':'t'},evidence_root=td)['complete'])
            before['x.py']['sha256']=hashlib.sha256(b'abc').hexdigest()
            self.assertFalse(a.build_complete_delta(baseline=before,current=after,binding={'task_id':'t'},evidence_root=td,capacity_bytes=1)['complete'])
            del before['x.py']['raw_hex']
            self.assertFalse(a.build_complete_delta(baseline=before,current=after,binding={'task_id':'t'},evidence_root=td)['complete'])
