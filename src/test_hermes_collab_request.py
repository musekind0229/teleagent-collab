"""Mock-HTTP unit tests for bin/hermes-collab-request.py (no live service)."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
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
        self._env = mock.patch.dict(
            "os.environ",
            {"COLLAB_API_BASE": "http://127.0.0.1:8765", "COLLAB_API_TOKEN": "test-token"},
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

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


if __name__ == "__main__":
    unittest.main()
