"""Permission/question handoff through Goal decision API (supervised backend)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from framework.app_service import CollabApplication, CollabHttpServer, DeterministicPlanner


class ExternalOnlyPlanner(DeterministicPlanner):
    """Same plan shape, but never auto-resolves worker gates via a lead."""

    decide_action = None  # type: ignore[assignment]


class FakeSupervisedBackend:
    backend_id = "fake.supervised"

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.resolved: list[dict[str, Any]] = []
        self._runs: dict[str, dict[str, Any]] = {}
        self._n = 0

    def start_run(
        self,
        *,
        title: str,
        directory: str,
        instruction: str = "",
        artifacts: list[str] | None = None,
        charter: dict | None = None,
    ) -> dict[str, Any]:
        self._n += 1
        run_id = f"run_{self._n:04d}"
        self._runs[run_id] = {
            "directory": directory,
            "artifacts": list(artifacts or ["out.md"]),
            "busy": True,
        }
        return {"ok": True, "run_id": run_id}

    def list_pending_actions(self, *, session_id: str | None = None) -> tuple[int, list]:
        rows = list(self.pending)
        if session_id:
            rows = [r for r in rows if str(r.get("run_id") or "") == str(session_id)]
        return 200, rows

    def resolve_decision(
        self,
        request_id: str,
        *,
        verdict: str,
        reason: str,
        answers: list | None = None,
    ) -> dict[str, Any]:
        hit = next((r for r in self.pending if str(r.get("request_id")) == str(request_id)), None)
        if hit is None:
            return {"ok": False, "error": "unknown"}
        self.pending = [r for r in self.pending if str(r.get("request_id")) != str(request_id)]
        rec = {
            "request_id": request_id,
            "kind": hit.get("kind"),
            "verdict": verdict,
            "reason": reason,
            "answers": list(answers or []),
        }
        self.resolved.append(rec)
        return {"ok": True, "request_id": request_id, "kind": hit.get("kind")}

    def observe_run(self, run_id: str, **_kwargs: Any) -> dict[str, Any]:
        pending_for = [r for r in self.pending if str(r.get("run_id") or "") == str(run_id)]
        busy = bool(pending_for) or bool(self._runs.get(run_id, {}).get("busy", True))
        return {"busy": busy}

    def collect_result(self, run_id: str) -> dict[str, Any]:
        meta = self._runs[run_id]
        root = Path(meta["directory"])
        for name in meta["artifacts"]:
            (root / name).write_text("done\n", encoding="utf-8")
        meta["busy"] = False
        return {"ok": True, "run_id": run_id, "artifacts": list(meta["artifacts"])}

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        out = self.resolve_decision(request_id, verdict=reply, reason="reply_permission")
        return (200 if out.get("ok") else 409), out

    def cancel(self, run_id: str) -> tuple[int, Any]:
        return 200, {"ok": True, "run_id": run_id}


class DecisionApiHandoffTests(unittest.TestCase):
    def _boot(self) -> tuple[CollabApplication, FakeSupervisedBackend, str]:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        backend = FakeSupervisedBackend()
        app = CollabApplication(td.name, planner=ExternalOnlyPlanner(), backend=backend)
        sub = app.submit(
            {
                "goal": "ship a note",
                "acceptance": {"artifacts": ["out.md"]},
            }
        )
        self.assertTrue(sub.get("ok"), sub)
        return app, backend, str(sub["goal_id"])

    def _ensure_run(self, app: CollabApplication, goal_id: str) -> str:
        for _ in range(6):
            app.coordinator.process_goal(goal_id)
            goal = app.layer.get_goal(goal_id)["goal"]
            run_id = str((goal.get("tasks") or [{}])[0].get("run_id") or "")
            if run_id:
                return run_id
        self.fail("task never bound a run_id")

    def _drive_until_decision(self, app: CollabApplication, goal_id: str) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for _ in range(8):
            last = app.coordinator.process_goal(goal_id)
            pending = (app.layer.get_goal(goal_id).get("goal") or {}).get("pending_decisions") or []
            if last.get("action") == "decision_required" or pending:
                return last
        return last

    def test_permission_opens_and_resolve_reaches_backend(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "perm_1",
                "kind": "permission",
                "run_id": run_id,
                "context_hash": "h1",
                "payload": {"permission": "edit", "patterns": ["*.md"]},
            }
        )
        tick = self._drive_until_decision(app, gid)
        self.assertEqual(tick.get("action"), "decision_required", tick)
        pending = app.layer.get_goal(gid)["goal"].get("pending_decisions") or []
        self.assertEqual(len(pending), 1, pending)
        self.assertEqual(pending[0].get("kind"), "action_approval")
        self.assertEqual((pending[0].get("details") or {}).get("backend_kind"), "permission")
        did = pending[0]["decision_id"]

        out = app.resolve(gid, did, {"verdict": "once", "reason": "scoped edit ok"})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(len(backend.resolved), 1)
        self.assertEqual(backend.resolved[0]["verdict"], "once")
        self.assertEqual(backend.resolved[0]["kind"], "permission")

    def test_question_answers_round_trip(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "q_1",
                "kind": "question",
                "run_id": run_id,
                "context_hash": "hq",
                "payload": {"questions": [{"header": "which?", "options": ["a", "b"]}]},
            }
        )
        tick = self._drive_until_decision(app, gid)
        self.assertEqual(tick.get("action"), "decision_required", tick)
        pending = app.layer.get_goal(gid)["goal"].get("pending_decisions") or []
        self.assertEqual(pending[0].get("kind"), "question")
        did = pending[0]["decision_id"]
        out = app.resolve(
            gid,
            did,
            {"verdict": "answer", "reason": "picked a", "answers": [["a"]]},
        )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(backend.resolved[0]["verdict"], "answer")
        self.assertEqual(backend.resolved[0]["answers"], [["a"]])

    def test_system_action_projects_before_later_permission(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending = [
            {
                "request_id": "sys_1",
                "kind": "system_action",
                "run_id": run_id,
                "context_hash": "hs",
                "payload": {"action": "install"},
            },
            {
                "request_id": "perm_2",
                "kind": "permission",
                "run_id": run_id,
                "context_hash": "h2",
                "payload": {"permission": "edit"},
            },
        ]
        tick = self._drive_until_decision(app, gid)
        self.assertEqual(tick.get("action"), "decision_required", tick)
        pending = app.layer.get_goal(gid)["goal"].get("pending_decisions") or []
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get("kind"), "system_action_approval")
        self.assertEqual((pending[0].get("details") or {}).get("backend_kind"), "system_action")
        self.assertEqual((pending[0].get("details") or {}).get("backend_request_id"), "sys_1")

    def test_system_action_resolve_reaches_backend_without_lead_auto(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending = [
            {
                "request_id": "sys_only",
                "kind": "system_action",
                "run_id": run_id,
                "context_hash": "hs",
                "payload": {"proposal_sha256": "abc"},
            }
        ]
        tick = self._drive_until_decision(app, gid)
        self.assertEqual(tick.get("action"), "decision_required", tick)
        pending = app.layer.get_goal(gid)["goal"].get("pending_decisions") or []
        self.assertEqual(pending[0].get("kind"), "system_action_approval")
        did = pending[0]["decision_id"]
        # ExternalOnlyPlanner has decide_action=None; even if lead existed, AUTO_RESOLVE excludes system_action.
        out = app.resolve(gid, did, {"verdict": "approve", "reason": "user authorized MSI"})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(backend.resolved[0]["verdict"], "approve")
        self.assertEqual(backend.resolved[0]["kind"], "system_action")

    def test_http_decision_route(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "perm_http",
                "kind": "permission",
                "run_id": run_id,
                "context_hash": "hh",
                "payload": {},
            }
        )
        self._drive_until_decision(app, gid)
        did = app.layer.get_goal(gid)["goal"]["pending_decisions"][0]["decision_id"]
        server = CollabHttpServer(("127.0.0.1", 0), app, api_token="")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            body = json.dumps({"verdict": "reject", "reason": "out of scope"})
            conn.request(
                "POST",
                f"/v1/requests/{gid}/decisions/{did}",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200, payload)
            self.assertTrue(payload.get("ok"), payload)
            self.assertEqual(backend.resolved[0]["verdict"], "reject")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


    def test_resolve_auto_ticks_coordinator(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "perm_tick",
                "kind": "permission",
                "run_id": run_id,
                "context_hash": "ht",
                "payload": {},
            }
        )
        self._drive_until_decision(app, gid)
        did = app.layer.get_goal(gid)["goal"]["pending_decisions"][0]["decision_id"]
        # After resolve, backend has no pending; observe should go idle and collect.
        backend._runs[run_id]["busy"] = False
        out = app.resolve(gid, did, {"verdict": "once", "reason": "tick after decide"})
        self.assertTrue(out.get("ok"), out)
        tick = out.get("tick") or {}
        self.assertTrue(isinstance(tick, dict), tick)
        self.assertIn(tick.get("action"), {"task_finished", "worker_running", "decision_required", "goal_finished"}, tick)
        # Pending decision cleared by durable resolve.
        pending = app.layer.get_goal(gid)["goal"].get("pending_decisions") or []
        self.assertEqual(pending, [])


    def test_status_and_events_expose_pending_decisions(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "sys_vis",
                "kind": "system_action",
                "run_id": run_id,
                "context_hash": "hv",
                "payload": {"proposal": "msi"},
            }
        )
        self._drive_until_decision(app, gid)
        st = app.status(gid)
        self.assertTrue(st.get("awaiting_decision"), st)
        self.assertEqual(st.get("pending_decision_count"), 1)
        row = (st.get("pending_decisions") or [])[0]
        self.assertEqual(row.get("kind"), "system_action_approval")
        self.assertEqual(row.get("backend_kind"), "system_action")
        self.assertEqual(row.get("backend_request_id"), "sys_vis")
        ev = app.events(gid)
        self.assertTrue(ev.get("awaiting_decision"), ev)
        self.assertEqual(ev.get("pending_decision_count"), 1)
        self.assertEqual((ev.get("pending_decisions") or [])[0].get("kind"), "system_action_approval")
        self.assertEqual((ev.get("pending") or [])[0].get("backend_kind"), "system_action")


    def test_list_requests_summarizes_awaiting_decision(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "perm_list",
                "kind": "permission",
                "run_id": run_id,
                "context_hash": "hl",
                "payload": {},
            }
        )
        self._drive_until_decision(app, gid)
        listing = app.list_requests()
        self.assertTrue(listing.get("ok"), listing)
        self.assertGreaterEqual(int(listing.get("awaiting_decision_count") or 0), 1)
        hit = next((r for r in (listing.get("requests") or []) if str(r.get("goal_id") or "") == gid), None)
        self.assertIsNotNone(hit, listing)
        assert hit is not None
        self.assertTrue(hit.get("awaiting_decision"), hit)
        self.assertGreaterEqual(int(hit.get("pending_decision_count") or 0), 1)



    def test_get_decisions_lists_pending(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "sys_getdec",
                "kind": "system_action",
                "run_id": run_id,
                "context_hash": "gd",
                "payload": {"action": "restart"},
            }
        )
        self._drive_until_decision(app, gid)
        listed = app.list_decisions(gid)
        self.assertTrue(listed.get("ok"), listed)
        self.assertTrue(listed.get("awaiting_decision"), listed)
        self.assertEqual(listed.get("pending_decision_count"), 1)
        row = (listed.get("pending_decisions") or [])[0]
        self.assertEqual(row.get("kind"), "system_action_approval")
        self.assertEqual(row.get("backend_kind"), "system_action")
        self.assertEqual(row.get("backend_request_id"), "sys_getdec")

        server = CollabHttpServer(("127.0.0.1", 0), app, api_token="")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/v1/requests/{gid}/decisions")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200, payload)
            self.assertTrue(payload.get("awaiting_decision"), payload)
            self.assertEqual(payload.get("pending_decision_count"), 1)
            self.assertEqual(
                (payload.get("pending_decisions") or [])[0].get("kind"),
                "system_action_approval",
            )
        finally:
            server.shutdown()
            server.server_close()


    def test_get_decision_by_id(self):
        app, backend, gid = self._boot()
        run_id = self._ensure_run(app, gid)
        backend.pending.append(
            {
                "request_id": "sys_getone",
                "kind": "system_action",
                "run_id": run_id,
                "context_hash": "g1",
                "payload": {"action": "restart"},
            }
        )
        self._drive_until_decision(app, gid)
        did = app.layer.get_goal(gid)["goal"]["pending_decisions"][0]["decision_id"]
        got = app.get_decision(gid, did)
        self.assertTrue(got.get("ok"), got)
        self.assertTrue(got.get("awaiting_decision"), got)
        self.assertEqual(got.get("pending_decision_count"), 1)
        row = got.get("decision") or {}
        self.assertEqual(row.get("kind"), "system_action_approval")
        self.assertEqual(row.get("backend_kind"), "system_action")
        self.assertEqual(row.get("backend_request_id"), "sys_getone")
        self.assertEqual(row.get("decision_id"), did)

        server = CollabHttpServer(("127.0.0.1", 0), app, api_token="")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/v1/requests/{gid}/decisions/{did}")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200, payload)
            self.assertEqual((payload.get("decision") or {}).get("kind"), "system_action_approval")
            conn.close()

            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/v1/requests/{gid}/decisions/does-not-exist")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 404, payload)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
