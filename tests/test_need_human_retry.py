"""Controlled retry for terminal failed+need_human (Goal API)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from framework.app_service import AppError, CollabApplication, CollabHttpServer
from framework.durable_api import DurableLayer


def _fail_need_human(app: CollabApplication, *, key: str = "retry-1") -> tuple[str, str]:
    sub = app.layer.submit_goal(
        submit_key=key,
        title="retry probe",
        desired_outcome="recover after human fix",
        tasks=[
            {
                "title": "win worker",
                "expected_artifacts": ["delivery.md"],
                "done_when": {"artifacts": ["delivery.md"]},
            }
        ],
    )
    assert sub.get("ok"), sub
    gid = sub["goal_id"]
    tid = app.layer.get_goal(gid)["goal"]["tasks"][0]["task_id"]
    started = app.layer.start_task(gid, tid)
    assert started.get("ok"), started
    fin = app.layer.finish_task(
        gid,
        tid,
        succeeded=False,
        result={
            "ok": False,
            "error": "need_human: session lost after TeleAgent restart; refusing silent redispatch",
            "run_id": "job_dead",
        },
    )
    assert fin.get("ok"), fin
    assert fin.get("state") == "failed"
    return gid, tid


class NeedHumanRetryTests(unittest.TestCase):
    def test_non_need_human_failed_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(
                td,
                connection_probe=lambda: {"ok": True, "status": "ok"},
            )
            sub = app.layer.submit_goal(
                submit_key="plain-fail",
                title="plain",
                desired_outcome="no nh",
                tasks=[{"title": "w", "expected_artifacts": ["a.txt"]}],
            )
            gid = sub["goal_id"]
            tid = app.layer.get_goal(gid)["goal"]["tasks"][0]["task_id"]
            app.layer.start_task(gid, tid)
            app.layer.finish_task(
                gid,
                tid,
                succeeded=False,
                result={"ok": False, "error": "worker crashed"},
            )
            with self.assertRaises(AppError) as cm:
                app.retry(gid)
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(cm.exception.code, "not_need_human")

    def test_need_human_connection_not_ready_409(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(
                td,
                connection_probe=lambda: {
                    "ok": False,
                    "status": "missing_creds",
                    "reason": "GUI TeleAgent credentials unavailable",
                },
            )
            gid, _tid = _fail_need_human(app, key="nh-409")
            with self.assertRaises(AppError) as cm:
                app.retry(gid)
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(cm.exception.code, "connection_not_ready")
            self.assertIn("credentials", str(cm.exception).lower())
            # Goal remains failed; history not wiped.
            st = app.status(gid)
            self.assertEqual(st["state"], "failed")
            self.assertTrue(st["need_human"])
            events = app.events(gid).get("events") or []
            self.assertTrue(any(e.get("event_kind") == "need_human" or e.get("need_human") for e in events))

    def test_happy_path_redispatch_with_fake_backend(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(
                td,
                connection_probe=lambda: {"ok": True, "status": "ok"},
            )
            gid, tid = _fail_need_human(app, key="nh-ok")
            before = app.events(gid).get("events") or []
            nh_before = [e for e in before if e.get("event_kind") == "need_human" or e.get("need_human")]
            self.assertTrue(nh_before)

            out = app.retry(gid)
            self.assertTrue(out.get("ok"), out)
            self.assertEqual(out.get("mode"), "redispatch")
            self.assertEqual(out.get("task_id"), tid)
            self.assertIn(out.get("state"), {"queued", "running", "completed"})

            st = app.status(gid)
            self.assertFalse(st.get("need_human"))
            # Prior need_human history retained; retry_task appended.
            events = app.events(gid).get("events") or []
            self.assertTrue(any(e.get("event_kind") == "need_human" or e.get("need_human") for e in events))
            self.assertTrue(any(e.get("op") == "retry_task" or e.get("event_kind") == "retry_task" for e in events))

            # Coordinator tick during retry should have redispatched; ensure completion.
            if st.get("state") != "completed":
                tick = app.coordinator.process_goal(gid)
                self.assertTrue(tick.get("ok"), tick)
                st = app.status(gid)
            self.assertEqual(st.get("state"), "completed", st)
            self.assertEqual(st["tasks"][0]["status"], "succeeded")

    def test_http_retry_route_sketch(self):
        with tempfile.TemporaryDirectory() as td:
            app = CollabApplication(
                td,
                connection_probe=lambda: {"ok": True, "status": "ok"},
            )
            gid, _tid = _fail_need_human(app, key="nh-http")
            server = CollabHttpServer(("127.0.0.1", 0), app, api_token="")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", f"/v1/requests/{gid}/retry", body="{}", headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, 200, body)
                self.assertTrue(body.get("ok"), body)
                self.assertEqual(body.get("mode"), "redispatch")
                conn.close()

                # Non-need_human style gate via connection failure sketch
                app2 = CollabApplication(
                    td + "-b",
                    connection_probe=lambda: {"ok": False, "status": "not_running", "reason": "GUI port closed"},
                )
                gid2, _ = _fail_need_human(app2, key="nh-http-409")
                server2 = CollabHttpServer(("127.0.0.1", 0), app2, api_token="")
                t2 = threading.Thread(target=server2.serve_forever, daemon=True)
                t2.start()
                try:
                    port2 = server2.server_address[1]
                    c2 = HTTPConnection("127.0.0.1", port2, timeout=5)
                    c2.request("POST", f"/v1/requests/{gid2}/retry", body="{}", headers={"Content-Type": "application/json"})
                    r2 = c2.getresponse()
                    b2 = json.loads(r2.read().decode("utf-8"))
                    self.assertEqual(r2.status, 409, b2)
                    self.assertEqual(b2.get("code"), "connection_not_ready")
                    c2.close()
                finally:
                    server2.shutdown()
                    server2.server_close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
