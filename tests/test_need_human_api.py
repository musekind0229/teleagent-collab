"""Need-human surface on Goal HTTP status/events (Win recovery fail-closed)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from framework.app_service import CollabApplication
from framework.durable_api import DurableLayer
from framework.need_human import (
    enrich_result_for_need_human,
    goal_need_human_view,
    parse_need_human,
    sanitize_reason,
)


class NeedHumanParseTests(unittest.TestCase):
    def test_parse_prefix(self):
        parsed = parse_need_human(
            "need_human: TeleAgent backend/port/cred instance changed; refusing silent redispatch"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed["need_human"])
        self.assertIn("refusing silent redispatch", parsed["reason"])

    def test_parse_rejects_plain_error(self):
        self.assertIsNone(parse_need_human("timeout after 30s"))

    def test_sanitize_redacts_tokenish(self):
        cleaned = sanitize_reason("leak token=abc123sk-abcdefghijklmnopqrstuvwxyz012345 more")
        self.assertIn("[redacted]", cleaned)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", cleaned)

    def test_enrich_result(self):
        out = enrich_result_for_need_human(
            {"ok": False, "error": "need_human: session lost after TeleAgent restart"}
        )
        assert out is not None
        self.assertTrue(out["need_human"])
        self.assertEqual(out["failure_reason"], "session lost after TeleAgent restart")


class NeedHumanDurableApiTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.layer = DurableLayer(self.root)

    def tearDown(self):
        self._td.cleanup()

    def _running_task(self):
        sub = self.layer.submit_goal(
            submit_key="nh-1",
            title="need human probe",
            desired_outcome="expose need_human",
            tasks=[{"title": "win worker"}],
        )
        self.assertTrue(sub.get("ok"), sub)
        gid = sub["goal_id"]
        tid = (self.layer.get_goal(gid)["goal"]["tasks"][0])["task_id"]
        started = self.layer.start_task(gid, tid)
        self.assertTrue(started.get("ok"), started)
        return gid, tid

    def test_finish_task_sets_failure_and_history(self):
        gid, tid = self._running_task()
        fin = self.layer.finish_task(
            gid,
            tid,
            succeeded=False,
            result={
                "ok": False,
                "error": "need_human: session lost after TeleAgent restart; refusing silent redispatch",
                "run_id": "job_abc",
            },
        )
        self.assertTrue(fin.get("ok"), fin)
        self.assertEqual(fin.get("state"), "failed")
        goal = self.layer.get_goal(gid)["goal"]
        failure = goal.get("failure") or {}
        self.assertTrue(failure.get("need_human"))
        self.assertIn("session lost", failure.get("failure_reason") or "")
        result = (goal.get("tasks") or [{}])[0].get("result") or {}
        self.assertTrue(result.get("need_human"))
        events = self.layer.list_events(gid).get("events") or []
        nh = [e for e in events if e.get("need_human") or e.get("event_kind") == "need_human"]
        self.assertTrue(nh, events)
        self.assertIn("session lost", nh[0].get("failure_reason") or "")

    def test_plain_failure_does_not_mark_need_human(self):
        gid, tid = self._running_task()
        fin = self.layer.finish_task(
            gid,
            tid,
            succeeded=False,
            result={"ok": False, "error": "worker crashed"},
        )
        self.assertTrue(fin.get("ok"), fin)
        goal = self.layer.get_goal(gid)["goal"]
        self.assertIsNone(goal.get("failure"))
        result = (goal.get("tasks") or [{}])[0].get("result") or {}
        self.assertFalse(result.get("need_human"))


class NeedHumanStatusTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.layer = DurableLayer(self.root)
        self.app = CollabApplication(self.root)

    def tearDown(self):
        self._td.cleanup()

    def test_status_exposes_need_human(self):
        sub = self.layer.submit_goal(
            submit_key="nh-status",
            title="status",
            desired_outcome="see flag",
            tasks=[{"title": "w"}],
        )
        gid = sub["goal_id"]
        tid = self.layer.get_goal(gid)["goal"]["tasks"][0]["task_id"]
        self.layer.start_task(gid, tid)
        self.layer.finish_task(
            gid,
            tid,
            succeeded=False,
            result={
                "ok": False,
                "error": "need_human: local API auth failed after cred refresh; refusing silent redispatch",
            },
        )
        st = self.app.status(gid)
        self.assertTrue(st.get("need_human"))
        self.assertIn("local API auth failed", st.get("failure_reason") or "")
        self.assertEqual(st.get("state"), "failed")
        self.assertTrue((st.get("failure") or {}).get("need_human"))

    def test_view_helper_false_when_clean(self):
        view = goal_need_human_view(failure=None, tasks=[{"status": "succeeded", "result": {"ok": True}}])
        self.assertFalse(view["need_human"])
        self.assertEqual(view["failure_reason"], "")


if __name__ == "__main__":
    unittest.main()
