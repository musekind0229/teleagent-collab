"""Tests for GUI model reuse (fake tokens only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from teleagent_adapter.windows_gui_model_reuse import (
    AUTH_STATE_ENV,
    OPENCODE_CONFIG_CONTENT_ENV,
    GuiModelAuthError,
    build_opencode_config_content,
    encrypt_auth_state,
    gui_auth_sources_present,
    load_gui_auth_material,
    prepare_gui_model_reuse,
    reuse_gui_model_mode,
)
from teleagent_adapter.windows_process_environ import CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING


class EncryptTests(unittest.TestCase):
    def test_encrypt_shape(self) -> None:
        enc = encrypt_auth_state(
            {"token": "fake-token-" + ("x" * 40), "deviceId": "dev", "installId": "inst"},
            "session-key-for-unit-test",
        )
        self.assertEqual(enc["version"], "v1")
        for k in ("iv", "tag", "ciphertext"):
            self.assertIsInstance(enc[k], str)
            self.assertGreater(len(enc[k]), 4)
            self.assertNotIn("=", enc[k])

    def test_config_content_has_newapi(self) -> None:
        cfg = json.loads(build_opencode_config_content())
        self.assertIn("NewApi", cfg["provider"])
        self.assertIn("baseURL", cfg["provider"]["NewApi"]["options"])


class ReuseModeTests(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "0"}), "off")
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "1"}), "on")
        self.assertEqual(reuse_gui_model_mode({}), "auto")


class PrepareTests(unittest.TestCase):
    def test_off_returns_none(self) -> None:
        env = {"TELEAGENT_WIN_REUSE_GUI_MODEL": "0"}
        self.assertIsNone(prepare_gui_model_reuse("k", environ=env))

    def test_auto_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "auto",
                "TELEAGENT_GUI_LEVELDB_DIR": str(Path(td) / "missing"),
                "TELEAGENT_GUI_DEVICE_META": str(Path(td) / "no-meta.json"),
            }
            self.assertFalse(gui_auth_sources_present(env))
            self.assertIsNone(prepare_gui_model_reuse("k", environ=env))

    def test_on_missing_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "1",
                "TELEAGENT_GUI_LEVELDB_DIR": str(Path(td) / "missing"),
                "TELEAGENT_GUI_DEVICE_META": str(Path(td) / "no-meta.json"),
            }
            with self.assertRaises(GuiModelAuthError) as cm:
                prepare_gui_model_reuse("k", environ=env)
            self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING)

    def test_prepare_with_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ldb = Path(td) / "leveldb"
            ldb.mkdir()
            token = "Prefix:" + ("a" * 80)
            blob = b"opencowork-auth\x00" + json.dumps(
                {"state": {"token": token}, "version": 0}
            ).encode()
            (ldb / "000001.ldb").write_bytes(blob)
            meta = Path(td) / "device-meta.json"
            meta.write_text(
                json.dumps({"deviceId": "device-test", "installId": "install-test"}),
                encoding="utf-8",
            )
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "1",
                "TELEAGENT_GUI_LEVELDB_DIR": str(ldb),
                "TELEAGENT_GUI_DEVICE_META": str(meta),
            }
            self.assertTrue(gui_auth_sources_present(env))
            mat = load_gui_auth_material(environ=env)
            self.assertEqual(mat.token, token)
            extras = prepare_gui_model_reuse("unit-session", environ=env)
            assert extras is not None
            self.assertIn(AUTH_STATE_ENV, extras)
            self.assertIn(OPENCODE_CONFIG_CONTENT_ENV, extras)
            self.assertNotIn(token, extras[AUTH_STATE_ENV])


class PortWaitTests(unittest.TestCase):
    def test_wait_port_free_hook(self) -> None:
        from teleagent_adapter.windows_stdin_wrap import _wait_port_free

        calls = {"n": 0}

        def listening(_port: int) -> bool:
            calls["n"] += 1
            return calls["n"] < 3

        sleeps: list[float] = []
        _wait_port_free(4401, timeout=2.0, sleep_fn=sleeps.append, listening_fn=listening)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertTrue(sleeps)


if __name__ == "__main__":
    unittest.main()
