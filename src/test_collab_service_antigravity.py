"""collab-service --backend antigravity wiring (no live agy)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BIN = ROOT / "bin"
for p in (str(ROOT), str(SRC), str(BIN)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_collab_service():
    path = BIN / "collab-service.py"
    spec = importlib.util.spec_from_file_location("collab_service_mod", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class CollabServiceAntigravityBackendTests(unittest.TestCase):
    def test_argparse_accepts_antigravity_choices(self):
        mod = _load_collab_service()
        parser_ns = None

        def fake_parse(argv=None):
            nonlocal parser_ns
            # Re-create a minimal Namespace by calling the real parser builder path
            # via main would start a server; instead inspect ArgumentParser choices.
            import argparse

            p = argparse.ArgumentParser()
            # Mirror production choices for this regression guard.
            p.add_argument(
                "--backend",
                choices=("inprocess", "teleagent-windows", "antigravity", "agy"),
            )
            ns = p.parse_args(["--backend", "antigravity"])
            parser_ns = ns
            return ns

        # Prefer reading the live module's main parser by invoking --help path.
        # Direct factory tests below cover wiring; here assert choices string present.
        src = (BIN / "collab-service.py").read_text(encoding="utf-8")
        self.assertIn('"antigravity"', src)
        self.assertIn('"agy"', src)
        self.assertIn("--agy-account-pool", src)
        self.assertIn("reply_permission is 501", src)

    def test_backend_factory_returns_agy_cli_without_pool(self):
        mod = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            be = mod._backend("antigravity", Path(td))
        self.assertEqual(be.backend_id, "antigravity.cli_v1")
        code, body = be.reply_permission("req-1", "once")
        self.assertEqual(code, 501)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("status"), "unsupported")

    def test_agy_alias_same_factory(self):
        mod = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            be = mod._backend("agy", Path(td))
        self.assertEqual(be.backend_id, "antigravity.cli_v1")

    def test_empty_pool_fails_closed_at_service_wiring(self):
        mod = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            pool = Path(td) / "pool.json"
            pool.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": "a",
                                "home": str(Path(td) / "homeA"),
                                "state": "unavailable",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                mod._backend("antigravity", Path(td), agy_account_pool=str(pool))
            msg = str(ctx.exception).lower()
            self.assertIn("account pool unavailable", msg)

    def test_unknown_run_observe_is_not_silent(self):
        mod = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            be = mod._backend("antigravity", Path(td))
        from execution_backend.base import BackendError

        with self.assertRaises(BackendError):
            be.observe_run("agy_missing_handle")

    def test_service_once_wires_antigravity_backend_id(self):
        mod = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            be = MagicMock()
            be.backend_id = "antigravity.cli_v1"
            be.close = MagicMock()
            with patch.object(mod, "_backend", return_value=be) as be_factory:
                with patch.object(mod, "CollabApplication") as app_cls:
                    app = app_cls.return_value
                    app.coordinator.backend = be
                    app.coordinator.process_all.return_value = []
                    rc = mod.main(
                        [
                            "--backend",
                            "antigravity",
                            "--agy-account-pool",
                            str(Path(td) / "pool.json"),
                            "--once",
                            "--persist",
                            td,
                        ]
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(be_factory.call_args.kwargs.get("agy_account_pool"), str(Path(td) / "pool.json"))
            self.assertEqual(be_factory.call_args.args[0], "antigravity")
            be.close.assert_called()

    def test_backend_with_pool_defers_home_pin_until_start_run(self):
        """Service wiring must not reserve/pin HOME; start_run does per-dispatch."""
        mod = _load_collab_service()
        with tempfile.TemporaryDirectory() as td:
            pool = Path(td) / "pool.json"
            home_a = Path(td) / "homeA"
            home_c = Path(td) / "homeC"
            home_a.mkdir()
            home_c.mkdir()
            pool.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {"id": "A", "home": str(home_a), "state": "available"},
                            {"id": "C", "home": str(home_c), "state": "available"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            be = mod._backend("antigravity", Path(td), agy_account_pool=str(pool))
            self.assertEqual(be.backend_id, "antigravity.cli_v1")
            self.assertEqual(be._account_pool_path, str(pool))
            # Not pinned at construction.
            self.assertNotEqual(str(dict(be._env()).get("HOME") or ""), str(home_a))
            self.assertNotEqual(dict(be._env()).get("AGY_PROFILE"), "A")
            src = (BIN / "collab-service.py").read_text(encoding="utf-8")
            self.assertIn("per-dispatch", src)
            self.assertNotIn("one HOME pinned for this service lifetime", src)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
