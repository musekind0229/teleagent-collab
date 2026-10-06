#!/usr/bin/env python3
"""Per-Goal agy model selection (Goal ``model`` > env AGY_MODEL > built-in default).

RED on 23cc991:
- charter ``agy_model`` was shadowed by ``resolve_agy_model(explicit=self.model)``
  (the model was pinned when the backend was built), so a per-Goal model never
  reached ``agy --model=``;
- the Application API ignored ``model`` (no 400 for an unknown name);
- ``hermes-collab-request.py open`` had no ``--model``.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_backend.antigravity_cli_v1 import (  # noqa: E402
    DEFAULT_AGY_MODEL,
    AntigravityCliExecutionBackend,
    resolve_agy_model,
)
from execution_backend.inprocess_v1 import InProcessExecutionBackend  # noqa: E402
from framework.app_service import AppError, CollabApplication, worker_charter_for_task  # noqa: E402
from test_contract_render import _write_fake_agy  # noqa: E402

REPO = SRC.parent
AGY_MODELS_SAMPLE = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGY_", "COLLAB_AGY_"))}
    env.update(extra)
    return env


def _wait_done(be: AntigravityCliExecutionBackend, run_id: str, sec: float = 10.0) -> dict:
    deadline = time.time() + sec
    obs = be.observe_run(run_id)
    while obs.get("busy") and time.time() < deadline:
        time.sleep(0.05)
        obs = be.observe_run(run_id)
    return be.collect_result(run_id)


def _goal(**overrides) -> dict:
    body = {
        "idempotency_key": "model-1",
        "client_id": "test-suite",
        "title": "model probe",
        "goal": "Write delivery.txt inside the assigned workspace.",
        "boundaries": {"must": ["Stay inside the assigned workspace"], "must_not": ["No network"]},
        "acceptance": {"artifacts": ["delivery.txt"], "text": "delivery.txt exists"},
        "budget": {"wall_sec": 30, "max_reworks": 0},
    }
    body.update(overrides)
    return body


class ModelPrecedenceTests(unittest.TestCase):
    """per-Goal (charter) > env AGY_MODEL > built-in default."""

    def test_resolve_prefers_charter_over_env(self) -> None:
        got = resolve_agy_model(
            environ={"AGY_MODEL": "gemini-3.8-flash-high"},
            charter={"agy_model": "claude-opus-4-6-thinking"},
        )
        self.assertEqual(got, "claude-opus-4-6-thinking")

    def _spawned_model(self, env: dict[str, str], charter: dict | None) -> tuple[str, dict]:
        with tempfile.TemporaryDirectory() as td:
            fake = _write_fake_agy(td)
            argv_file = Path(td) / "argv.json"
            env = dict(env, AGY_FAKE_ARGV=str(argv_file), AGY_FAKE_ARTIFACT="delivery.txt")
            be = AntigravityCliExecutionBackend(bin_path=str(fake), environ=env, timeout_sec=10, poll_sec=0.05)
            work = Path(td) / "w"
            work.mkdir()
            run = be.start_run(title="m", directory=str(work), charter=charter)
            self.assertTrue(run.get("ok"), run)
            collected = _wait_done(be, run["run_id"])
            argv = json.loads(argv_file.read_text(encoding="utf-8"))
            models = [a.split("=", 1)[1] for a in argv if a.startswith("--model=")]
            self.assertEqual(len(models), 1, argv)
            return models[0], collected

    def test_charter_model_reaches_spawn(self) -> None:
        model, collected = self._spawned_model(
            _clean_env(AGY_MODEL="gemini-3.8-flash-high"),
            {"goal": "write delivery.txt", "agy_model": "claude-opus-4-6-thinking"},
        )
        self.assertEqual(model, "claude-opus-4-6-thinking")
        self.assertEqual(collected.get("model"), "claude-opus-4-6-thinking")

    def test_env_model_without_charter(self) -> None:
        model, collected = self._spawned_model(
            _clean_env(AGY_MODEL="gemini-3.8-flash-high"), {"goal": "write delivery.txt"}
        )
        self.assertEqual(model, "gemini-3.8-flash-high")
        self.assertEqual(collected.get("model"), "gemini-3.8-flash-high")

    def test_builtin_default_without_env_or_charter(self) -> None:
        model, _ = self._spawned_model(_clean_env(), None)
        self.assertEqual(model, DEFAULT_AGY_MODEL)

    def test_worker_charter_carries_goal_model(self) -> None:
        charter = worker_charter_for_task(
            goal={"model": "claude-opus-4-6-thinking", "boundaries": {}, "budget": {}},
            task={"title": "t", "inputs": {"instruction": "do"}},
        )
        self.assertEqual(charter.get("agy_model"), "claude-opus-4-6-thinking")


class ModelCatalogTests(unittest.TestCase):
    def test_parse_agy_models_output(self) -> None:
        from execution_backend.agy_models import parse_agy_models_output

        self.assertEqual(
            parse_agy_models_output(AGY_MODELS_SAMPLE),
            ["gemini-3.8-flash-high", "gemini-3.8-flash-low", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"],
        )
        self.assertEqual(parse_agy_models_output("Please sign in to continue\n"), [])

    def test_catalog_falls_back_to_builtin_and_keeps_last_good(self) -> None:
        from execution_backend.agy_models import BUILTIN_AGY_MODELS, AgyModelCatalog

        answers = [None]
        cat = AgyModelCatalog(probe=lambda: answers[0], ttl_sec=0.0)
        known, source = cat.known()
        self.assertEqual(source, "builtin")
        self.assertIn("gemini-3.8-flash-high", known)
        self.assertEqual(list(known), list(BUILTIN_AGY_MODELS))
        answers[0] = ["gemini-3.8-flash-high", "brand-new-model"]
        self.assertTrue(cat.refresh_now())
        self.assertEqual(cat.known(), (["gemini-3.8-flash-high", "brand-new-model"], "agy_models"))
        answers[0] = None  # agy models now failing: keep the last good list
        self.assertFalse(cat.refresh_now())
        self.assertEqual(cat.known()[0], ["gemini-3.8-flash-high", "brand-new-model"])

    def test_catalog_does_not_probe_unless_enabled(self) -> None:
        from execution_backend.agy_models import AgyModelCatalog

        calls = []
        cat = AgyModelCatalog(probe=lambda: calls.append(1) or ["x"])
        cat.known()
        time.sleep(0.05)
        self.assertEqual(calls, [])

    def test_backend_capabilities_list_models(self) -> None:
        be = AntigravityCliExecutionBackend(bin_path="agy-test", environ=_clean_env(AGY_MODEL="gemini-3.8-flash-high"))
        models = be.capabilities().get("models")
        self.assertIsInstance(models, dict)
        self.assertTrue(models["selectable"])
        self.assertEqual(models["default"], "gemini-3.8-flash-high")
        self.assertIn("claude-opus-4-6-thinking", models["known"])
        self.assertIn(models["source"], {"builtin", "agy_models"})


class ModelSubmitTests(unittest.TestCase):
    def _agy_app(self, root: Path) -> CollabApplication:
        be = AntigravityCliExecutionBackend(bin_path="agy-test", environ=_clean_env(), timeout_sec=5)
        return CollabApplication(root / "app", backend=be)

    def test_unknown_model_is_400_with_known_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = self._agy_app(Path(td))
            with self.assertRaises(AppError) as ctx:
                app.submit(_goal(model="claude-opus-5-5"))
            err = ctx.exception
            self.assertEqual(err.status, 400)
            self.assertEqual(err.code, "unknown_model")
            self.assertIn("claude-opus-5-5", str(err))
            self.assertIn("claude-opus-4-6-thinking", err.extra.get("known_models") or [])
            self.assertEqual(app.list_requests()["requests"], [])

    def test_known_model_is_stored_on_goal_and_charter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = self._agy_app(Path(td))
            out = app.submit(_goal(model="claude-opus-4-6-thinking"))
            self.assertTrue(out["ok"], out)
            goal = app.status(out["request_id"])["goal"]
            self.assertEqual(goal.get("model"), "claude-opus-4-6-thinking")

    def test_model_must_be_a_plain_string(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = self._agy_app(Path(td))
            for bad in (5, ["x"], "", "bad model name", "x" * 200):
                with self.assertRaises(AppError) as ctx:
                    app.submit(_goal(idempotency_key=f"bad-{bad!r}"[:60], model=bad))
                self.assertEqual(ctx.exception.status, 400, bad)

    def test_backend_without_model_selection_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(Path(td) / "app", backend=InProcessExecutionBackend())
            with self.assertRaises(AppError) as ctx:
                app.submit(_goal(model="gemini-3.8-flash-high"))
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, "capability_unavailable")
            self.assertIn("model_selection", ctx.exception.extra.get("missing") or [])


def _load_client():
    spec = importlib.util.spec_from_file_location("hermes_collab_request_model", REPO / "bin" / "hermes-collab-request.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ClientModelFlagTests(unittest.TestCase):
    def test_open_model_flag_reaches_body(self) -> None:
        hcr = _load_client()
        captured: dict = {}

        class _Resp:
            status = 202

            def read(self):
                return json.dumps({"ok": True, "request_id": "g", "state": "queued"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        with tempfile.TemporaryDirectory() as td:
            env = {
                "COLLAB_API_BASE": "http://127.0.0.1:8765",
                "COLLAB_API_TOKEN": "test-token",
                "COLLAB_ENV_FILE": str(Path(td) / "missing.env"),
                "COLLAB_JSON_UNICODE": "",
                "COLLAB_OUTPUT_FULL": "",
            }
            buf = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with mock.patch("sys.stdout", buf):
                        code = hcr.main(["open", "--goal", "write it", "--model", "claude-opus-4-6-thinking"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(captured["body"]["model"], "claude-opus-4-6-thinking")


if __name__ == "__main__":
    unittest.main()
