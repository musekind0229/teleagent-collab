"""Mock-HTTP unit tests for bin/hermes-collab-request.py (no live service)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "hermes-collab-request.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_collab_request", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HCR = _load_module()


class _FakeResp:
    def __init__(self, body: dict | str, status: int = 200):
        if isinstance(body, dict):
            self._raw = json.dumps(body).encode("utf-8")
        else:
            self._raw = str(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HermesCollabRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        # Point dotenv at a missing file so an empty token never opens a real
        # Hermes .env (tests that need a file override COLLAB_ENV_FILE).
        self._tmp = tempfile.TemporaryDirectory()
        missing = str(Path(self._tmp.name) / "missing.env")
        self._env = mock.patch.dict(
            os.environ,
            {
                "COLLAB_API_BASE": "http://127.0.0.1:8765",
                "COLLAB_API_TOKEN": "test-token",
                "COLLAB_ENV_FILE": missing,
                # Empty keeps the default ASCII JSON even if the developer shell
                # exported COLLAB_JSON_UNICODE. patch.dict restores the original.
                "COLLAB_JSON_UNICODE": "",
                # Empty keeps the default summary even if the shell exported
                # COLLAB_OUTPUT_FULL. patch.dict restores the original.
                "COLLAB_OUTPUT_FULL": "",
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("HERMES_HOME", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_open_posts_bearer_and_body(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["auth"] = req.headers.get("Authorization") or req.headers.get("authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(
                {
                    "ok": True,
                    "request_id": "g1",
                    "goal_id": "g1",
                    "state": "queued",
                },
                status=202,
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = HCR.request_json(
                "POST",
                "/v1/requests",
                body={"goal": "x", "acceptance": {"artifacts": ["a.md"]}},
            )
        self.assertEqual(captured["url"], "http://127.0.0.1:8765/v1/requests")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["auth"], "Bearer test-token")
        self.assertEqual(captured["body"]["goal"], "x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["request_id"], "g1")

    def test_status_encodes_unicode_id(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            return _FakeResp({"ok": True, "request_id": "目标-1", "state": "running"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ns = mock.Mock(request_id="目标-1", http_timeout=10.0)
            out = HCR.cmd_status(ns)
        self.assertIn("/v1/requests/", captured["url"])
        self.assertNotIn("目标", captured["url"])  # must be percent-encoded
        self.assertEqual(out["state"], "running")

    def test_report_get(self) -> None:
        def fake_urlopen(req, timeout=30):
            self.assertEqual(req.get_method(), "GET")
            self.assertTrue(req.full_url.endswith("/v1/requests/g1/report"))
            return _FakeResp({"ok": True, "report": {"summary": "done"}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ns = mock.Mock(request_id="g1", http_timeout=10.0)
            out = HCR.cmd_report(ns)
        self.assertTrue(out["ok"])

    def test_http_error_raises_client_error(self) -> None:
        err_body = json.dumps({"ok": False, "code": "unauthorized", "error": "unauthorized"}).encode()

        def fake_urlopen(req, timeout=30):
            raise HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(err_body),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(HCR.ClientError) as cm:
                HCR.request_json("GET", "/v1/requests/g1")
        self.assertEqual(cm.exception.payload.get("code"), "unauthorized")
        self.assertEqual(cm.exception.payload.get("http_status"), 401)

    def test_transport_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=URLError("refused")):
            with self.assertRaises(HCR.ClientError) as cm:
                HCR.request_json("GET", "/health")
        self.assertEqual(cm.exception.payload.get("code"), "transport_error")

    def test_wait_reaches_completed(self) -> None:
        states = iter(
            [
                {"ok": True, "state": "running", "request_id": "g1"},
                {"ok": True, "state": "completed", "request_id": "g1"},
            ]
        )

        def fake_urlopen(req, timeout=30):
            return _FakeResp(next(states))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(HCR.time, "sleep", return_value=None):
                ns = mock.Mock(request_id="g1", timeout=10.0, interval=0.01, http_timeout=5.0)
                out = HCR.cmd_wait(ns)
        self.assertEqual(out["state"], "completed")
        self.assertTrue(out["wait"]["terminal"])

    def test_wait_failed_exits_nonzero_via_main(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp({"ok": True, "state": "failed", "request_id": "g1", "need_human": False})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code = HCR.main(["wait", "g1", "--timeout", "5", "--interval", "0.01"])
        self.assertEqual(code, 2)

    def test_wait_timeout_exit_3(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp({"ok": True, "state": "running", "request_id": "g1"})

        # Force deadline immediately after first poll.
        mono = iter([100.0, 100.1, 999.0])

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(HCR.time, "monotonic", side_effect=lambda: next(mono)):
                with mock.patch.object(HCR.time, "sleep", return_value=None):
                    code = HCR.main(["wait", "g1", "--timeout", "1", "--interval", "0.01"])
        self.assertEqual(code, 3)

    def test_open_cli_builds_body_with_backend_hint(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"ok": True, "request_id": "g2", "state": "queued"}, status=202)

        argv = [
            "open",
            "--goal",
            "写一份说明",
            "--title",
            "demo",
            "--artifact",
            "out.md",
            "--backend",
            "antigravity",
            "--idempotency-key",
            "k-1",
        ]
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("sys.stdout", buf):
                code = HCR.main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(captured["body"]["goal"], "写一份说明")
        self.assertEqual(captured["body"]["acceptance"]["artifacts"], ["out.md"])
        # No synthesized prose criterion when --acceptance-text is absent.
        self.assertNotIn("text", captured["body"]["acceptance"])
        self.assertEqual(captured["body"]["caller_backend_hint"], "antigravity")
        self.assertEqual(captured["body"]["idempotency_key"], "k-1")
        printed = json.loads(buf.getvalue())
        self.assertEqual(printed["request_id"], "g2")

    def test_no_token_omits_authorization(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.headers)
            return _FakeResp({"ok": True})

        with mock.patch.dict("os.environ", {"COLLAB_API_TOKEN": ""}, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                HCR.request_json("GET", "/v1/requests/g1")
        auth_keys = [k for k in captured["headers"] if k.lower() == "authorization"]
        self.assertEqual(auth_keys, [])

    def _wait_main(self, argv: list[str], urlopen):
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            with mock.patch.object(HCR.time, "sleep", return_value=None) as slept:
                with mock.patch("sys.stdout", buf):
                    code = HCR.main(argv)
        printed = json.loads(buf.getvalue())
        return code, printed, slept

    def test_wait_pending_decisions_exits_4_immediately(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            return _FakeResp(
                {
                    "ok": True,
                    "state": "blocked",
                    "request_id": "g1",
                    "pending_decision_count": 1,
                    "awaiting_decision": True,
                    "pending_decisions": [
                        {
                            "decision_id": "dec-1",
                            "kind": "system_action_approval",
                            "title": "需要批准安装",
                            "task_id": "task-1",
                            "status": "pending",
                            "details": {"summary": "title 优先，这条不该进 summary"},
                        }
                    ],
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(calls, ["http://127.0.0.1:8765/v1/requests/g1"])
        self.assertEqual(printed["ok"], True)
        self.assertEqual(printed["code"], "need_human")
        self.assertIs(printed["need_human"], True)
        self.assertEqual(printed["state"], "blocked")
        wait = printed["wait"]
        self.assertEqual(wait["reason"], "pending_decisions")
        self.assertFalse(wait["terminal"])
        self.assertTrue(wait["need_human"])
        self.assertFalse(wait["timed_out"])
        self.assertEqual(wait["decision_ids"], ["dec-1"])
        self.assertEqual(wait["decisions"][0]["summary"], "需要批准安装")
        self.assertEqual(wait["decisions"][0]["reason"], "")
        self.assertEqual(wait["decisions"][0]["decision_id"], "dec-1")
        self.assertEqual(wait["decisions"][0]["kind"], "system_action_approval")
        self.assertEqual(wait["decisions"][0]["task_id"], "task-1")
        self.assertEqual(wait["decisions"][0]["status"], "pending")
        self.assertNotIn("task_ids", wait)

    def test_wait_awaiting_decision_fetches_decisions(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            if req.full_url.endswith("/decisions"):
                return _FakeResp(
                    {
                        "ok": True,
                        "pending_decisions": [
                            {
                                "decision_id": "dec-9",
                                "kind": "question",
                                "title": "",
                                "task_id": "t9",
                                "status": "open",
                                "details": {"question": "是否允许写文件？"},
                            }
                        ],
                    }
                )
            return _FakeResp(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "pending_decisions": [],
                    "pending_decision_count": 0,
                    "awaiting_decision": True,
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(
            calls,
            [
                "http://127.0.0.1:8765/v1/requests/g1",
                "http://127.0.0.1:8765/v1/requests/g1/decisions",
            ],
        )
        self.assertEqual(printed["code"], "need_human")
        self.assertEqual(printed["wait"]["reason"], "awaiting_decision")
        self.assertEqual(printed["wait"]["decision_ids"], ["dec-9"])
        self.assertEqual(printed["wait"]["decisions"][0]["summary"], "是否允许写文件？")
        self.assertEqual(printed["ok"], True)

    def test_wait_awaiting_decision_decisions_http_500_still_exits_4(self) -> None:
        def fake_urlopen(req, timeout=30):
            if req.full_url.endswith("/decisions"):
                raise HTTPError(
                    req.full_url,
                    500,
                    "Server Error",
                    hdrs=None,  # type: ignore[arg-type]
                    fp=io.BytesIO(b'{"ok": false, "error": "boom"}'),
                )
            return _FakeResp(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "pending_decisions": [],
                    "pending_decision_count": 1,
                    "awaiting_decision": True,
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(printed["code"], "need_human")
        self.assertEqual(printed["ok"], True)
        self.assertEqual(printed["wait"]["reason"], "awaiting_decision")
        self.assertEqual(printed["wait"]["decisions"], [])
        self.assertEqual(printed["wait"]["decision_ids"], [])

    def test_wait_task_awaiting_decision_exits_4(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            if req.full_url.endswith("/decisions"):
                return _FakeResp({"ok": True, "pending_decisions": []})
            return _FakeResp(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "pending_decisions": [],
                    "pending_decision_count": 0,
                    "awaiting_decision": False,
                    "tasks": [
                        {"task_id": "t-run", "status": "running"},
                        {"task_id": "t-wait", "status": "awaiting_decision"},
                        {"task_id": "t-also", "status": "awaiting_decision"},
                    ],
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 0)
        self.assertTrue(any(url.endswith("/decisions") for url in calls))
        self.assertEqual(printed["wait"]["reason"], "task_awaiting_decision")
        self.assertEqual(printed["wait"]["task_ids"], ["t-wait", "t-also"])
        self.assertEqual(printed["wait"]["decisions"], [])
        self.assertEqual(printed["wait"]["decision_ids"], [])
        self.assertEqual(printed["code"], "need_human")

    def test_wait_running_then_completed_exit_0(self) -> None:
        states = iter(
            [
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "pending_decisions": [],
                    "awaiting_decision": False,
                    "pending_decision_count": 0,
                    "tasks": [{"task_id": "t1", "status": "running"}],
                },
                {
                    "ok": True,
                    "state": "completed",
                    "request_id": "g1",
                    "pending_decisions": [],
                },
            ]
        )

        def fake_urlopen(req, timeout=30):
            self.assertNotIn("/decisions", req.full_url)
            return _FakeResp(next(states))

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "10", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 0)
        self.assertEqual(slept.call_count, 1)
        self.assertEqual(printed["state"], "completed")
        self.assertTrue(printed["wait"]["terminal"])
        self.assertNotEqual(printed.get("code"), "need_human")
        self.assertNotIn("need_human", printed["wait"])

    def test_wait_completed_with_pending_decisions_terminal_wins(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            return _FakeResp(
                {
                    "ok": True,
                    "state": "completed",
                    "request_id": "g1",
                    "awaiting_decision": True,
                    "pending_decision_count": 1,
                    "pending_decisions": [
                        {
                            "decision_id": "dec-old",
                            "kind": "question",
                            "title": "stale",
                        }
                    ],
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 0)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(calls, ["http://127.0.0.1:8765/v1/requests/g1"])
        self.assertEqual(printed["state"], "completed")
        self.assertTrue(printed["wait"]["terminal"])
        self.assertNotEqual(printed.get("code"), "need_human")

    def test_need_human_view_lead_rows_keep_polling(self) -> None:
        all_lead = HCR.need_human_view(
            {
                "state": "running",
                "awaiting_decision": True,
                "pending_decision_count": 2,
                "awaiting_lead_count": 2,
                "awaiting_human_count": 0,
                "pending_decisions": [
                    {
                        "decision_id": "d-lead",
                        "kind": "artifact_review",
                        "title": "review",
                        "awaiting": "lead",
                    },
                    {
                        "decision_id": "d-perm",
                        "kind": "action_approval",
                        "title": "perm",
                        "awaiting": "lead",
                    },
                ],
            }
        )
        self.assertIsNone(all_lead)

        mixed = HCR.need_human_view(
            {
                "state": "running",
                "pending_decisions": [
                    {
                        "decision_id": "h1",
                        "kind": "question",
                        "title": "ask",
                        "status": "pending",
                        "awaiting": "human",
                    },
                    {
                        "decision_id": "l1",
                        "kind": "artifact_review",
                        "title": "rev",
                        "awaiting": "lead",
                    },
                    {
                        "decision_id": "h2",
                        "kind": "system_action_approval",
                        "title": "sys",
                        "status": "pending",
                        "awaiting": "human",
                    },
                ],
            }
        )
        self.assertIsNotNone(mixed)
        assert mixed is not None
        self.assertEqual(mixed["reason"], "pending_decisions")
        self.assertEqual(mixed["decision_ids"], ["h1", "h2"])
        self.assertEqual([row["decision_id"] for row in mixed["decisions"]], ["h1", "h2"])
        self.assertEqual(mixed["decisions"][0]["awaiting"], "human")
        self.assertEqual(mixed["decisions"][1]["awaiting"], "human")
        self.assertEqual(mixed["lead_pending_ids"], ["l1"])

        old = HCR.need_human_view(
            {
                "state": "blocked",
                "pending_decisions": [
                    {
                        "decision_id": "dec-1",
                        "kind": "system_action_approval",
                        "title": "需要批准安装",
                        "task_id": "task-1",
                        "status": "pending",
                    }
                ],
            }
        )
        self.assertIsNotNone(old)
        assert old is not None
        self.assertEqual(old["reason"], "pending_decisions")
        self.assertEqual(old["decision_ids"], ["dec-1"])
        self.assertEqual(old["decisions"][0]["summary"], "需要批准安装")
        self.assertEqual(old["decisions"][0]["kind"], "system_action_approval")
        self.assertNotIn("awaiting", old["decisions"][0])
        self.assertNotIn("lead_pending_ids", old)

        mixed_old = HCR.need_human_view(
            {
                "state": "running",
                "pending_decisions": [
                    {"decision_id": "old-1", "kind": "question", "title": "旧服务"},
                    {
                        "decision_id": "lead-1",
                        "kind": "artifact_review",
                        "title": "rev",
                        "awaiting": "lead",
                    },
                ],
            }
        )
        self.assertIsNotNone(mixed_old)
        assert mixed_old is not None
        self.assertEqual(mixed_old["decision_ids"], ["old-1"])
        self.assertNotIn("awaiting", mixed_old["decisions"][0])
        self.assertEqual(mixed_old["lead_pending_ids"], ["lead-1"])

        self.assertIsNone(
            HCR.need_human_view(
                {
                    "state": "running",
                    "pending_decisions": [],
                    "pending_decision_count": 1,
                    "awaiting_decision": True,
                    "awaiting_human_count": 0,
                    "awaiting_lead_count": 2,
                }
            )
        )
        old_counts = HCR.need_human_view(
            {
                "state": "running",
                "pending_decisions": [],
                "pending_decision_count": 1,
                "awaiting_decision": True,
            }
        )
        self.assertIsNotNone(old_counts)
        assert old_counts is not None
        self.assertEqual(old_counts["reason"], "awaiting_decision")
        self.assertEqual(old_counts["decisions"], [])
        self.assertNotIn("lead_pending_ids", old_counts)
        human_counts = HCR.need_human_view(
            {
                "state": "running",
                "awaiting_decision": True,
                "pending_decision_count": 2,
                "awaiting_human_count": 1,
                "awaiting_lead_count": 1,
            }
        )
        self.assertIsNotNone(human_counts)
        assert human_counts is not None
        self.assertEqual(human_counts["reason"], "awaiting_decision")
        missing_lead = HCR.need_human_view(
            {
                "state": "running",
                "awaiting_decision": True,
                "pending_decision_count": 1,
                "awaiting_human_count": 0,
            }
        )
        self.assertIsNotNone(missing_lead)
        assert missing_lead is not None
        self.assertEqual(missing_lead["reason"], "awaiting_decision")

    def test_wait_lead_pending_then_completed_exits_0(self) -> None:
        states = iter(
            [
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "awaiting_decision": True,
                    "pending_decision_count": 1,
                    "awaiting_lead_count": 1,
                    "awaiting_human_count": 0,
                    "pending_decisions": [
                        {
                            "decision_id": "d-lead",
                            "kind": "artifact_review",
                            "title": "TeleAgent review",
                            "status": "pending",
                            "awaiting": "lead",
                        }
                    ],
                },
                {
                    "ok": True,
                    "state": "completed",
                    "request_id": "g1",
                    "pending_decisions": [],
                    "awaiting_lead_count": 0,
                    "awaiting_human_count": 0,
                },
            ]
        )

        def fake_urlopen(req, timeout=30):
            self.assertNotIn("/decisions", req.full_url)
            return _FakeResp(next(states))

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 0)
        self.assertEqual(slept.call_count, 1)
        self.assertEqual(printed["state"], "completed")
        self.assertTrue(printed["wait"]["terminal"])
        self.assertNotEqual(printed.get("code"), "need_human")

    def test_wait_lead_pending_then_human_exits_4(self) -> None:
        states = iter(
            [
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "awaiting_decision": True,
                    "pending_decision_count": 1,
                    "awaiting_lead_count": 1,
                    "awaiting_human_count": 0,
                    "pending_decisions": [
                        {
                            "decision_id": "d-lead",
                            "kind": "artifact_review",
                            "title": "TeleAgent review",
                            "status": "pending",
                            "awaiting": "lead",
                        }
                    ],
                },
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "awaiting_decision": True,
                    "pending_decision_count": 1,
                    "awaiting_lead_count": 0,
                    "awaiting_human_count": 1,
                    "pending_decisions": [
                        {
                            "decision_id": "d-lead",
                            "kind": "artifact_review",
                            "title": "TeleAgent review",
                            "status": "pending",
                            "awaiting": "human",
                            "reason": "quota",
                            "lead_error": {
                                "code": "quota",
                                "retryable": False,
                                "message": "quota",
                            },
                        }
                    ],
                },
            ]
        )

        def fake_urlopen(req, timeout=30):
            return _FakeResp(next(states))

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 1)
        self.assertEqual(printed["code"], "need_human")
        wait = printed["wait"]
        self.assertEqual(wait["reason"], "pending_decisions")
        self.assertEqual(wait["decision_ids"], ["d-lead"])
        self.assertEqual(wait["decisions"][0]["awaiting"], "human")
        self.assertEqual(wait["decisions"][0]["summary"], "TeleAgent review")
        self.assertFalse(wait["timed_out"])
        self.assertNotIn("lead_pending_ids", wait)

    def test_wait_mixed_emits_human_only_and_lead_ids(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "pending_decisions": [
                        {
                            "decision_id": "h1",
                            "kind": "question",
                            "title": "ask",
                            "awaiting": "human",
                        },
                        {
                            "decision_id": "l1",
                            "kind": "artifact_review",
                            "title": "rev",
                            "awaiting": "lead",
                        },
                    ],
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(printed["wait"]["decision_ids"], ["h1"])
        self.assertEqual(printed["wait"]["lead_pending_ids"], ["l1"])
        self.assertEqual(printed["wait"]["decisions"][0]["awaiting"], "human")
        self.assertEqual(len(printed["wait"]["decisions"]), 1)

    def test_wait_lead_pending_times_out_exit_3(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "awaiting_decision": True,
                    "pending_decision_count": 1,
                    "awaiting_lead_count": 1,
                    "awaiting_human_count": 0,
                    "pending_decisions": [
                        {
                            "decision_id": "d-lead",
                            "kind": "permission",
                            "title": "perm",
                            "awaiting": "lead",
                        }
                    ],
                }
            )

        mono = iter([100.0, 101.0])
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(HCR.time, "monotonic", side_effect=lambda: next(mono)):
                with mock.patch.object(HCR.time, "sleep", return_value=None) as slept:
                    with mock.patch("sys.stdout", buf):
                        code = HCR.main(["wait", "g1", "--timeout", "1", "--interval", "0.2"])
        printed = json.loads(buf.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(slept.call_count, 0)
        self.assertTrue(printed["wait"]["timed_out"])
        self.assertNotEqual(printed.get("code"), "need_human")
        self.assertEqual(printed["wait"]["timeout_sec"], 1.0)

    def test_need_human_view_summary_truncate_and_fallback(self) -> None:
        long_summary = "第一行\n" + ("测" * 250)
        truncated = HCR.need_human_view(
            {
                "state": "blocked",
                "pending_decisions": [
                    {
                        "decision_id": "d-long",
                        "kind": "question",
                        "title": " \n\t",
                        "task_id": "t1",
                        "status": "pending",
                        "details": {"summary": long_summary, "message": "should-not-win"},
                    }
                ],
            }
        )
        self.assertIsNotNone(truncated)
        assert truncated is not None
        summary = truncated["decisions"][0]["summary"]
        self.assertEqual(summary, ("第一行 " + ("测" * 250))[:200])
        self.assertEqual(len(summary), 200)
        self.assertNotIn("\n", summary)
        self.assertNotIn("should-not-win", summary)

        lead = HCR.need_human_view(
            {
                "state": "blocked",
                "pending_decisions": [
                    {
                        "decision_id": "d-lead",
                        "kind": "question",
                        "title": "",
                        "details": {"summary": "  ", "message": "", "reason": None, "question": "\n"},
                        "lead_error": {"message": "组长失败\n请看日志"},
                    }
                ],
            }
        )
        self.assertIsNotNone(lead)
        assert lead is not None
        self.assertEqual(lead["decisions"][0]["summary"], "组长失败 请看日志")

        fallback = HCR.need_human_view(
            {
                "state": "running",
                "awaiting_decision": True,
                "pending_decisions": [
                    {
                        "decision_id": "d-kind",
                        "kind": "system_action_approval",
                        "title": "",
                        "details": {"summary": "", "message": "  ", "reason": "", "question": ""},
                        "lead_error": {"message": "   "},
                    }
                ],
            }
        )
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback["reason"], "pending_decisions")
        self.assertEqual(fallback["decisions"][0]["summary"], "system_action_approval")
        self.assertEqual(fallback["decisions"][0]["reason"], "")
        self.assertIsNone(
            HCR.need_human_view(
                {
                    "state": "completed",
                    "pending_decisions": [
                        {"decision_id": "d", "kind": "question", "title": "ignored"}
                    ],
                }
            )
        )

    def test_decision_summary_skips_kind_title(self) -> None:
        kind = "escalate_over_budget"
        with_reason = {
            "decision_id": "d-reason",
            "kind": kind,
            "title": kind,
            "task_id": "T",
            "status": "pending",
            "reason": "预算用尽\n需要加人",
        }
        self.assertEqual(HCR._decision_summary(with_reason), "预算用尽 需要加人")
        brief = HCR._decision_brief(with_reason)
        self.assertEqual(brief["summary"], "预算用尽 需要加人")
        self.assertEqual(brief["reason"], "预算用尽\n需要加人")
        self.assertIn("reason", brief)

        with_details = {
            "decision_id": "d-details",
            "kind": kind,
            "title": kind,
            "reason": "row reason 不该盖过 details.summary",
            "details": {"summary": "细节里的摘要", "message": "也不该赢"},
            "lead_error": {"message": "lead 更不该赢"},
        }
        self.assertEqual(HCR._decision_summary(with_details), "细节里的摘要")
        self.assertEqual(
            HCR._decision_brief(with_details)["reason"],
            "row reason 不该盖过 details.summary",
        )

        meaningful = {
            "kind": "system_action_approval",
            "title": "需要批准安装",
            "reason": "不应盖过有意义的 title",
            "details": {"summary": "也不应盖过 title"},
        }
        self.assertEqual(HCR._decision_summary(meaningful), "需要批准安装")

        only_kind = {"kind": kind}
        only_brief = HCR._decision_brief(only_kind)
        self.assertEqual(only_brief["summary"], kind)
        self.assertEqual(only_brief["reason"], "")

        same_title = {
            "kind": kind,
            "title": kind,
            "reason": "",
            "details": {"summary": "  ", "message": "", "reason": None, "question": ""},
            "lead_error": {"message": "   "},
        }
        self.assertEqual(HCR._decision_summary(same_title), kind)

        view = HCR.need_human_view(
            {"state": "blocked", "pending_decisions": [with_reason]}
        )
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view["decisions"][0]["summary"], "预算用尽 需要加人")
        self.assertEqual(view["decisions"][0]["reason"], "预算用尽\n需要加人")
        self.assertEqual(view["decisions"][0]["kind"], kind)
        self.assertEqual(view["decisions"][0]["title"], kind)

    def test_native_review_summary_from_payload(self) -> None:
        preview = "hello\u200b「AI生成」watermark-preview"
        tool_secret = "TOOL_INPUT_SECRET_DO_NOT_LEAK"
        row = {
            "decision_id": "d-review",
            "kind": "artifact_review",
            "title": "TeleAgent review",
            "status": "pending",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "review",
                "backend_request_id": "req-review",
                "context_hash": "abc",
                "payload": {
                    "artifacts": {
                        "hello.txt": {
                            "sha256": "deadbeef",
                            "bytes": 10777,
                            "preview": preview,
                            "truncated": False,
                        }
                    },
                    "tools": [
                        {
                            "tool": "write",
                            "status": "completed",
                            "input": {"content": tool_secret},
                            "output": preview,
                        },
                        {"tool": "read", "status": "completed", "input": {"path": "hello.txt"}},
                        {"tool": "report_final_files", "status": "completed", "input": {}},
                    ],
                    "policy_violations": [],
                    "approved_permissions": 0,
                    "finish": "stop",
                },
            },
        }
        summary = HCR._decision_summary(row)
        self.assertEqual(
            summary,
            "review: artifacts hello.txt(10777B); tools write,read,report_final_files; finish=stop; violations=0",
        )
        self.assertNotIn(preview, summary)
        self.assertNotIn("\u200b", summary)
        self.assertNotIn("AI", summary)
        self.assertNotIn(tool_secret, summary)
        self.assertNotIn("sha256", summary)
        self.assertNotIn("deadbeef", summary)

        with_details = {
            **row,
            "details": {**row["details"], "summary": "细节里的摘要"},
        }
        self.assertEqual(HCR._decision_summary(with_details), "细节里的摘要")
        meaningful_title = {**row, "title": "请复核 hello.txt"}
        self.assertEqual(HCR._decision_summary(meaningful_title), "请复核 hello.txt")

    def test_review_summary_prefixes_contamination(self) -> None:
        scan = {
            "contaminated": True,
            "aigc_marks": {"AI生成": 1},
            "invisible": {"U+200B": 1926, "U+200D": 1658},
        }
        payload = {
            "artifacts": {
                "hello.txt": {
                    "bytes": 10777,
                    "preview": "hello\u200b「AI生成」",
                    "contamination": scan,
                }
            },
            "tools": [
                {"tool": "write", "status": "completed", "input": {"content": "secret"}},
                {"tool": "read", "status": "completed"},
            ],
            "finish": "stop",
            "policy_violations": [],
        }
        direct = HCR._review_payload_summary(payload)
        self.assertEqual(
            direct,
            "CONTAMINATED hello.txt: AI生成x1, U+200Bx1926, U+200Dx1658; "
            "review: artifacts hello.txt(10777B); tools write,read; finish=stop; violations=0",
        )
        assert direct is not None
        self.assertLessEqual(len(direct), 200)
        self.assertNotIn("\u200b", direct)
        self.assertNotIn("secret", direct)
        row = {
            "kind": "artifact_review",
            "title": "TeleAgent review (CONTAMINATED)",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "review",
                "summary": "细节不该盖过污染前缀",
                "payload": payload,
            },
        }
        summary = HCR._decision_summary(row)
        self.assertTrue(summary.startswith("CONTAMINATED hello.txt: AI生成x1, U+200Bx1926, U+200Dx1658; "))
        self.assertLess(summary.find("CONTAMINATED"), summary.find("review:"))
        self.assertLessEqual(len(summary), 200)
        long_payload = {
            "artifacts": payload["artifacts"],
            "tools": [{"tool": f"toolname{i:02d}", "status": "completed"} for i in range(40)],
            "finish": "stop",
            "policy_violations": [],
        }
        truncated = HCR._review_payload_summary(long_payload)
        assert truncated is not None
        self.assertTrue(truncated.startswith("CONTAMINATED hello.txt:"))
        self.assertLessEqual(len(truncated), 200)

    def test_native_permission_summary_from_payload(self) -> None:
        row = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "permission",
                "payload": {
                    "id": "perm-1",
                    "permission": "external_directory",
                    "patterns": ["*.md", 3, None, "src/*.py"],
                    "metadata": {"token": "do-not-include"},
                    "always": ["*"],
                    "input": {"command": "secret-cmd"},
                },
            },
        }
        summary = HCR._decision_summary(row)
        self.assertEqual(summary, "permission: external_directory *.md, src/*.py")
        self.assertNotIn("do-not-include", summary)
        self.assertNotIn("secret-cmd", summary)
        self.assertNotIn("perm-1", summary)

        no_patterns = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "permission",
                "payload": {"permission": "edit", "metadata": {"k": "v"}},
            },
        }
        self.assertEqual(HCR._decision_summary(no_patterns), "permission: edit")

    def test_permission_scope_summary_keeps_not_only_pinned(self) -> None:
        only = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "permission",
                "summary": "permission: external_directory C:/d/0/*",
                "payload": {"permission": "external_directory", "patterns": ["C:/d/0/*"]},
                "scope": [{
                    "pattern": "C:/d/0/*",
                    "directory": "C:/d/0",
                    "files": ["ext-input.txt"],
                    "only_pinned": True,
                }],
            },
        }
        self.assertEqual(
            HCR._decision_summary(only),
            "permission: external_directory C:/d/0/* [only pinned] -> dir contains 1 file(s): ext-input.txt",
        )
        self.assertEqual(
            HCR._permission_payload_summary(
                {"permission": "external_directory"},
                [{
                    "pattern": "C:/d/*",
                    "files": ["a", "b", "c"],
                    "only_pinned": False,
                }],
            ),
            "permission: external_directory C:/d/* [NOT ONLY PINNED] -> dir contains 3 file(s): a, b, c",
        )
        flagged = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "permission",
                "summary": "permission: external_directory C:/d/*",
                "payload": {
                    "permission": "external_directory",
                    "patterns": ["C:/d/*"],
                    "metadata": {"token": "do-not-include"},
                },
                "scope": [{
                    "pattern": "C:/d/*",
                    "files": ["a", "b", "c"],
                    "only_pinned": False,
                }],
            },
        }
        summary = HCR._decision_summary(flagged)
        self.assertEqual(
            summary,
            "permission: external_directory C:/d/* [NOT ONLY PINNED] -> dir contains 3 file(s): a, b, c",
        )
        self.assertNotIn("do-not-include", summary)
        long_files = [f"file{i}.txt" for i in range(40)]
        long_row = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "permission",
                "summary": (
                    "permission: external_directory C:/d/* -> dir contains 40 file(s): "
                    + ", ".join(long_files)
                    + " [NOT ONLY PINNED]"
                ),
                "payload": {"permission": "external_directory", "patterns": ["C:/d/*"]},
                "scope": [{
                    "pattern": "C:/d/*",
                    "files": long_files,
                    "only_pinned": False,
                }],
            },
        }
        truncated = HCR._decision_summary(long_row)
        self.assertLessEqual(len(truncated), 200)
        self.assertIn("NOT ONLY PINNED", truncated)
        self.assertIn("file0.txt", truncated)
        self.assertLess(truncated.find("NOT ONLY PINNED"), truncated.find("file"))

    def test_permission_scope_summary_keeps_only_pinned_for_long_pattern(self) -> None:
        tail = "/0/*"
        head = "C:/Users/"
        pattern = head + ("n" * (180 - len(head) - len(tail))) + tail
        self.assertEqual(len(pattern), 180)
        row = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "permission",
                "payload": {"permission": "external_directory", "patterns": [pattern]},
                "scope": [{
                    "pattern": pattern,
                    "files": ["ext-input.txt"],
                    "only_pinned": True,
                }],
            },
        }
        summary = HCR._decision_summary(row)
        self.assertLessEqual(len(summary), 200)
        self.assertNotIn(pattern, summary)
        head_part, flag, tail_part = summary.partition(" [only pinned]")
        self.assertEqual(flag, " [only pinned]")
        self.assertTrue(head_part.startswith("permission: external_directory C:/"))
        self.assertIn("\u2026", head_part)
        self.assertTrue(head_part.endswith("/0/*"))
        self.assertIn("dir contains 1 file(s): ext-input.txt", tail_part)
        self.assertLess(summary.find("[only pinned]"), summary.find("ext-input.txt"))

    def test_native_question_summary_from_payload(self) -> None:
        row = {
            "kind": "question",
            "title": "TeleAgent question",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "question",
                "payload": {
                    "id": "q1",
                    "questions": [
                        {
                            "question": "Which file?\nhello or notes",
                            "header": "which?",
                            "options": ["a", "b"],
                        },
                        {"question": "second should not win"},
                    ],
                },
            },
        }
        self.assertEqual(
            HCR._decision_summary(row),
            "question: Which file? hello or notes",
        )

        header_only = {
            "kind": "question",
            "title": "TeleAgent question",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "question",
                "payload": {"questions": [{"header": "continue?", "options": ["yes"]}]},
            },
        }
        self.assertEqual(HCR._decision_summary(header_only), "question: continue?")

        flat = {
            "kind": "question",
            "title": "TeleAgent question",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {"backend_kind": "question", "payload": {"question": "Proceed with install?"}},
        }
        self.assertEqual(HCR._decision_summary(flat), "question: Proceed with install?")

    def test_native_system_action_summary_from_payload(self) -> None:
        row = {
            "kind": "system_action_approval",
            "title": "TeleAgent system_action",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "system_action",
                "payload": {
                    "proposal": {
                        "type": "msi_install",
                        "elevation": "runas",
                        "package": {"filename": "test-package.msi", "sha256": "abc"},
                        "arguments": ["/qn"],
                    },
                    "proposal_sha256": "zzz",
                    "preapproval_tools": [{"tool": "write", "input": {"secret": "NOPE"}}],
                },
            },
        }
        summary = HCR._decision_summary(row)
        self.assertEqual(summary, "system_action: msi_install test-package.msi")
        self.assertNotIn("NOPE", summary)
        self.assertNotIn("/qn", summary)
        self.assertNotIn("runas", summary)

        flat = {
            "kind": "system_action_approval",
            "title": "TeleAgent system_action",
            "reason": "TeleAgent worker requires a bounded decision",
            "details": {
                "backend_kind": "system_action",
                "payload": {
                    "type": "msi_install",
                    "package": {"filename": "setup.msi", "sha256": "ff"},
                    "arguments": ["/qn"],
                },
            },
        }
        self.assertEqual(HCR._decision_summary(flat), "system_action: msi_install setup.msi")

    def test_native_malformed_payload_falls_back(self) -> None:
        generic_title = "TeleAgent review"
        generic_reason = "TeleAgent worker requires a bounded decision"
        payloads = [
            ["not", "a", "dict"],
            "review-blob",
            None,
            {
                "artifacts": "hello.txt",
                "tools": 5,
                "policy_violations": "nope",
                "finish": {"no": "dict"},
            },
            {"artifacts": None, "tools": None, "policy_violations": None, "finish": None},
            {},
        ]
        for payload in payloads:
            row = {
                "kind": "artifact_review",
                "title": generic_title,
                "reason": generic_reason,
                "details": {"backend_kind": "review", "payload": payload},
            }
            self.assertEqual(HCR._decision_summary(row), generic_title)

        odd_question = {
            "kind": "question",
            "title": "TeleAgent question",
            "reason": generic_reason,
            "details": {
                "backend_kind": "question",
                "payload": {"questions": [1, "nope"], "question": {"nested": True}},
            },
        }
        self.assertEqual(HCR._decision_summary(odd_question), "TeleAgent question")

        odd_permission = {
            "kind": "action_approval",
            "title": "TeleAgent permission",
            "reason": generic_reason,
            "details": {
                "backend_kind": "permission",
                "payload": {"permission": ["edit"], "patterns": {"a": 1}},
            },
        }
        self.assertEqual(HCR._decision_summary(odd_permission), "TeleAgent permission")

        # Non-native rows keep the previous candidate order.
        non_native = {
            "kind": "escalate_over_budget",
            "title": "escalate_over_budget",
            "reason": "预算用尽",
            "details": {
                "backend_kind": "antigravity",
                "payload": {"permission": "edit", "patterns": ["*"]},
            },
        }
        self.assertEqual(HCR._decision_summary(non_native), "预算用尽")

    def test_collab_env_file_supplies_bearer(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["auth"] = req.headers.get("Authorization")
            return _FakeResp({"ok": True, "request_id": "g1", "state": "running"})

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text('COLLAB_API_TOKEN="dotenv-tok"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"COLLAB_ENV_FILE": str(env_file)}, clear=False):
                os.environ.pop("COLLAB_API_TOKEN", None)
                os.environ.pop("HERMES_HOME", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    HCR.request_json("GET", "/v1/requests/g1")
        self.assertTrue(
            captured.get("auth") == "Bearer dotenv-tok",
            "expected Bearer token from COLLAB_ENV_FILE",
        )

    def test_hermes_home_dotenv_supplies_bearer(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["auth"] = req.headers.get("Authorization")
            return _FakeResp({"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text("COLLAB_API_TOKEN=home-tok\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                os.environ.pop("COLLAB_API_TOKEN", None)
                os.environ.pop("COLLAB_ENV_FILE", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    HCR.request_json("GET", "/v1/requests/g1")
        self.assertTrue(
            captured.get("auth") == "Bearer home-tok",
            "expected Bearer token from HERMES_HOME/.env",
        )

    def test_process_env_overrides_dotenv(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["auth"] = req.headers.get("Authorization")
            return _FakeResp({"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text('COLLAB_API_TOKEN="dotenv-tok"\n', encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"COLLAB_API_TOKEN": "env-tok", "COLLAB_ENV_FILE": str(env_file)},
                clear=False,
            ):
                os.environ.pop("HERMES_HOME", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    HCR.request_json("GET", "/v1/requests/g1")
        self.assertTrue(
            captured.get("auth") == "Bearer env-tok",
            "process environment should beat dotenv",
        )

    def test_dotenv_api_base_used(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            return _FakeResp({"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "COLLAB_API_BASE=http://127.0.0.1:9999\nOTHER_FROM_DOTENV=nope\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"COLLAB_ENV_FILE": str(env_file)}, clear=False):
                os.environ.pop("COLLAB_API_BASE", None)
                os.environ.pop("HERMES_HOME", None)
                os.environ.pop("OTHER_FROM_DOTENV", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    HCR.request_json("GET", "/v1/requests/g1")
                self.assertNotIn("OTHER_FROM_DOTENV", os.environ)
        self.assertEqual(captured["url"], "http://127.0.0.1:9999/v1/requests/g1")

    def test_read_dotenv_comments_quotes_export_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            body = (
                "# comment only\n"
                "# COLLAB_API_TOKEN=not-this\n"
                "\n"
                "   \n"
                "  # indented comment\n"
                "export COLLAB_API_BASE='http://127.0.0.1:9999'\n"
                "UNQUOTED=hello # inline comment\n"
                'DOUBLE="keep # inside"\n'
                "SINGLE='a b'\n"
                "NO_EQUALS this line\n"
                "export TRAIL=tail # gone\n"
                "DOLLAR=$COLLAB_API_TOKEN\n"
            )
            path.write_bytes(b'\xef\xbb\xbfCOLLAB_API_TOKEN="dotenv-tok"\n' + body.encode("utf-8"))
            data = HCR._read_dotenv(path)
            self.assertEqual(HCR._read_dotenv(Path(tmp) / "absent.env"), {})
        self.assertEqual(data["COLLAB_API_TOKEN"], "dotenv-tok")
        self.assertNotIn("\ufeffCOLLAB_API_TOKEN", data)
        self.assertEqual(data["COLLAB_API_BASE"], "http://127.0.0.1:9999")
        self.assertEqual(data["UNQUOTED"], "hello")
        self.assertEqual(data["DOUBLE"], "keep # inside")
        self.assertEqual(data["SINGLE"], "a b")
        self.assertEqual(data["TRAIL"], "tail")
        self.assertEqual(data["DOLLAR"], "$COLLAB_API_TOKEN")
        self.assertNotIn("NO_EQUALS", data)
        self.assertNotIn("not-this", list(data.values()))
        self.assertNotIn("\ufeff", "".join(data))

    def test_missing_dotenv_omits_authorization(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["headers"] = dict(req.headers)
            return _FakeResp({"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.env")
            with mock.patch.dict(os.environ, {"COLLAB_ENV_FILE": missing}, clear=False):
                os.environ.pop("COLLAB_API_TOKEN", None)
                os.environ.pop("HERMES_HOME", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    out = HCR.request_json("GET", "/v1/requests/g1")
        self.assertTrue(out["ok"])
        auth_keys = [k for k in captured["headers"] if k.lower() == "authorization"]
        self.assertEqual(auth_keys, [])

    def test_401_dotenv_source_hides_token(self) -> None:
        token = "dotenv-tok"
        seen: dict = {}

        def fake_urlopen(req, timeout=30):
            seen["auth"] = req.headers.get("Authorization")
            err_body = json.dumps(
                {"ok": False, "code": "unauthorized", "error": "unauthorized"}
            ).encode()
            raise HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(err_body),
            )

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f'COLLAB_API_TOKEN="{token}"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"COLLAB_ENV_FILE": str(env_file)}, clear=False):
                os.environ.pop("COLLAB_API_TOKEN", None)
                os.environ.pop("HERMES_HOME", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with mock.patch("sys.stdout", buf):
                        code = HCR.main(["status", "g1"])
        text = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn(token, text)
        self.assertTrue(seen.get("auth") == f"Bearer {token}", "401 request missing dotenv bearer")
        payload = json.loads(text)
        self.assertEqual(payload["auth"]["token_source"], "dotenv")
        self.assertEqual(set(payload["auth"]), {"token_source", "env_file"})

    def test_401_without_token_source_is_none(self) -> None:
        def fake_urlopen(req, timeout=30):
            auth_keys = [k for k in req.headers if k.lower() == "authorization"]
            if auth_keys:
                raise AssertionError("Authorization header present without a token")
            raise HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"ok": false, "code": "unauthorized", "error": "unauthorized"}'),
            )

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.env")
            with mock.patch.dict(os.environ, {"COLLAB_ENV_FILE": missing}, clear=False):
                os.environ.pop("COLLAB_API_TOKEN", None)
                os.environ.pop("HERMES_HOME", None)
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with mock.patch("sys.stdout", buf):
                        code = HCR.main(["status", "g1"])
        text = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn("test-token", text)
        payload = json.loads(text)
        self.assertEqual(payload["auth"]["token_source"], "none")
        self.assertEqual(payload["auth"]["env_file"], missing)

    def test_dotenv_path_windows_localappdata(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": r"C:\Users\Admin\AppData\Local"},
            clear=False,
        ):
            os.environ.pop("COLLAB_ENV_FILE", None)
            os.environ.pop("HERMES_HOME", None)
            with mock.patch.object(HCR.os, "name", "nt"):
                got = HCR._dotenv_path()
        self.assertEqual(
            got,
            Path(r"C:\Users\Admin\AppData\Local") / "hermes" / ".env",
        )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLAB_ENV_FILE", None)
            os.environ.pop("HERMES_HOME", None)
            os.environ.pop("LOCALAPPDATA", None)
            with mock.patch.object(HCR.os, "name", "nt"):
                missing = HCR._dotenv_path()
        self.assertIsNone(missing)

    def _run_main(self, argv: list[str], urlopen, env: dict | None = None):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                with mock.patch("sys.stdout", buf):
                    code = HCR.main(argv)
        return code, buf.getvalue()

    def test_status_default_stdout_is_ascii_json(self) -> None:
        outcome = "在工作区写 hello.txt"

        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {"ok": True, "state": "running", "goal": {"desired_outcome": outcome}}
            )

        code, text = self._run_main(["status", "ID", "--full"], fake_urlopen)
        self.assertEqual(code, 0)
        self.assertTrue(text.isascii())
        self.assertIn("\\u5728", text)
        self.assertNotIn(outcome, text)
        self.assertEqual(json.loads(text)["goal"]["desired_outcome"], outcome)

    def test_http_404_chinese_error_stdout_is_ascii(self) -> None:
        message = "找不到该请求"

        def fake_urlopen(req, timeout=30):
            body = json.dumps(
                {"ok": False, "code": "not_found", "error": message}
            ).encode("utf-8")
            raise HTTPError(
                req.full_url,
                404,
                "Not Found",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(body),
            )

        code, text = self._run_main(["status", "missing"], fake_urlopen)
        self.assertEqual(code, 1)
        self.assertTrue(text.isascii())
        self.assertNotIn(message, text)
        payload = json.loads(text)
        self.assertEqual(payload["error"], message)
        self.assertEqual(payload["http_status"], 404)

    def test_unicode_flag_and_env_emit_raw_chinese(self) -> None:
        outcome = "在工作区写 hello.txt"

        def fake_urlopen(req, timeout=30):
            return _FakeResp({"ok": True, "goal": {"desired_outcome": outcome}})

        for argv, env in (
            (["--unicode", "status", "ID", "--full"], None),
            (["--unicode", "status", "ID", "--full"], {"COLLAB_JSON_UNICODE": "0"}),
            (["status", "ID", "--full"], {"COLLAB_JSON_UNICODE": "1"}),
            (["status", "ID", "--full"], {"COLLAB_JSON_UNICODE": "true"}),
            (["status", "ID", "--full"], {"COLLAB_JSON_UNICODE": "yes"}),
            (["status", "ID", "--full"], {"COLLAB_JSON_UNICODE": "on"}),
            (["status", "ID", "--full"], {"COLLAB_JSON_UNICODE": "YES"}),
        ):
            code, text = self._run_main(argv, fake_urlopen, env)
            self.assertEqual(code, 0, msg=f"argv={argv} env={env}")
            self.assertIn(outcome, text)
            self.assertEqual(json.loads(text)["goal"]["desired_outcome"], outcome)

        for env in ({"COLLAB_JSON_UNICODE": "0"}, {"COLLAB_JSON_UNICODE": ""}):
            code, text = self._run_main(["status", "ID", "--full"], fake_urlopen, env)
            self.assertEqual(code, 0, msg=repr(env))
            self.assertTrue(text.isascii(), msg=repr(env))
            self.assertIn("\\u5728", text)
            self.assertNotIn(outcome, text)
            self.assertEqual(json.loads(text)["goal"]["desired_outcome"], outcome)

    def test_emit_default_cp936_and_ascii_streams(self) -> None:
        payload = {"goal": {"desired_outcome": "在工作区写 hello.txt"}}
        raw = io.BytesIO()
        cp936 = io.TextIOWrapper(raw, encoding="cp936")
        HCR._emit(payload, stream=cp936)
        cp936.flush()
        text = raw.getvalue().decode("ascii")
        self.assertTrue(text.isascii())
        self.assertIn("\\u5728", text)
        loaded = json.loads(text)
        self.assertEqual(loaded["goal"]["desired_outcome"], "在工作区写 hello.txt")

        raw_ascii = io.BytesIO()
        ascii_stream = io.TextIOWrapper(raw_ascii, encoding="ascii")
        HCR._emit(payload, stream=ascii_stream)
        ascii_stream.flush()
        ascii_text = raw_ascii.getvalue().decode("ascii")
        self.assertEqual(
            json.loads(ascii_text)["goal"]["desired_outcome"],
            payload["goal"]["desired_outcome"],
        )

        odd = io.StringIO()

        class _Odd:
            def __str__(self) -> str:
                return "odd-value"

        HCR._emit({"x": _Odd()}, stream=odd)
        self.assertEqual(json.loads(odd.getvalue())["x"], "odd-value")

    def test_main_stringio_without_reconfigure(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp({"ok": True, "state": "queued", "request_id": "g1"})

        out = io.StringIO()
        err = io.StringIO()
        self.assertFalse(hasattr(out, "reconfigure"))
        self.assertFalse(hasattr(err, "reconfigure"))
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                code = HCR.main(["status", "g1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["request_id"], "g1")

    def test_reconfigure_failure_is_ignored(self) -> None:
        class _Boom(io.StringIO):
            def reconfigure(self, **kwargs):
                raise OSError("cannot reconfigure")

        out, err = _Boom(), _Boom()
        outcome = "在工作区写 hello.txt"

        def fake_urlopen(req, timeout=30):
            return _FakeResp({"ok": True, "goal": {"desired_outcome": outcome}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                code = HCR.main(["--unicode", "status", "--full", "ID"])
        self.assertEqual(code, 0)
        self.assertIn(outcome, out.getvalue())

    def test_main_reconfigure_errors_then_optional_utf8(self) -> None:
        class _Rec(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[dict] = []

            def reconfigure(self, **kwargs):
                self.calls.append(dict(kwargs))

        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {"ok": True, "goal": {"desired_outcome": "在工作区写 hello.txt"}}
            )

        out, err = _Rec(), _Rec()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                code = HCR.main(["status", "--full", "ID"])
        self.assertEqual(code, 0)
        self.assertEqual(out.calls, [{"errors": "backslashreplace"}])
        self.assertEqual(err.calls, [{"errors": "backslashreplace"}])
        self.assertTrue(out.getvalue().isascii())

        out_u, err_u = _Rec(), _Rec()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("sys.stdout", out_u), mock.patch("sys.stderr", err_u):
                code = HCR.main(["--unicode", "status", "--full", "ID"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out_u.calls,
            [{"errors": "backslashreplace"}, {"encoding": "utf-8"}],
        )
        self.assertEqual(err_u.calls, [{"errors": "backslashreplace"}])
        self.assertIn("在工作区写 hello.txt", out_u.getvalue())

    def test_keyboard_interrupt_emits_json(self) -> None:
        def fake_urlopen(req, timeout=30):
            raise KeyboardInterrupt

        code, text = self._run_main(["status", "ID"], fake_urlopen)
        self.assertEqual(code, 130)
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text)["code"], "interrupted")

        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(HCR, "_emit", wraps=HCR._emit) as emit:
                with mock.patch("sys.stdout", buf):
                    code = HCR.main(["--unicode", "status", "ID"])
        self.assertEqual(code, 130)
        self.assertIs(emit.call_args.kwargs.get("unicode"), True)
        self.assertEqual(json.loads(buf.getvalue())["error"], "KeyboardInterrupt")

    def test_open_external_input_sends_resolved_path_and_sha256(self) -> None:
        payload = b"pinned-bytes\n"
        second = b"other"
        src_dir = Path(self._tmp.name)
        src = src_dir / "pin.txt"
        other = src_dir / "other.txt"
        src.write_bytes(payload)
        other.write_bytes(second)
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"ok": True, "request_id": "g1", "state": "queued"})

        prev = os.getcwd()
        try:
            os.chdir(src_dir)
            code, text = self._run_main(
                [
                    "open",
                    "--goal",
                    "read pins",
                    "--external-input",
                    "pin.txt",
                    "--external-input",
                    str(other),
                ],
                fake_urlopen,
            )
        finally:
            os.chdir(prev)
        self.assertEqual(code, 0)
        self.assertTrue(text.isascii())
        pins = captured["body"]["external_inputs"]
        self.assertEqual(
            pins,
            [
                {"path": str(src.resolve()), "sha256": hashlib.sha256(payload).hexdigest()},
                {"path": str(other.resolve()), "sha256": hashlib.sha256(second).hexdigest()},
            ],
        )
        self.assertTrue(all(Path(item["path"]).is_absolute() for item in pins))

        captured.clear()
        code, text = self._run_main(["open", "--goal", "no pin"], fake_urlopen)
        self.assertEqual(code, 0)
        self.assertNotIn("external_inputs", captured["body"])
        self.assertTrue(text.isascii())

    def test_open_missing_external_input_exits_bad_external_input(self) -> None:
        missing = str(Path(self._tmp.name) / "no-such-file.txt")

        def fail_urlopen(req, timeout=30):
            raise AssertionError("open must not send when the external input is missing")

        code, text = self._run_main(
            ["open", "--goal", "read pin", "--external-input", missing],
            fail_urlopen,
        )
        self.assertEqual(code, 1)
        self.assertTrue(text.isascii())
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "bad_external_input")
        self.assertIn("no-such-file.txt", payload["error"])

    def test_decide_posts_encoded_ids_and_exits_0(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"ok": True, "state": "running", "request_id": "目标/1"})

        code, text = self._run_main(
            [
                "decide",
                "目标/1",
                "决策 2",
                "--verdict",
                "pass",
                "--reason",
                "checked",
                "--answers",
                '[["yes"]]',
            ],
            fake_urlopen,
        )
        self.assertEqual(code, 0)
        self.assertEqual(captured["method"], "POST")
        self.assertNotIn("目标", captured["url"])
        self.assertNotIn("决策", captured["url"])
        self.assertIn("/v1/requests/", captured["url"])
        self.assertIn("/decisions/", captured["url"])
        self.assertIn("%2F", captured["url"])
        self.assertEqual(captured["body"]["verdict"], "pass")
        self.assertEqual(captured["body"]["reason"], "checked")
        self.assertEqual(captured["body"]["answers"], [["yes"]])
        self.assertTrue(text.strip().endswith("}") or "\n" in text)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertTrue(text.isascii())
        self.assertEqual(payload["request_id"], "目标/1")

    def test_decide_409_passthrough_exits_5(self) -> None:
        message = "artifact contaminated: CONTAMINATED label.txt: AI生成x1. Fail this review"
        err_body = {
            "ok": False,
            "code": "artifact_contaminated",
            "error": message,
            "contamination": {
                "label.txt": {
                    "aigc_marks": {"AI生成": 1},
                    "invisible": {},
                    "encoding": "utf-8",
                }
            },
            "hint": "allow_aigc_marks",
        }

        def fake_urlopen(req, timeout=30):
            self.assertEqual(req.get_method(), "POST")
            self.assertTrue(req.full_url.endswith("/v1/requests/g1/decisions/d1"))
            raise HTTPError(
                req.full_url,
                409,
                "Conflict",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(json.dumps(err_body).encode("utf-8")),
            )

        code, text = self._run_main(
            ["decide", "g1", "d1", "--verdict", "pass"],
            fake_urlopen,
        )
        self.assertEqual(code, HCR.EXIT_DECISION_REFUSED)
        self.assertEqual(code, 5)
        self.assertTrue(text.isascii())
        self.assertNotIn("AI生成", text)
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "artifact_contaminated")
        self.assertEqual(payload["error"], message)
        self.assertEqual(payload["http_status"], 409)
        self.assertEqual(payload["hint"], "allow_aigc_marks")
        self.assertEqual(payload["contamination"]["label.txt"]["aigc_marks"]["AI生成"], 1)

    def test_decide_transport_error_exits_1(self) -> None:
        def fake_urlopen(req, timeout=30):
            raise URLError("refused")

        code, text = self._run_main(
            ["decide", "g1", "d1", "--verdict", "fail", "--reason", "redo"],
            fake_urlopen,
        )
        self.assertEqual(code, 1)
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "transport_error")
        self.assertNotIn("contamination", payload)

    def test_help_mentions_ascii_default_and_unicode(self) -> None:
        help_text = HCR.build_parser().format_help()
        self.assertIn("--unicode", help_text)
        self.assertIn("ASCII", help_text)
        self.assertIn("\\uXXXX", help_text)
        self.assertIn("COLLAB_JSON_UNICODE", help_text)

    def test_ping_ok_includes_capabilities(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            if req.full_url.endswith("/health"):
                return _FakeResp({"ok": True, "api_version": "v9"})
            if req.full_url.endswith("/v1/capabilities"):
                auth = req.headers.get("Authorization") or req.headers.get("authorization")
                self.assertEqual(auth, "Bearer test-token")
                return _FakeResp({"ok": True, "planners": ["deterministic"]})
            raise AssertionError(req.full_url)

        code, text = self._run_main(["ping"], fake_urlopen)
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [
                "http://127.0.0.1:8765/health",
                "http://127.0.0.1:8765/v1/capabilities",
            ],
        )
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_version"], "v9")
        self.assertEqual(payload["base"], "http://127.0.0.1:8765")
        self.assertEqual(payload["capabilities"]["planners"], ["deterministic"])
        self.assertNotIn("test-token", text)

    def test_ping_transport_error(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=30):
            calls.append(req.full_url)
            raise URLError("refused")

        code, text = self._run_main(["ping"], fake_urlopen)
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["http://127.0.0.1:8765/health"])
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "transport_error")

    def test_ping_capabilities_404_is_null(self) -> None:
        def fake_urlopen(req, timeout=30):
            if req.full_url.endswith("/v1/capabilities"):
                body = json.dumps({"ok": False, "code": "not_found", "error": "no such route"}).encode()
                raise HTTPError(
                    req.full_url,
                    404,
                    "Not Found",
                    hdrs=None,  # type: ignore[arg-type]
                    fp=io.BytesIO(body),
                )
            return _FakeResp({"ok": True, "api_version": "v9"})

        code, text = self._run_main(["ping"], fake_urlopen)
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_version"], "v9")
        self.assertIsNone(payload["capabilities"])
        self.assertNotIn("no such route", text)

    def _rich_status_body(self) -> dict:
        return {
            "ok": True,
            "request_id": "g1",
            "state": "blocked",
            "need_human": True,
            "failure_reason": "need a human",
            "failure_code": "awaiting_user",
            "warnings": ["disk low"],
            "capability_warnings": ["no push"],
            "goal": {
                "desired_outcome": "GOAL_CONTRACT_SECRET",
                "boundaries": {"must": ["SECRET_MUST"]},
            },
            "goal_contract": {"budget": {"wall_sec": 300}, "text": "SECRET_CONTRACT"},
            "pending_decisions": [
                {
                    "decision_id": "d1",
                    "kind": "question",
                    "title": "Pick one",
                    "status": "pending",
                    "awaiting": "human",
                    "reason": "because",
                }
            ],
            "awaiting_lead_count": 1,
            "awaiting_human_count": 1,
            "tasks": [
                {
                    "task_id": "t1",
                    "title": "sample",
                    "status": "awaiting_decision",
                    "workspace": "/tmp/ws",
                    "expected_artifacts": ["out.md"],
                    "result": {
                        "artifacts": [
                            {
                                "path": "out.md",
                                "bytes": 12,
                                "preview": "SECRET_PREVIEW",
                            }
                        ],
                        "response": "WORKER_RESPONSE_SECRET",
                        "stdout": "STDOUT_SECRET_BLOB",
                        "stderr": "STDERR_SECRET_BLOB",
                        "error": "E" * 500,
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    },
                }
            ],
        }

    def test_status_summary_hides_secrets_and_keeps_signals(self) -> None:
        body = self._rich_status_body()

        def fake_urlopen(req, timeout=30):
            return _FakeResp(body)

        code, text = self._run_main(["status", "g1"], fake_urlopen)
        self.assertEqual(code, 0)
        for secret in (
            "GOAL_CONTRACT_SECRET",
            "SECRET_MUST",
            "SECRET_CONTRACT",
            "WORKER_RESPONSE_SECRET",
            "STDOUT_SECRET_BLOB",
            "STDERR_SECRET_BLOB",
            "SECRET_PREVIEW",
        ):
            self.assertNotIn(secret, text)
        summary = json.loads(text)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["request_id"], "g1")
        self.assertEqual(summary["state"], "blocked")
        self.assertFalse(summary["terminal"])
        self.assertIs(summary["need_human"], True)
        self.assertEqual(summary["failure_reason"], "need a human")
        self.assertEqual(summary["failure_code"], "awaiting_user")
        decision = summary["pending_decisions"][0]
        self.assertEqual(decision["decision_id"], "d1")
        self.assertEqual(decision["kind"], "question")
        self.assertEqual(decision["awaiting"], "human")
        self.assertEqual(decision["summary"], "Pick one")
        self.assertEqual(summary["awaiting_lead_count"], 1)
        self.assertEqual(summary["awaiting_human_count"], 1)
        task = summary["tasks"][0]
        self.assertEqual(task["task_id"], "t1")
        self.assertEqual(task["title"], "sample")
        self.assertEqual(task["status"], "awaiting_decision")
        self.assertEqual(task["artifacts"], ["out.md"])
        self.assertEqual(task["workspace"], "/tmp/ws")
        self.assertEqual(len(task["error"]), 300)
        self.assertTrue(set(task["error"]) <= {"E"})
        self.assertEqual(summary["artifacts"], [{"task_id": "t1", "path": "out.md", "size": 12}])
        self.assertEqual(summary["progress"], {"available": False, "phase": "unknown"})
        self.assertNotIn("percent", summary["progress"])
        self.assertEqual(summary["usage"]["source"], "worker_self_reported")
        self.assertEqual(summary["usage"]["values"], {"input_tokens": 3, "output_tokens": 4})
        self.assertEqual(summary["warnings"], ["disk low", "no push"])
        self.assertEqual(summary["http_status"], 200)
        self.assertIs(summary["truncated"], True)
        self.assertEqual(summary["full_hint"], "rerun with --full")
        self.assertNotIn("goal", summary)
        self.assertNotIn("stdout", summary)
        self.assertNotIn("stderr", summary)

    def test_status_full_and_env_print_raw_payload(self) -> None:
        body = self._rich_status_body()

        def fake_urlopen(req, timeout=30):
            return _FakeResp(body)

        code, text = self._run_main(["status", "g1", "--full"], fake_urlopen)
        self.assertEqual(code, 0)
        raw = json.loads(text)
        self.assertEqual(raw["goal"]["desired_outcome"], "GOAL_CONTRACT_SECRET")
        self.assertIn("WORKER_RESPONSE_SECRET", text)
        self.assertIn("STDOUT_SECRET_BLOB", text)
        self.assertNotIn("full_hint", raw)

        code, text = self._run_main(
            ["status", "g1"],
            fake_urlopen,
            {"COLLAB_OUTPUT_FULL": "1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["goal"]["desired_outcome"], "GOAL_CONTRACT_SECRET")

    def test_report_summary_drops_desired_outcome_and_stdout(self) -> None:
        def fake_urlopen(req, timeout=30):
            self.assertTrue(req.full_url.endswith("/report"))
            return _FakeResp(
                {
                    "ok": True,
                    "goal_id": "g9",
                    "state": "completed",
                    "report": {
                        "desired_outcome": "GOAL_CONTRACT_SECRET",
                        "state": "completed",
                        "tasks": [
                            {
                                "task_id": "t9",
                                "title": "done",
                                "status": "succeeded",
                                "result": {
                                    "stdout": "STDOUT_SECRET_BLOB",
                                    "response": "WORKER_RESPONSE_SECRET",
                                    "artifacts": ["poem.txt"],
                                },
                            }
                        ],
                    },
                }
            )

        code, text = self._run_main(["report", "g9"], fake_urlopen)
        self.assertEqual(code, 0)
        self.assertNotIn("GOAL_CONTRACT_SECRET", text)
        self.assertNotIn("STDOUT_SECRET_BLOB", text)
        self.assertNotIn("WORKER_RESPONSE_SECRET", text)
        summary = json.loads(text)
        self.assertEqual(summary["request_id"], "g9")
        self.assertEqual(summary["state"], "completed")
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["tasks"][0]["artifacts"], ["poem.txt"])
        self.assertEqual(summary["usage"], {"source": "unknown"})
        self.assertEqual(summary["full_hint"], "rerun with --full")

    def test_summarize_progress_and_usage_are_not_invented(self) -> None:
        unknown = HCR.summarize_status({"ok": True, "state": "running", "request_id": "g"})
        self.assertEqual(unknown["progress"], {"available": False, "phase": "unknown"})
        self.assertNotIn("percent", unknown["progress"])
        self.assertEqual(unknown["usage"], {"source": "unknown"})
        self.assertIs(unknown["truncated"], False)

        real = HCR.summarize_status(
            {
                "ok": True,
                "state": "running",
                "request_id": "g",
                "progress": {"phase": "writing"},
                "tasks": [
                    {"task_id": "a", "title": "a", "status": "running", "result": {"usage": {"input_tokens": 1}}},
                    {"task_id": "b", "title": "b", "status": "running", "result": {"usage": {"input_tokens": 4}}},
                ],
            }
        )
        self.assertTrue(real["progress"]["available"])
        self.assertEqual(real["progress"]["phase"], "writing")
        self.assertNotIn("percent", real["progress"])
        self.assertEqual(real["usage"]["source"], "worker_self_reported")
        values = real["usage"]["values"]
        self.assertNotEqual(values.get("input_tokens"), 5)
        self.assertEqual(values["a"], {"input_tokens": 1})
        self.assertEqual(values["b"], {"input_tokens": 4})

    def test_wait_observation_timeout_is_not_a_task_failure(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "running",
                    "request_id": "g1",
                    "goal": {"desired_outcome": "GOAL_CONTRACT_SECRET"},
                    "tasks": [
                        {
                            "task_id": "t1",
                            "status": "running",
                            "result": {"stdout": "STDOUT_SECRET_BLOB", "response": "WORKER_RESPONSE_SECRET"},
                        }
                    ],
                }
            )

        mono = iter([100.0, 100.1, 999.0])
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(HCR.time, "monotonic", side_effect=lambda: next(mono)):
                with mock.patch.object(HCR.time, "sleep", return_value=None):
                    with mock.patch("sys.stdout", buf):
                        code = HCR.main(["wait", "g1", "--timeout", "30", "--interval", "0.01"])
        self.assertEqual(code, 3)
        text = buf.getvalue()
        self.assertNotIn("GOAL_CONTRACT_SECRET", text)
        self.assertNotIn("STDOUT_SECRET_BLOB", text)
        self.assertNotIn("WORKER_RESPONSE_SECRET", text)
        printed = json.loads(text)
        wait = printed["wait"]
        self.assertEqual(wait["kind"], "observation_timeout")
        self.assertTrue(wait["task_still_running"])
        self.assertTrue(wait["timed_out"])
        self.assertEqual(wait["timeout_sec"], 30.0)
        self.assertEqual(
            wait["resume"],
            "python bin/hermes-collab-request.py wait g1 --timeout 30",
        )
        self.assertIn("NOT a task failure", wait["note"])
        self.assertIn("do not re-open or retry", wait["note"])
        self.assertNotEqual(printed.get("code"), "need_human")
        self.assertNotIn("task_timeout", wait)

    def test_wait_task_failed_wall_budget_sets_task_timeout(self) -> None:
        def failed(reason: str, state: str = "failed"):
            def fake_urlopen(req, timeout=30):
                return _FakeResp(
                    {
                        "ok": True,
                        "state": state,
                        "request_id": "g1",
                        "failure_reason": reason,
                        "failure": {"error": reason},
                        "goal": {"desired_outcome": "GOAL_CONTRACT_SECRET"},
                        "tasks": [
                            {
                                "task_id": "t1",
                                "title": "work",
                                "status": "failed",
                                "result": {
                                    "error": reason,
                                    "stdout": "STDOUT_SECRET_BLOB",
                                    "response": "WORKER_RESPONSE_SECRET",
                                },
                            }
                        ],
                    }
                )

            return fake_urlopen

        code, text = self._run_main(
            ["wait", "g1", "--timeout", "5", "--interval", "0.01"],
            failed("need_human: budget_exceeded wall wall_s=301 max=300"),
        )
        self.assertEqual(code, 2)
        self.assertNotIn("GOAL_CONTRACT_SECRET", text)
        self.assertNotIn("STDOUT_SECRET_BLOB", text)
        self.assertNotIn("WORKER_RESPONSE_SECRET", text)
        printed = json.loads(text)
        self.assertEqual(printed["wait"]["kind"], "task_failed")
        self.assertIs(printed["wait"]["task_timeout"], True)
        self.assertTrue(printed["wait"]["terminal"])
        self.assertEqual(printed["state"], "failed")

        code, text = self._run_main(
            ["wait", "g1", "--timeout", "5", "--interval", "0.01"],
            failed("need_human: budget_exceeded steps steps=401 max=400"),
        )
        self.assertEqual(code, 2)
        step_wait = json.loads(text)["wait"]
        self.assertEqual(step_wait["kind"], "task_failed")
        self.assertNotIn("task_timeout", step_wait)

        code, text = self._run_main(
            ["wait", "g1", "--timeout", "5", "--interval", "0.01"],
            failed("worker crashed", state="cancelled"),
        )
        self.assertEqual(code, 2)
        cancelled = json.loads(text)["wait"]
        self.assertEqual(cancelled["kind"], "task_cancelled")
        self.assertNotIn("task_timeout", cancelled)

        code, text = self._run_main(
            ["wait", "g1", "--timeout", "5", "--interval", "0.01"],
            failed("Task deadline exhausted (includes approvals and redo)"),
        )
        self.assertEqual(code, 2)
        self.assertIs(json.loads(text)["wait"]["task_timeout"], True)

    def test_wait_need_human_kind(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "blocked",
                    "request_id": "g1",
                    "pending_decisions": [
                        {
                            "decision_id": "dec-1",
                            "kind": "question",
                            "title": "继续吗",
                            "status": "pending",
                            "awaiting": "human",
                        }
                    ],
                }
            )

        code, printed, slept = self._wait_main(
            ["wait", "g1", "--timeout", "30", "--interval", "0.2"],
            fake_urlopen,
        )
        self.assertEqual(code, 4)
        self.assertEqual(slept.call_count, 0)
        self.assertEqual(printed["code"], "need_human")
        self.assertIs(printed["need_human"], True)
        self.assertEqual(printed["wait"]["kind"], "need_human")
        self.assertEqual(printed["wait"]["reason"], "pending_decisions")
        self.assertFalse(printed["wait"]["timed_out"])

    def test_pending_filters_terminal_requests(self) -> None:
        def fake_urlopen(req, timeout=30):
            self.assertEqual(req.get_method(), "GET")
            self.assertTrue(req.full_url.endswith("/v1/requests"))
            self.assertNotIn("/v1/requests/", req.full_url)
            return _FakeResp(
                {
                    "ok": True,
                    "requests": [
                        {
                            "goal_id": "run-1",
                            "state": "running",
                            "awaiting_decision": False,
                            "updated_at": 10,
                            "updated_at_iso": "t1",
                        },
                        {"goal_id": "done-1", "state": "completed", "updated_at": 11},
                        {"request_id": "fail-1", "state": "failed", "updated_at": 12},
                        {
                            "goal_id": "blk-1",
                            "state": "blocked",
                            "pending_count": 1,
                            "updated_at_iso": "t2",
                        },
                        {"goal_id": "can-1", "state": "cancelled", "updated_at": 13},
                        {"goal_id": "q-1", "state": "queued", "updated_at": 14},
                    ],
                }
            )

        code, text = self._run_main(["pending"], fake_urlopen)
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        rows = payload["requests"]
        self.assertEqual([row["request_id"] for row in rows], ["run-1", "blk-1", "q-1"])
        self.assertEqual(rows[0]["state"], "running")
        self.assertFalse(rows[0]["awaiting_decision"])
        self.assertEqual(rows[0]["updated_at"], "t1")
        self.assertEqual(rows[1]["state"], "blocked")
        self.assertTrue(rows[1]["awaiting_decision"])
        self.assertEqual(rows[1]["updated_at"], "t2")
        self.assertEqual(rows[2]["state"], "queued")
        self.assertFalse(rows[2]["awaiting_decision"])
        self.assertEqual(rows[2]["updated_at"], 14)

    def test_open_too_many_external_inputs_before_io(self) -> None:
        argv = ["open", "--goal", "too many"]
        for index in range(9):
            argv.extend(["--external-input", f"/tmp/does-not-exist-{index}.txt"])

        def fail_urlopen(req, timeout=30):
            raise AssertionError("open must not send HTTP when there are too many pins")

        with mock.patch.object(Path, "open", side_effect=AssertionError("opened")) as opened:
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes")) as read_bytes:
                with mock.patch.object(HCR, "_sha256_file", side_effect=AssertionError("hashed")) as hashed:
                    with mock.patch.object(HCR.hashlib, "sha256", side_effect=AssertionError("sha")) as sha:
                        code, text = self._run_main(argv, fail_urlopen)
        self.assertEqual(code, 1)
        self.assertEqual(opened.call_count, 0)
        self.assertEqual(read_bytes.call_count, 0)
        self.assertEqual(hashed.call_count, 0)
        self.assertEqual(sha.call_count, 0)
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "too_many_external_inputs")
        self.assertIn("8", payload["error"])
        self.assertIn("at most", payload["error"])

    def test_sha256_file_streams_and_matches_hashlib(self) -> None:
        content = b"abc123" * 50
        path = Path(self._tmp.name) / "blob.bin"
        path.write_bytes(content)
        reads: list[int] = []
        real_open = Path.open

        def spy_open(self, mode="r", *args, **kwargs):
            handle = real_open(self, mode, *args, **kwargs)
            if Path(self) == path and "b" in str(mode):
                original = handle.read

                def read(n=-1):
                    data = original(n)
                    reads.append(0 if not data else len(data))
                    return data

                handle.read = read  # type: ignore[method-assign]
            return handle

        with mock.patch.object(HCR, "_HASH_CHUNK", 16):
            with mock.patch.object(Path, "open", spy_open):
                digest = HCR._sha256_file(path)
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        self.assertGreater(sum(1 for size in reads if size), 1)

        record = HCR._external_input_record(str(path))
        self.assertEqual(record["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(record["path"], str(path.resolve()))

    def test_open_require_capability_and_ack_flags(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=30):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp({"ok": True, "request_id": "g", "state": "queued"}, status=202)

        code, text = self._run_main(
            [
                "open",
                "--goal",
                "write the file",
                "--require-capability",
                "permission_gate",
                "--require-capability",
                "no_skip_permissions",
                "--ack-prompt-only-inputs",
            ],
            fake_urlopen,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            captured["body"]["required_capabilities"],
            ["permission_gate", "no_skip_permissions"],
        )
        self.assertIs(captured["body"]["acknowledge_prompt_only_inputs"], True)
        self.assertEqual(json.loads(text)["request_id"], "g")

    def test_open_capability_unavailable_prints_missing_and_exits_1(self) -> None:
        def fake_urlopen(req, timeout=30):
            raw = json.dumps(
                {
                    "ok": False,
                    "code": "capability_unavailable",
                    "error": "required capabilities are not available on this backend",
                    "missing": ["permission_gate", "os_sandbox"],
                    "capabilities": {"backend": {"id": "inprocess.local_v1"}},
                }
            ).encode("utf-8")
            raise HTTPError(
                req.full_url,
                409,
                "Conflict",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(raw),
            )

        code, text = self._run_main(
            ["open", "--goal", "write the file", "--require-capability", "permission_gate"],
            fake_urlopen,
        )
        self.assertEqual(code, 1)
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "capability_unavailable")
        self.assertEqual(payload["missing"], ["permission_gate", "os_sandbox"])
        self.assertEqual(payload["http_status"], 409)
        self.assertEqual(payload["capabilities"]["backend"]["id"], "inprocess.local_v1")

    def test_status_summary_includes_task_review(self) -> None:
        def fake_urlopen(req, timeout=30):
            return _FakeResp(
                {
                    "ok": True,
                    "state": "completed",
                    "request_id": "g1",
                    "tasks": [
                        {
                            "task_id": "t1",
                            "title": "write",
                            "status": "succeeded",
                            "result": {
                                "review": {
                                    "status": "unsupported",
                                    "source": "none",
                                    "evidence": "acceptance text was not independently verified",
                                }
                            },
                        }
                    ],
                }
            )

        code, text = self._run_main(["status", "g1"], fake_urlopen)
        self.assertEqual(code, 0)
        task = json.loads(text)["tasks"][0]
        self.assertEqual(
            task["review"],
            {
                "status": "unsupported",
                "source": "none",
                "evidence": "acceptance text was not independently verified",
            },
        )
        bare = HCR.summarize_status(
            {"ok": True, "state": "running", "tasks": [{"task_id": "t"}]}
        )
        self.assertEqual(
            bare["tasks"][0]["review"],
            {"status": "not_requested", "source": "none", "evidence": ""},
        )


if __name__ == "__main__":
    unittest.main()
