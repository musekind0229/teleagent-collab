#!/usr/bin/env python3
"""SIMULATED tests for GUI model reuse (fake token/device only).

Never real secrets. Does not exec TeleAgent.exe or read the live GUI leveldb
unless a test explicitly points TELEAGENT_GUI_* at a temp fixture.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from teleagent_adapter.windows_gui_model_reuse import (  # noqa: E402
    AUTH_STATE_ENV,
    NEWAPI_BASE_URL,
    NEWAPI_SMALL_MODEL,
    OPENCODE_CONFIG_CONTENT_ENV,
    OPENCODE_CONFIG_DIR_ENV,
    TELEAGENT_CONFIG_DIR_ENV,
    GuiAuthMaterial,
    GuiModelAuthError,
    build_opencode_config_content,
    encrypt_auth_state,
    gui_auth_sources_present,
    load_device_meta,
    load_gui_auth_material,
    load_gui_token_from_leveldb,
    prepare_gui_model_reuse,
    reuse_gui_model_mode,
)
from teleagent_adapter.windows_process_environ import (  # noqa: E402
    CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING,
)
from teleagent_adapter.windows_stdin_wrap import (  # noqa: E402
    SECRET_STDIN_KEYS,
    StdinWrapError,
    _wait_port_free,
    ensure_stdin_wrap,
    parse_env_payload,
    reset_wrap_for_tests,
)

SIMULATED = True
_FAKE_TOKEN = "fake-gui-token-" + ("a" * 48)
_FAKE_DEVICE = "fake-device"
_FAKE_INSTALL = "fake-install-id-00000000-0000-0000-0000-000000000001"
_FAKE_SESSION = "sim-session-key-for-unit-test"


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _decrypt_auth_state(enc: dict, session_key: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(session_key.encode("utf-8")).digest()
    iv = _b64url_decode(enc["iv"])
    tag = _b64url_decode(enc["tag"])
    ct = _b64url_decode(enc["ciphertext"])
    pt = AESGCM(key).decrypt(iv, ct + tag, None)
    return json.loads(pt.decode("utf-8"))


def _write_ldb(path: Path, token: str, *, mtime: float | None = None) -> None:
    blob = b"hdr\x00opencowork-auth\x01xxxx" + json.dumps(
        {"state": {"token": token, "user": {"id": "u-fake"}}, "version": 1}
    ).encode("utf-8")
    path.write_bytes(blob)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _write_meta(path: Path, device_id: str = _FAKE_DEVICE, install_id: str = _FAKE_INSTALL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"deviceId": device_id, "installId": install_id}),
        encoding="utf-8",
    )


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self) -> None:
        self._rc = 0

    def kill(self) -> None:
        self._rc = 0


class EncryptTests(unittest.TestCase):
    def test_encrypt_roundtrip_fake_token(self) -> None:
        self.assertTrue(SIMULATED)
        iv = b"\x11" * 12
        enc = encrypt_auth_state(
            {"token": _FAKE_TOKEN, "deviceId": _FAKE_DEVICE, "installId": _FAKE_INSTALL},
            _FAKE_SESSION,
            randbytes=lambda n: iv if n == 12 else b"\x00" * n,
        )
        self.assertEqual(enc["version"], "v1")
        for k in ("iv", "tag", "ciphertext"):
            self.assertIsInstance(enc[k], str)
            self.assertGreater(len(enc[k]), 4)
            self.assertNotIn("=", enc[k])
        self.assertNotIn(_FAKE_TOKEN, json.dumps(enc))
        plain = _decrypt_auth_state(enc, _FAKE_SESSION)
        self.assertEqual(plain["token"], _FAKE_TOKEN)
        self.assertEqual(plain["deviceId"], _FAKE_DEVICE)
        self.assertEqual(plain["installId"], _FAKE_INSTALL)

    def test_config_content_has_newapi(self) -> None:
        cfg = json.loads(build_opencode_config_content())
        self.assertEqual(cfg["small_model"], NEWAPI_SMALL_MODEL)
        self.assertIn("NewApi", cfg["enabled_providers"])
        self.assertEqual(cfg["provider"]["NewApi"]["options"]["baseURL"], NEWAPI_BASE_URL)
        self.assertEqual(cfg["agent"], {})
        self.assertEqual(cfg["mcp"], {})

    def test_material_repr_redacts(self) -> None:
        mat = GuiAuthMaterial(token=_FAKE_TOKEN, device_id=_FAKE_DEVICE, install_id=_FAKE_INSTALL)
        blob = repr(mat)
        self.assertNotIn(_FAKE_TOKEN, blob)
        self.assertNotIn(_FAKE_DEVICE, blob)
        self.assertNotIn(_FAKE_INSTALL, blob)
        self.assertIn("***", blob)


class ReuseModeTests(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "0"}), "off")
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "false"}), "off")
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "1"}), "on")
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "on"}), "on")
        self.assertEqual(reuse_gui_model_mode({}), "auto")
        self.assertEqual(reuse_gui_model_mode({"TELEAGENT_WIN_REUSE_GUI_MODEL": "auto"}), "auto")


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
            self.assertNotIn(_FAKE_TOKEN, str(cm.exception))

    def test_auto_found_broken_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ldb = Path(td) / "leveldb"
            ldb.mkdir()
            (ldb / "000001.ldb").write_bytes(b"opencowork-auth{{{{not-json")
            meta = Path(td) / "device-meta.json"
            _write_meta(meta)
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "auto",
                "TELEAGENT_GUI_LEVELDB_DIR": str(ldb),
                "TELEAGENT_GUI_DEVICE_META": str(meta),
            }
            self.assertTrue(gui_auth_sources_present(env))
            with self.assertRaises(GuiModelAuthError) as cm:
                prepare_gui_model_reuse(_FAKE_SESSION, environ=env)
            self.assertTrue(cm.exception.found_broken)
            self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING)

    def test_newest_mtime_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ldb = Path(td) / "leveldb"
            ldb.mkdir()
            now = time.time()
            _write_ldb(ldb / "000001.ldb", "fake-old-token-" + ("b" * 40), mtime=now - 100)
            _write_ldb(ldb / "000002.ldb", _FAKE_TOKEN, mtime=now)
            tok = load_gui_token_from_leveldb(ldb)
            self.assertEqual(tok, _FAKE_TOKEN)
            self.assertNotEqual(tok, "fake-old-token-" + ("b" * 40))

    def test_prepare_with_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ldb = Path(td) / "leveldb"
            ldb.mkdir()
            _write_ldb(ldb / "000001.ldb", _FAKE_TOKEN)
            meta = Path(td) / "device-meta.json"
            _write_meta(meta)
            cfg = Path(td) / ".config" / "TeleAgent"
            cfg.mkdir(parents=True)
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "1",
                "TELEAGENT_GUI_LEVELDB_DIR": str(ldb),
                "TELEAGENT_GUI_DEVICE_META": str(meta),
                "USERPROFILE": td,
            }
            self.assertTrue(gui_auth_sources_present(env))
            mat = load_gui_auth_material(environ=env)
            self.assertEqual(mat.token, _FAKE_TOKEN)
            extras = prepare_gui_model_reuse(_FAKE_SESSION, environ=env)
            assert extras is not None
            self.assertIn(AUTH_STATE_ENV, extras)
            self.assertIn(OPENCODE_CONFIG_CONTENT_ENV, extras)
            self.assertEqual(extras[OPENCODE_CONFIG_DIR_ENV], str(cfg))
            self.assertEqual(extras[TELEAGENT_CONFIG_DIR_ENV], str(cfg))
            blob = json.dumps(extras)
            self.assertNotIn(_FAKE_TOKEN, blob)
            enc = json.loads(extras[AUTH_STATE_ENV])
            plain = _decrypt_auth_state(enc, _FAKE_SESSION)
            self.assertEqual(plain["token"], _FAKE_TOKEN)
            self.assertIn("SUPER_AGENT_AUTH_STATE", SECRET_STDIN_KEYS)
            self.assertIn("OPENCODE_CONFIG_CONTENT", SECRET_STDIN_KEYS)

    def test_device_meta_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            meta = Path(td) / "device-meta.json"
            meta.write_text(json.dumps({"deviceId": _FAKE_DEVICE}), encoding="utf-8")
            with self.assertRaises(GuiModelAuthError) as cm:
                load_device_meta(meta)
            self.assertTrue(cm.exception.found_broken)
            self.assertNotIn(_FAKE_DEVICE, str(cm.exception))


class PortWaitTests(unittest.TestCase):
    def test_wait_port_free_hook(self) -> None:
        calls = {"n": 0}

        def listening(_port: int) -> bool:
            calls["n"] += 1
            return calls["n"] < 3

        sleeps: list[float] = []
        _wait_port_free(4401, timeout=2.0, sleep_fn=sleeps.append, listening_fn=listening)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertTrue(sleeps)

    def test_wait_port_free_timeout_fail_closed(self) -> None:
        with self.assertRaises(StdinWrapError) as cm:
            _wait_port_free(
                4401,
                timeout=0.0,
                sleep_fn=lambda _s: None,
                listening_fn=lambda _p: True,
            )
        self.assertEqual(cm.exception.blocker, "stdin_wrap_spawn_failed")
        self.assertIn("LISTENING", str(cm.exception))


class EnsureReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_wrap_for_tests()

    def tearDown(self) -> None:
        reset_wrap_for_tests()

    def test_reuse_injected_into_stdin_not_child_os(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ldb = Path(td) / "leveldb"
            ldb.mkdir()
            _write_ldb(ldb / "000001.ldb", _FAKE_TOKEN)
            meta = Path(td) / "device-meta.json"
            _write_meta(meta)
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "1",
                "TELEAGENT_GUI_LEVELDB_DIR": str(ldb),
                "TELEAGENT_GUI_DEVICE_META": str(meta),
                "TELEAGENT_WIN_CREDS_CHANNEL": "stdin_wrap",
            }
            captured: dict = {}

            def spawn(argv, *, stdin_payload, env):
                captured["payload"] = stdin_payload
                captured["env"] = dict(env)
                return _FakeProc()

            handle = ensure_stdin_wrap(
                kernel_bin="TeleAgent.exe",
                spawn_fn=spawn,
                ready_fn=lambda url: True,
                environ=env,
                listening_fn=lambda _p: False,
                kill_listeners_fn=lambda _p: None,
            )
            parsed = parse_env_payload(captured["payload"])
            self.assertIn(AUTH_STATE_ENV, parsed)
            self.assertIn(OPENCODE_CONFIG_CONTENT_ENV, parsed)
            self.assertEqual(parsed["SUPER_AGENT_LOCAL_SESSION_KEY"], handle.session_key)
            self.assertNotIn(AUTH_STATE_ENV, captured["env"])
            self.assertNotIn(OPENCODE_CONFIG_CONTENT_ENV, captured["env"])
            child_blob = json.dumps(captured["env"])
            self.assertNotIn(_FAKE_TOKEN, child_blob)
            self.assertNotIn(_FAKE_TOKEN, json.dumps(parsed[AUTH_STATE_ENV]))
            enc = json.loads(parsed[AUTH_STATE_ENV])
            plain = _decrypt_auth_state(enc, handle.session_key)
            self.assertEqual(plain["token"], _FAKE_TOKEN)

    def test_reuse_broken_does_not_spawn(self) -> None:
        spawned = {"n": 0}

        def spawn(argv, *, stdin_payload, env):
            spawned["n"] += 1
            return _FakeProc()

        with tempfile.TemporaryDirectory() as td:
            ldb = Path(td) / "leveldb"
            ldb.mkdir()
            (ldb / "000001.ldb").write_bytes(b"opencowork-auth{{{{")
            meta = Path(td) / "device-meta.json"
            _write_meta(meta)
            env = {
                "TELEAGENT_WIN_REUSE_GUI_MODEL": "auto",
                "TELEAGENT_GUI_LEVELDB_DIR": str(ldb),
                "TELEAGENT_GUI_DEVICE_META": str(meta),
            }
            with self.assertRaises(StdinWrapError) as cm:
                ensure_stdin_wrap(
                    kernel_bin="TeleAgent.exe",
                    spawn_fn=spawn,
                    ready_fn=lambda url: True,
                    environ=env,
                    listening_fn=lambda _p: False,
                    kill_listeners_fn=lambda _p: None,
                )
            self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_GUI_MODEL_AUTH_MISSING)
            self.assertEqual(spawned["n"], 0)
            self.assertNotIn(_FAKE_TOKEN, str(cm.exception))

    def test_port_wait_before_spawn(self) -> None:
        order: list[str] = []

        def listening(_port: int) -> bool:
            order.append("listen")
            return order.count("listen") < 3

        def spawn(argv, *, stdin_payload, env):
            order.append("spawn")
            return _FakeProc()

        env = {
            "TELEAGENT_WIN_REUSE_GUI_MODEL": "0",
            "TELEAGENT_GUI_LEVELDB_DIR": str(Path(tempfile.gettempdir()) / "no-ldb"),
            "TELEAGENT_GUI_DEVICE_META": str(Path(tempfile.gettempdir()) / "no-meta.json"),
        }
        ensure_stdin_wrap(
            kernel_bin="TeleAgent.exe",
            spawn_fn=spawn,
            ready_fn=lambda url: True,
            environ=env,
            listening_fn=listening,
            kill_listeners_fn=lambda _p: order.append("kill"),
            sleep_fn=lambda _s: None,
            wait_port_free_timeout=2.0,
        )
        self.assertIn("kill", order)
        self.assertGreaterEqual(order.count("listen"), 3)
        self.assertEqual(order[-1], "spawn")
        self.assertLess(order.index("kill"), order.index("spawn"))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
