#!/usr/bin/env python3
"""P3 tests: lead stubs, Question API, controlled install, Win blocked — no fake PASS."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from charter import load_charter, validate_charter
from controlled_install import (
    install_fake_package,
    rollback_fake_package,
    skip_dangerous_real_install,
)
from lead_adapter import (
    ClaudeCodeLeadAdapter,
    CodexCliLeadAdapter,
    GrokCliLeadAdapter,
    get_lead_adapter,
    build_lead_request,
    lead_permission_response_schema,
    validate_lead_decision,
    LeadDecisionError,
)
from question_api import (
    handle_question_need_human,
    probe_question_api,
    reply_question,
    reject_question,
    list_questions_for_session,
)
from teleagent_adapter import WindowsBlockedAdapter, AdapterError, AdapterStatus, get_adapter, doctor
from teleagent_adapter.linux_local_v1 import LinuxLocalV1Adapter


SIMULATED = True


class TestLeadStubs(unittest.TestCase):
    def test_claude_stub_no_fake_pass(self):
        ad = get_lead_adapter("claude_code")
        self.assertIsInstance(ad, ClaudeCodeLeadAdapter)
        req = build_lead_request(
            kind="permission",
            goal="g",
            authorized_scope=["ws"],
            prohibitions=[],
            acceptance_criteria={},
            current_application={"id": "p1"},
        )
        raw, parsed = ad.decide(req, schema=lead_permission_response_schema(), cwd="/tmp")
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        self.assertNotIn(parsed.get("decision"), ("once", "pass"))
        self.assertFalse(ad.doctor_hint().get("fake_pass"))
        with self.assertRaises(LeadDecisionError) as cm:
            validate_lead_decision(raw, parsed, request=req, kind="permission")
        self.assertEqual(cm.exception.code, "call_failed")

    def test_codex_cli_stub_no_fake_pass(self):
        ad = get_lead_adapter("codex_cli")
        self.assertIsInstance(ad, CodexCliLeadAdapter)
        req = build_lead_request(
            kind="review",
            goal="g",
            authorized_scope=[],
            prohibitions=[],
            acceptance_criteria={},
            current_application={},
        )
        raw, parsed = ad.decide(req, schema={}, cwd="/tmp")
        self.assertEqual(parsed.get("_lead_status"), "call_failed")
        # review stub must not yield verdict=pass
        self.assertNotEqual((parsed or {}).get("verdict"), "pass")

    def test_codex_alias_still_inprocess(self):
        ad = get_lead_adapter("codex")
        self.assertEqual(ad.name, "inprocess")

    def test_grok_factory(self):
        ad = get_lead_adapter("grok_cli")
        self.assertIsInstance(ad, GrokCliLeadAdapter)


class TestQuestionAPISim(unittest.TestCase):
    def test_probe_and_session_bind(self):
        class Fake:
            def call(self, method, path, body=None, **kw):
                if method == "GET" and path == "/question":
                    return 200, [
                        {"id": "q1", "sessionID": "ses_a", "questions": [{"header": "x?"}]},
                        {"id": "q2", "sessionID": "ses_b"},
                    ]
                if method == "POST" and path.endswith("/reply"):
                    return 200, True
                if method == "POST" and path.endswith("/reject"):
                    return 200, True
                return 404, None

            def list_questions(self, *, session_id=None):
                c, items = self.call("GET", "/question")
                from teleagent_adapter.base import filter_by_session
                return c, filter_by_session(items, session_id)

            def reply_question(self, request_id, answers):
                return self.call("POST", f"/question/{request_id}/reply", body={"answers": answers})

            def reject_question(self, request_id):
                return self.call("POST", f"/question/{request_id}/reject", body={})

        fake = Fake()
        probe = probe_question_api(fake)
        self.assertTrue(probe.available)
        _, qs = list_questions_for_session(fake, "ses_a")
        self.assertEqual(len(qs), 1)
        with self.assertRaises(ValueError):
            reply_question(
                fake, "q1", [["yes"]], session_id="ses_a", pending={"id": "q1", "sessionID": "ses_other"}
            )
        code, _ = reply_question(
            fake, "q1", [["yes"]], session_id="ses_a", pending={"id": "q1", "sessionID": "ses_a"}
        )
        self.assertEqual(code, 200)
        code, _ = reject_question(
            fake, "q2", session_id="ses_b", pending={"id": "q2", "sessionID": "ses_b"}
        )
        self.assertEqual(code, 200)

    def test_need_human_default_no_fake_answer(self):
        q = {"id": "q9", "sessionID": "ses_x", "questions": [{"header": "continue?"}]}
        d = handle_question_need_human(q, session_id="ses_x")
        self.assertEqual(d["action"], "need_human")
        d2 = handle_question_need_human(q, session_id="ses_x", auto_answers=[["yes"]])
        self.assertEqual(d2["action"], "reply")

    def test_probe_gap_404(self):
        class Fake404:
            def call(self, method, path, body=None, **kw):
                return 404, {"error": "missing"}

        probe = probe_question_api(Fake404())
        self.assertFalse(probe.available)
        self.assertEqual(probe.status, AdapterStatus.API_INCOMPATIBLE.value)


class TestControlledInstall(unittest.TestCase):
    def _charter(self, **over):
        c = {
            "name": "fake-inst",
            "task_kind": "system_install",
            "goal": "fake",
            "must": ["workdir"],
            "must_not": ["apt"],
            "network_allow": [],
            "install_roots": ["fake-opt/collab-apps/demo"],
            "rollback": "rm -rf fake-opt/collab-apps/demo",
            "user_gate_permissions": ["sudo", "package_manager"],
            "done_when": {"artifacts": ["install-plan.md", "rollback-notes.md"]},
            "acceptance": "fake ok",
        }
        c.update(over)
        return c

    def test_sample_charter_loads(self):
        path = REPO / "jobs" / "examples" / "controlled-fake-install.charter.yaml"
        ch = load_charter(path)
        validate_charter(ch)
        self.assertEqual(ch["task_kind"], "system_install")

    def test_fake_install_and_auth_gate(self):
        with tempfile.TemporaryDirectory(prefix="p3inst-") as d:
            ws = Path(d)
            r = install_fake_package(charter=self._charter(), workdir=ws)
            self.assertTrue(r.ok, r.reason)
            self.assertEqual(r.status, "installed")
            art = Path(r.artifact_path)
            self.assertTrue((art / "FAKE_PACKAGE.json").exists())
            self.assertTrue((ws / "install-plan.md").exists())
            self.assertTrue((ws / "rollback-notes.md").exists())
            # outside install_roots denied
            bad = install_fake_package(
                charter=self._charter(install_roots=["/etc/collab-evil"]),
                workdir=ws,
            )
            self.assertFalse(bad.ok)
            self.assertEqual(bad.status, "denied")
            rb = rollback_fake_package(art, workdir=ws)
            self.assertTrue(rb.ok)
            self.assertFalse(art.exists())

    def test_dangerous_real_install_blocked(self):
        blocked = skip_dangerous_real_install("sudo apt-get install -y rustdesk")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.status, "blocked")
        with tempfile.TemporaryDirectory(prefix="p3blk-") as d:
            r = install_fake_package(
                charter=self._charter(),
                workdir=d,
                real_install_command="apt install nginx",
            )
            self.assertFalse(r.ok)
            self.assertEqual(r.status, "blocked")

    def test_user_gate_package_manager(self):
        from task_auth import authorize_action

        d = authorize_action(
            charter=self._charter(),
            permission={"tool": "bash", "command": "apt install foo"},
            workspace="/tmp",
        )
        self.assertTrue(d.needs_user)
        self.assertFalse(d.allowed)


class TestWindowsStillBlocked(unittest.TestCase):
    def test_win_blocked(self):
        w = get_adapter(platform="win32")
        self.assertIsInstance(w, WindowsBlockedAdapter)
        with self.assertRaises(AdapterError) as cm:
            w.list_questions()
        self.assertEqual(cm.exception.status, AdapterStatus.BLOCKED)
        with self.assertRaises(AdapterError):
            w.reply_question("q1", [["a"]])
        with self.assertRaises(AdapterError):
            w.reject_question("q1")
        rep = doctor(platform="win32", simulated=True, simulate_status="blocked")
        self.assertEqual(rep.status, "blocked")


class TestLiveQuestionProbeOptional(unittest.TestCase):
    """Best-effort live probe — skip/block honestly, never fake PASS."""

    def test_live_question_route_if_up(self):
        try:
            ad = get_adapter()
            ad.refresh_creds()
        except Exception as e:
            self.skipTest(f"BLOCKED: no creds/TeleAgent: {e}")
        probe = probe_question_api(ad)
        # Document result; if available must be list-shaped
        if not probe.available:
            self.skipTest(f"GAP: question API unavailable: {probe.to_dict()}")
        self.assertTrue(probe.available)
        self.assertEqual(probe.status, "ok")


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
