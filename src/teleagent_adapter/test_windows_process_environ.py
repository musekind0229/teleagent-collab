#!/usr/bin/env python3
"""SIMULATED tests for Windows foreign-process environ creds (no live TeleAgent).

Fake values only: sim-pass / sim-key. Never real secrets.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from teleagent_adapter.base import AdapterError, AdapterStatus  # noqa: E402
from teleagent_adapter.doctor import doctor  # noqa: E402
from teleagent_adapter.windows_local_v1 import default_find_creds_windows  # noqa: E402
from teleagent_adapter.windows_process_environ import (  # noqa: E402
    CREDS_BLOCKER_ENVIRON_SECRETS_STRIPPED,
    CREDS_BLOCKER_OPENPROCESS_VM_READ_DENIED,
    CREDS_FAIL_CLOSED_HINT,
    CREDS_SOURCE_FOREIGN,
    CREDS_SOURCE_MISSING,
    CREDS_SOURCE_PROCESS_ENV,
    MISSING_CREDS_MESSAGE,
    ProcessSnapshot,
    WindowsCredsPresence,
    WindowsEnvironUnavailable,
    encode_environ_block,
    extract_local_v1_creds,
    find_creds_in_environ_blocks,
    find_creds_in_foreign_processes,
    is_teleagent_candidate,
    parse_environ_block,
    probe_windows_creds_presence,
    read_process_environ_via_peb,
    resolve_windows_local_v1_creds,
)

SIMULATED = True
_SIM_ENV = {
    "OPENCODE_SERVER_USERNAME": "super-agent",
    "OPENCODE_SERVER_PASSWORD": "sim-pass",
    "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
}


class TestParseEnvironBlock(unittest.TestCase):
    def test_utf16_le_native_windows_block(self):
        raw = encode_environ_block(_SIM_ENV, wide=True)
        env = parse_environ_block(raw)
        self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], "sim-pass")
        self.assertEqual(env["SUPER_AGENT_LOCAL_SESSION_KEY"], "sim-key")
        self.assertEqual(env["OPENCODE_SERVER_USERNAME"], "super-agent")

    def test_utf8_linux_style_block(self):
        raw = encode_environ_block(_SIM_ENV, wide=False)
        env = parse_environ_block(raw)
        self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], "sim-pass")
        self.assertEqual(extract_local_v1_creds(env), ("super-agent", "sim-pass", "sim-key"))

    def test_username_defaults_super_agent(self):
        env = parse_environ_block(
            encode_environ_block(
                {
                    "OPENCODE_SERVER_PASSWORD": "sim-pass",
                    "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
                },
                wide=True,
            )
        )
        self.assertEqual(extract_local_v1_creds(env), ("super-agent", "sim-pass", "sim-key"))

    def test_opencode_password_alias(self):
        env = {
            "SUPER_AGENT_OPENCODE_PASSWORD": "sim-pass",
            "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key",
        }
        self.assertEqual(extract_local_v1_creds(env), ("super-agent", "sim-pass", "sim-key"))

    def test_missing_session_key_is_none(self):
        self.assertIsNone(
            extract_local_v1_creds({"OPENCODE_SERVER_PASSWORD": "sim-pass"})
        )

    def test_missing_password_is_none(self):
        self.assertIsNone(
            extract_local_v1_creds({"SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key"})
        )

    def test_empty_block(self):
        self.assertEqual(parse_environ_block(b""), {})
        self.assertIsNone(find_creds_in_environ_blocks([]))


class TestCandidateFilter(unittest.TestCase):
    def test_teleagent_exe(self):
        self.assertTrue(is_teleagent_candidate("TeleAgent.exe"))
        self.assertTrue(is_teleagent_candidate("teleagent.exe"))

    def test_runtime_path(self):
        self.assertTrue(
            is_teleagent_candidate(
                "node.exe",
                r"C:\Users\x\AppData\Roaming\TeleAgent\runtimes\super-agent-code\bin\node.exe",
            )
        )
        self.assertTrue(
            is_teleagent_candidate("sac.exe", r"D:\opencode\runtime\sac.exe")
        )

    def test_unrelated(self):
        self.assertFalse(is_teleagent_candidate("notepad.exe", r"C:\Windows\notepad.exe"))
        self.assertFalse(is_teleagent_candidate("svchost.exe", r"C:\Windows\System32\svchost.exe"))


class TestForeignFinderInjected(unittest.TestCase):
    def test_injected_utf16_block_yields_creds(self):
        self.assertTrue(SIMULATED)
        raw = encode_environ_block(_SIM_ENV, wide=True)
        got = find_creds_in_foreign_processes(environ_blocks=[raw])
        self.assertEqual(got, ("super-agent", "sim-pass", "sim-key"))

    def test_fake_enumerator_reads_candidate_only(self):
        ta = ProcessSnapshot(
            pid=4242,
            name="TeleAgent.exe",
            image_path=r"C:\Program Files\TeleAgent\TeleAgent.exe",
        )
        other = ProcessSnapshot(pid=7, name="notepad.exe", image_path=r"C:\Windows\notepad.exe")
        blocks = {
            4242: encode_environ_block(_SIM_ENV, wide=True),
            7: encode_environ_block(
                {"OPENCODE_SERVER_PASSWORD": "sim-pass", "SUPER_AGENT_LOCAL_SESSION_KEY": "sim-key"},
                wide=True,
            ),
        }
        read_pids: list[int] = []

        def reader(pid: int) -> bytes | None:
            read_pids.append(pid)
            return blocks.get(pid)

        got = find_creds_in_foreign_processes(
            enumerator=lambda: [other, ta],
            environ_reader=reader,
            skip_pid=1,
        )
        self.assertEqual(got, ("super-agent", "sim-pass", "sim-key"))
        self.assertIn(4242, read_pids)
        self.assertNotIn(7, read_pids)

    def test_skips_own_pid(self):
        own = ProcessSnapshot(pid=99, name="TeleAgent.exe", image_path=r"C:\TeleAgent\TeleAgent.exe")

        def reader(pid: int) -> bytes | None:
            raise AssertionError("should not read skipped pid")

        got = find_creds_in_foreign_processes(
            enumerator=lambda: [own],
            environ_reader=reader,
            skip_pid=99,
        )
        self.assertIsNone(got)

    def test_missing_key_in_foreign_block(self):
        raw = encode_environ_block({"OPENCODE_SERVER_PASSWORD": "sim-pass"}, wide=True)
        self.assertIsNone(find_creds_in_foreign_processes(environ_blocks=[raw]))

    def test_non_windows_peb_reader_raises(self):
        if sys.platform.lower().startswith("win"):
            self.skipTest("host is Windows; unavailability raise is for non-Win")
        with self.assertRaises(WindowsEnvironUnavailable) as cm:
            read_process_environ_via_peb(4242)
        msg = str(cm.exception).lower()
        self.assertIn("win32", msg)
        self.assertNotIn("disable", msg)

    def test_non_windows_enumerate_noop(self):
        if sys.platform.lower().startswith("win"):
            self.skipTest("host is Windows")
        from teleagent_adapter.windows_process_environ import enumerate_windows_processes

        self.assertEqual(enumerate_windows_processes(), [])
        self.assertIsNone(find_creds_in_foreign_processes())


class TestDefaultFindCredsOrder(unittest.TestCase):
    def test_process_env_wins_over_foreign(self):
        u, p, k = default_find_creds_windows(
            environ=dict(_SIM_ENV),
            foreign_finder=lambda: ("other", "sim-other-pass", "sim-other-key"),
        )
        self.assertEqual((u, p, k), ("super-agent", "sim-pass", "sim-key"))
        creds, presence = resolve_windows_local_v1_creds(
            environ=dict(_SIM_ENV),
            foreign_finder=lambda: ("other", "sim-other-pass", "sim-other-key"),
        )
        self.assertEqual(presence.source, CREDS_SOURCE_PROCESS_ENV)
        self.assertEqual(creds, ("super-agent", "sim-pass", "sim-key"))

    def test_foreign_used_when_process_env_empty(self):
        u, p, k = default_find_creds_windows(
            environ={},
            foreign_finder=lambda: ("super-agent", "sim-pass", "sim-key"),
        )
        self.assertEqual((u, p, k), ("super-agent", "sim-pass", "sim-key"))
        _creds, presence = resolve_windows_local_v1_creds(
            environ={},
            foreign_finder=lambda: ("super-agent", "sim-pass", "sim-key"),
        )
        self.assertEqual(presence.source, CREDS_SOURCE_FOREIGN)

    def test_missing_both_sources(self):
        with self.assertRaises(AdapterError) as cm:
            default_find_creds_windows(
                environ={}, foreign_finder=lambda: None, wrap_fn=lambda: None
            )
        self.assertEqual(cm.exception.status, AdapterStatus.MISSING_CREDS)
        msg = str(cm.exception)
        self.assertIn("other", msg.lower())
        self.assertIn("process environ", msg.lower())
        self.assertIn("not this-process env", msg)
        self.assertIn("Do not disable authentication", msg)
        self.assertIn("stdin", msg.lower())
        self.assertIn("SECRET_ENV_KEYS", msg)
        self.assertIn("Do not scrape Credential Manager", msg)
        self.assertIn("SeDebugPrivilege", msg)
        self.assertIn(CREDS_FAIL_CLOSED_HINT, msg)
        self.assertEqual(MISSING_CREDS_MESSAGE, msg)
        self.assertNotIn("sim-pass", msg)
        self.assertNotIn("sim-key", msg)

    def test_missing_only_session_key(self):
        with self.assertRaises(AdapterError) as cm:
            default_find_creds_windows(
                environ={"OPENCODE_SERVER_PASSWORD": "sim-pass"},
                foreign_finder=lambda: None,
            )
        self.assertEqual(cm.exception.status, AdapterStatus.MISSING_CREDS)

    def test_probe_never_returns_secret_values(self):
        presence = probe_windows_creds_presence(
            environ=dict(_SIM_ENV),
            foreign_finder=lambda: ("super-agent", "sim-pass", "sim-key"),
        )
        blob = json.dumps(presence.__dict__)
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)
        self.assertEqual(presence.source, CREDS_SOURCE_PROCESS_ENV)
        self.assertTrue(presence.password_present)
        self.assertTrue(presence.session_key_present)

        missing = probe_windows_creds_presence(
            environ={},
            foreign_finder=lambda: None,
            wrap_fn=lambda: None,
            enumerator=lambda: [],
        )
        self.assertEqual(missing.source, CREDS_SOURCE_MISSING)
        self.assertFalse(missing.password_present)
        self.assertFalse(missing.session_key_present)
        self.assertIsNone(missing.blocker)


class TestDoctorExtrasWin(unittest.TestCase):
    def test_extras_ports_and_creds_source_no_secrets(self):
        r = doctor(platform="win32", port_open_fn=lambda h, p: p == 4397)
        self.assertFalse(r.extras.get("windows_live_verified", True))
        self.assertIn(r.extras.get("creds_source"), (
            CREDS_SOURCE_PROCESS_ENV,
            CREDS_SOURCE_FOREIGN,
            CREDS_SOURCE_MISSING,
            "stdin_wrap",
        ))
        ports = r.extras.get("ports") or {}
        self.assertIn("4397", ports)
        self.assertIn("4399", ports)
        self.assertIn("4398", ports)
        self.assertTrue(ports["4397"])
        self.assertFalse(ports["4399"])
        self.assertFalse(ports["4398"])
        self.assertIsInstance(r.extras.get("password_present"), bool)
        self.assertIsInstance(r.extras.get("session_key_present"), bool)
        blob = json.dumps(r.to_dict())
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)
        self.assertNotIn("OPENCODE_SERVER_PASSWORD", blob)
        self.assertNotIn("SUPER_AGENT_LOCAL_SESSION_KEY", blob)
        self.assertIn(r.extras.get("creds_blocker"), (
            None,
            CREDS_BLOCKER_OPENPROCESS_VM_READ_DENIED,
            CREDS_BLOCKER_ENVIRON_SECRETS_STRIPPED,
            "stdin_wrap_bin_missing",
            "stdin_wrap_ready_timeout",
            "stdin_wrap_spawn_failed",
            "gui_model_auth_missing",
        ))


class TestCredsBlockerDiagnosis(unittest.TestCase):
    """Mock-only: distinguish OpenProcess denied vs stdin-stripped environ."""

    _TA = ProcessSnapshot(
        pid=4242,
        name="TeleAgent.exe",
        image_path=r"C:\Program Files\TeleAgent\TeleAgent.exe",
    )
    _WORKER = ProcessSnapshot(
        pid=4243,
        name="node.exe",
        image_path=r"C:\Users\x\AppData\Roaming\TeleAgent\runtimes\super-agent-code\bin\node.exe",
    )
    _RENDERER = ProcessSnapshot(
        pid=4244,
        name="TeleAgent.exe",
        image_path=r"C:\Program Files\TeleAgent\TeleAgent.exe",
    )

    def test_openprocess_vm_read_denied(self):
        def reader(pid: int) -> bytes | None:
            raise WindowsEnvironUnavailable(
                f"OpenProcess failed for pid {pid} ACCESS_DENIED"
            )

        presence = probe_windows_creds_presence(
            environ={},
            enumerator=lambda: [self._TA, self._WORKER],
            environ_reader=reader,
            skip_pid=1,
            wrap_fn=lambda: None,
        )
        self.assertEqual(presence.source, CREDS_SOURCE_MISSING)
        self.assertEqual(presence.blocker, CREDS_BLOCKER_OPENPROCESS_VM_READ_DENIED)
        self.assertGreaterEqual(presence.openprocess_denied_count, 1)
        self.assertGreaterEqual(presence.foreign_candidates, 1)
        self.assertEqual(presence.environ_readable_without_secrets, 0)
        blob = json.dumps(presence.__dict__)
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)

    def test_environ_secrets_stripped(self):
        def reader(pid: int) -> bytes | None:
            return encode_environ_block(
                {"PATH": r"C:\Windows", "TEMP": r"C:\Temp"},
                wide=True,
            )

        presence = probe_windows_creds_presence(
            environ={},
            enumerator=lambda: [self._RENDERER],
            environ_reader=reader,
            skip_pid=1,
            wrap_fn=lambda: None,
        )
        self.assertEqual(presence.source, CREDS_SOURCE_MISSING)
        self.assertEqual(presence.blocker, CREDS_BLOCKER_ENVIRON_SECRETS_STRIPPED)
        self.assertGreaterEqual(presence.environ_readable_without_secrets, 1)
        blob = json.dumps(presence.__dict__)
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)

    def test_stripped_preferred_when_both_signs(self):
        def reader(pid: int) -> bytes | None:
            if pid == self._WORKER.pid:
                raise WindowsEnvironUnavailable(
                    f"OpenProcess failed for pid {pid} ACCESS_DENIED"
                )
            return encode_environ_block({"PATH": r"C:\Windows"}, wide=True)

        presence = probe_windows_creds_presence(
            environ={},
            enumerator=lambda: [self._WORKER, self._RENDERER],
            environ_reader=reader,
            skip_pid=1,
            wrap_fn=lambda: None,
        )
        self.assertEqual(presence.blocker, CREDS_BLOCKER_ENVIRON_SECRETS_STRIPPED)
        self.assertGreaterEqual(presence.openprocess_denied_count, 1)
        self.assertGreaterEqual(presence.environ_readable_without_secrets, 1)

    def test_process_env_complete_blocker_empty(self):
        def reader(pid: int) -> bytes | None:
            raise AssertionError("must not scan foreign when process env is complete")

        presence = probe_windows_creds_presence(
            environ=dict(_SIM_ENV),
            enumerator=lambda: [self._TA],
            environ_reader=reader,
            skip_pid=1,
        )
        self.assertEqual(presence.source, CREDS_SOURCE_PROCESS_ENV)
        self.assertIsNone(presence.blocker)
        self.assertTrue(presence.password_present)
        self.assertTrue(presence.session_key_present)
        blob = json.dumps(presence.__dict__)
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)

    def test_doctor_extras_blocker_and_fail_closed_details(self):
        presence = WindowsCredsPresence(
            source=CREDS_SOURCE_MISSING,
            password_present=False,
            session_key_present=False,
            foreign_candidates=2,
            blocker=CREDS_BLOCKER_OPENPROCESS_VM_READ_DENIED,
            openprocess_denied_count=2,
            environ_readable_without_secrets=0,
        )

        class _MissingAdapter:
            base_url = "http://127.0.0.1:4398"

            def refresh_creds(self):
                raise AdapterError(AdapterStatus.MISSING_CREDS, MISSING_CREDS_MESSAGE)

        r = doctor(
            platform="win32",
            adapter=_MissingAdapter(),
            port_open_fn=lambda h, p: p == 4398,
            creds_presence_fn=lambda: presence,
        )
        self.assertEqual(r.status, AdapterStatus.MISSING_CREDS.value)
        self.assertEqual(r.extras.get("creds_blocker"), CREDS_BLOCKER_OPENPROCESS_VM_READ_DENIED)
        self.assertEqual(r.extras.get("creds_source"), CREDS_SOURCE_MISSING)
        self.assertFalse(r.extras.get("password_present"))
        self.assertFalse(r.extras.get("session_key_present"))
        self.assertIn("4398", r.extras.get("ports") or {})
        details = " ".join(r.details)
        self.assertIn("stdin", details.lower())
        self.assertIn("Do not disable authentication", details)
        self.assertIn("Credential Manager", details)
        self.assertIn("SeDebugPrivilege", details)
        blob = json.dumps(r.to_dict())
        self.assertNotIn("sim-pass", blob)
        self.assertNotIn("sim-key", blob)

    def test_doctor_extras_stripped_blocker(self):
        presence = WindowsCredsPresence(
            source=CREDS_SOURCE_MISSING,
            password_present=False,
            session_key_present=False,
            foreign_candidates=1,
            blocker=CREDS_BLOCKER_ENVIRON_SECRETS_STRIPPED,
            openprocess_denied_count=0,
            environ_readable_without_secrets=1,
        )
        r = doctor(
            platform="win32",
            port_open_fn=lambda h, p: p == 4398,
            creds_presence_fn=lambda: presence,
        )
        self.assertEqual(r.extras.get("creds_blocker"), CREDS_BLOCKER_ENVIRON_SECRETS_STRIPPED)
        self.assertFalse(r.extras.get("windows_live_verified", True))


if __name__ == "__main__":
    raise SystemExit(unittest.main())


class TestPbiClassNotShadowed(unittest.TestCase):
    def test_process_basic_information_class_is_int_zero(self):
        from teleagent_adapter import windows_process_environ as m

        self.assertIsInstance(m._PROCESS_BASIC_INFORMATION_CLASS, int)
        self.assertEqual(m._PROCESS_BASIC_INFORMATION_CLASS, 0)
        self.assertTrue(issubclass(m._PROCESS_BASIC_INFORMATION_STRUCT, object))

