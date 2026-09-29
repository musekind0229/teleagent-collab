"""Mock-HTTP unit tests for bin/hermes-collab-request.py (no live service)."""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
