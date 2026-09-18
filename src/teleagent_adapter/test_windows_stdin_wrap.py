#!/usr/bin/env python3
"""SIMULATED tests for Windows stdin_wrap creds channel (no live TeleAgent).

Fake values only: sim-pass / sim-key. Never real secrets. Spawn and /ready
are injected; this module does not exec TeleAgent.exe.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from teleagent_adapter.doctor import doctor  # noqa: E402
from teleagent_adapter.windows_process_environ import (  # noqa: E402
    CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING,
    CREDS_BLOCKER_STDIN_WRAP_READY_TIMEOUT,
    CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED,
    CREDS_SOURCE_STDIN_WRAP,
    probe_windows_creds_presence,
    resolve_windows_local_v1_creds,
)
from teleagent_adapter.windows_stdin_wrap import (  # noqa: E402
    DEFAULT_WRAP_PORT,
    SECRET_STDIN_KEYS,
    StdinWrapError,
    WrapHandle,
    build_child_os_env,
    build_env_payload,
    ensure_stdin_wrap,
    parse_env_payload,
    reset_wrap_for_tests,
    resolve_kernel_bin,
    stop_stdin_wrap,
)

SIMULATED = True

_INJECT_KEYS = (
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
    "SUPER_AGENT_LOCAL_SESSION_KEY",
    "TELEAGENT_BASE_URL",
    "TELEAGENT_CREDS_SOURCE",
    "TELEAGENT_WIN_CREDS_CHANNEL",
    "TELEAGENT_WIN_SKIP_PEB",
    "TELEAGENT_KERNEL_BIN",
    "TELEAGENT_WRAP_PORT",
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


class _WrapTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _INJECT_KEYS}
        for k in _INJECT_KEYS:
            os.environ.pop(k, None)
        reset_wrap_for_tests()

    def tearDown(self) -> None:
        reset_wrap_for_tests()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestBuildEnvPayload(_WrapTestCase):
    def test_length_header_matches_json_body(self):
        env = {
            "SERVER_PORT": "4401",
            "OPENCODE_SERVER_USERNAME": "super-agent",
            "OPENCODE_SERVER_PASSWORD": "sim-pass",
            "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
            "SUPER_AGENT_SERVER_URL": "http://127.0.0.1:4401",
        }
        payload = build_env_payload(env)
        (n,) = struct.unpack(">I", payload[:4])
        body = payload[4:]
        self.assertEqual(n, len(body))
        self.assertEqual(payload, struct.pack(">I", len(body)) + body)
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(parsed["SERVER_PORT"], "4401")
        self.assertEqual(parsed["OPENCODE_SERVER_PASSWORD"], "sim-pass")
        self.assertEqual(parse_env_payload(payload)["SUPER_AGENT_LOCAL_SESSION_KEY"], "sim-key")

    def test_child_os_env_strips_secret_keys(self):
        parent = {
            "PATH": r"C:\Windows\system32",
            "SystemRoot": r"C:\Windows",
            "TEMP": r"C:\Temp",
            "USERPROFILE": r"C:\Users\x",
            "OPENCODE_SERVER_PASSWORD": "sim-pass",
            "OPENCODE_SERVER_USERNAME": "super-agent",
            "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
        }
        child = build_child_os_env(
            parent=parent,
            xdg_data_home=r"C:\Temp\teleagent-collab-wrap\xdg",
        )
        for key in SECRET_STDIN_KEYS:
            self.assertNotIn(key, child)
        self.assertEqual(child["PATH"], parent["PATH"])
        self.assertEqual(child["SystemRoot"], parent["SystemRoot"])
        self.assertEqual(child["TEMP"], parent["TEMP"])
        self.assertEqual(child["USERPROFILE"], parent["USERPROFILE"])
        self.assertEqual(child["XDG_DATA_HOME"], r"C:\Temp\teleagent-collab-wrap\xdg")
        blob = json.dumps(child)
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)


class TestResolveKernelBin(_WrapTestCase):
    def test_env_override_when_file_exists(self):
        env = {**os.environ, "TELEAGENT_KERNEL_BIN": r"C:\custom\TeleAgent.exe"}
        got = resolve_kernel_bin(environ=env, is_file=lambda p: str(p).endswith("TeleAgent.exe"))
        self.assertEqual(Path(got), Path(r"C:\custom\TeleAgent.exe"))

    def test_default_userprofile_local_share(self):
        env = {
            "USERPROFILE": r"C:\Users\x",
            "LOCALAPPDATA": r"C:\Users\x\AppData\Local",
            "HOME": r"C:\Users\x",
        }
        needle = ".local/share/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe"

        def is_file(p: Path) -> bool:
            return needle in str(p).replace("\\", "/")

        got = resolve_kernel_bin(environ=env, is_file=is_file)
        self.assertIn(needle, str(got).replace("\\", "/"))

    def test_localappdata_variant(self):
        env = {
            "USERPROFILE": r"C:\Users\x",
            "LOCALAPPDATA": r"C:\Users\x\AppData\Local",
        }
        needle = "AppData/Local/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe"

        def is_file(p: Path) -> bool:
            return needle in str(p).replace("\\", "/")

        got = resolve_kernel_bin(environ=env, is_file=is_file)
        self.assertIn(needle, str(got).replace("\\", "/"))

    def test_missing_is_blocker_does_not_guess_program_files(self):
        env = {
            "USERPROFILE": r"C:\Users\x",
            "LOCALAPPDATA": r"C:\Users\x\AppData\Local",
            "ProgramFiles": r"C:\Program Files",
            "PROGRAMFILES": r"C:\Program Files",
        }
        gui = Path(r"C:\Program Files\TeleAgent\TeleAgent.exe")
        seen: list[str] = []

        def is_file(p: Path) -> bool:
            seen.append(str(p).replace("/", "\\"))
            return Path(p) == gui

        with self.assertRaises(StdinWrapError) as cm:
            resolve_kernel_bin(environ=env, is_file=is_file)
        self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING)
        msg = str(cm.exception)
        self.assertIn("TELEAGENT_KERNEL_BIN", msg)
        self.assertIn("Program Files", msg)
        self.assertNotIn("sim-pass", msg)
        for s in seen:
            self.assertNotIn("Program Files", s)


class TestEnsureStdinWrap(_WrapTestCase):
    def _spawn_capturing(self, store: dict):
        def spawn(argv, *, stdin_payload, env):
            store["argv"] = list(argv)
            store["payload"] = stdin_payload
            store["env"] = dict(env)
            return _FakeProc(pid=4242)

        return spawn

    def test_ensure_success_presence_source_stdin_wrap(self):
        self.assertTrue(SIMULATED)
        captured: dict = {}
        handle = ensure_stdin_wrap(
            kernel_bin=r"C:\Users\x\.local\share\TeleAgent\runtimes\super-agent-code\bin\TeleAgent.exe",
            spawn_fn=self._spawn_capturing(captured),
            ready_fn=lambda url: True,
            port=4401,
        )
        self.assertEqual(handle.source, CREDS_SOURCE_STDIN_WRAP)
        self.assertEqual(handle.port, DEFAULT_WRAP_PORT)
        self.assertEqual(handle.base_url, "http://127.0.0.1:4401")
        self.assertEqual(handle.pid, 4242)
        self.assertEqual(handle.username, "super-agent")
        self.assertTrue(handle.password)
        self.assertTrue(handle.session_key)
        self.assertNotEqual(handle.password, "sim-pass")

        child_env = captured["env"]
        for key in SECRET_STDIN_KEYS:
            self.assertNotIn(key, child_env)
        self.assertNotIn(handle.password, json.dumps(child_env))
        self.assertNotIn(handle.session_key, json.dumps(child_env))
        self.assertIn("XDG_DATA_HOME", child_env)
        self.assertIn("teleagent-collab-wrap", child_env["XDG_DATA_HOME"].replace("\\", "/"))

        parsed = parse_env_payload(captured["payload"])
        (n,) = struct.unpack(">I", captured["payload"][:4])
        self.assertEqual(n, len(captured["payload"]) - 4)
        self.assertEqual(parsed["SERVER_PORT"], "4401")
        self.assertEqual(parsed["OPENCODE_SERVER_USERNAME"], "super-agent")
        self.assertEqual(parsed["OPENCODE_SERVER_PASSWORD"], handle.password)
        self.assertEqual(parsed["SUPER_AGENT_LOCAL_SESSION_KEY"], handle.session_key)
        self.assertEqual(parsed["SUPER_AGENT_SERVER_URL"], "http://127.0.0.1:4401")

        creds, presence = resolve_windows_local_v1_creds(
            environ={},
            foreign_finder=lambda: None,
            wrap_fn=lambda: handle,
        )
        self.assertEqual(presence.source, CREDS_SOURCE_STDIN_WRAP)
        self.assertEqual(creds, (handle.username, handle.password, handle.session_key))
        self.assertTrue(presence.password_present)
        self.assertTrue(presence.session_key_present)
        self.assertIsNone(presence.blocker)
        self.assertEqual(os.environ.get("TELEAGENT_CREDS_SOURCE"), CREDS_SOURCE_STDIN_WRAP)
        self.assertEqual(os.environ.get("TELEAGENT_BASE_URL"), "http://127.0.0.1:4401")
        blob = json.dumps(presence.__dict__)
        self.assertNotIn(handle.password, blob)
        self.assertNotIn(handle.session_key, blob)
        self.assertNotIn("sim-pass", repr(handle))
        self.assertNotIn("sim-key", repr(handle))
        self.assertIn("***", repr(handle))
        from teleagent_adapter.windows_stdin_wrap import wrap_state_dir

        pid_raw = (wrap_state_dir() / "wrap.pid").read_text(encoding="utf-8")
        self.assertNotIn(handle.password, pid_raw)
        self.assertNotIn(handle.session_key, pid_raw)
        self.assertIn("4242", pid_raw)

    def test_bin_missing_blocker(self):
        with self.assertRaises(StdinWrapError) as cm:
            ensure_stdin_wrap(kernel_bin=r"C:\missing\TeleAgent.exe")
        self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING)
        self.assertNotIn("sim-pass", str(cm.exception))

        _creds, presence = resolve_windows_local_v1_creds(
            environ={"TELEAGENT_WIN_CREDS_CHANNEL": "stdin_wrap"},
            foreign_finder=lambda: (_ for _ in ()).throw(AssertionError("peb skipped")),
            wrap_fn=lambda: (_ for _ in ()).throw(
                StdinWrapError(CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING, "kernel bin missing")
            ),
        )
        self.assertIsNone(_creds)
        self.assertEqual(presence.source, "missing")
        self.assertEqual(presence.blocker, CREDS_BLOCKER_STDIN_WRAP_BIN_MISSING)
        blob = json.dumps(presence.__dict__)
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)

    def test_ready_timeout_blocker(self):
        with self.assertRaises(StdinWrapError) as cm:
            ensure_stdin_wrap(
                kernel_bin="TeleAgent.exe",
                spawn_fn=lambda argv, stdin_payload, env: _FakeProc(),
                ready_fn=lambda url: False,
                ready_timeout=0.2,
            )
        self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_STDIN_WRAP_READY_TIMEOUT)
        self.assertIn("/ready", str(cm.exception))
        self.assertNotIn("sim-pass", str(cm.exception))

        _creds, presence = resolve_windows_local_v1_creds(
            environ={"TELEAGENT_WIN_CREDS_CHANNEL": "stdin_wrap"},
            wrap_fn=lambda: (_ for _ in ()).throw(
                StdinWrapError(CREDS_BLOCKER_STDIN_WRAP_READY_TIMEOUT, "ready timeout")
            ),
        )
        self.assertEqual(presence.blocker, CREDS_BLOCKER_STDIN_WRAP_READY_TIMEOUT)

    def test_spawn_failed_blocker(self):
        def boom(argv, *, stdin_payload, env):
            raise OSError("exec format")

        with self.assertRaises(StdinWrapError) as cm:
            ensure_stdin_wrap(
                kernel_bin="TeleAgent.exe",
                spawn_fn=boom,
                ready_fn=lambda url: True,
            )
        self.assertEqual(cm.exception.blocker, CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED)
        self.assertNotIn("sim-pass", str(cm.exception))

        _creds, presence = resolve_windows_local_v1_creds(
            environ={"TELEAGENT_WIN_CREDS_CHANNEL": "stdin_wrap"},
            wrap_fn=lambda: (_ for _ in ()).throw(
                StdinWrapError(CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED, "spawn failed")
            ),
        )
        self.assertEqual(presence.blocker, CREDS_BLOCKER_STDIN_WRAP_SPAWN_FAILED)

    def test_channel_off_does_not_wrap(self):
        def nope():
            raise AssertionError("wrap must not run when channel=off")

        creds, presence = resolve_windows_local_v1_creds(
            environ={"TELEAGENT_WIN_CREDS_CHANNEL": "off"},
            foreign_finder=lambda: None,
            wrap_fn=nope,
        )
        self.assertIsNone(creds)
        self.assertEqual(presence.source, "missing")
        self.assertIsNone(presence.blocker)

    def test_channel_env_skips_peb_and_wrap(self):
        creds, presence = resolve_windows_local_v1_creds(
            environ={"TELEAGENT_WIN_CREDS_CHANNEL": "env"},
            foreign_finder=lambda: ("super-agent", "sim-pass", "sim-key"),
            wrap_fn=lambda: (_ for _ in ()).throw(AssertionError("no wrap")),
        )
        self.assertIsNone(creds)
        self.assertEqual(presence.source, "missing")

    def test_does_not_overwrite_existing_base_url(self):
        os.environ["TELEAGENT_BASE_URL"] = "http://127.0.0.1:4398"
        handle = WrapHandle(
            base_url="http://127.0.0.1:4401",
            username="super-agent",
            password="sim-pass",
            session_key="sim-key",
            pid=7,
            owns_base_url=False,
        )
        resolve_windows_local_v1_creds(
            environ={"TELEAGENT_WIN_CREDS_CHANNEL": "stdin_wrap"},
            wrap_fn=lambda: handle,
        )
        self.assertEqual(os.environ.get("TELEAGENT_BASE_URL"), "http://127.0.0.1:4398")

    def test_reuse_live_handle(self):
        captured = {"n": 0}

        def spawn(argv, *, stdin_payload, env):
            captured["n"] += 1
            return _FakeProc(pid=99)

        first = ensure_stdin_wrap(
            kernel_bin="TeleAgent.exe",
            spawn_fn=spawn,
            ready_fn=lambda url: True,
        )
        second = ensure_stdin_wrap(
            kernel_bin="TeleAgent.exe",
            spawn_fn=spawn,
            ready_fn=lambda url: True,
        )
        self.assertIs(first, second)
        self.assertEqual(captured["n"], 1)
        stop_stdin_wrap()

    def test_doctor_extras_stdin_wrap_no_secrets(self):
        presence = probe_windows_creds_presence(
            environ={},
            foreign_finder=lambda: None,
            wrap_fn=lambda: WrapHandle(
                base_url="http://127.0.0.1:4401",
                username="super-agent",
                password="sim-pass",
                session_key="sim-key",
                pid=11,
            ),
        )
        self.assertEqual(presence.source, CREDS_SOURCE_STDIN_WRAP)
        r = doctor(
            platform="win32",
            port_open_fn=lambda h, p: p == 4401,
            creds_presence_fn=lambda: presence,
        )
        self.assertEqual(r.extras.get("creds_source"), CREDS_SOURCE_STDIN_WRAP)
        self.assertTrue(r.extras.get("password_present"))
        self.assertTrue(r.extras.get("session_key_present"))
        self.assertIsNone(r.extras.get("creds_blocker"))
        blob = json.dumps(r.to_dict())
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)

    def test_auto_without_wrap_fn_on_non_windows_skips_wrap(self):
        if sys.platform.lower().startswith("win"):
            self.skipTest("host is Windows; skip-wrap default is for non-Win CI")
        creds, presence = resolve_windows_local_v1_creds(
            environ={},
            foreign_finder=lambda: None,
        )
        self.assertIsNone(creds)
        self.assertEqual(presence.source, "missing")
        self.assertIsNone(presence.blocker)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
