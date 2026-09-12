#!/usr/bin/env python3
"""Contract tests for ExecutionBackend — no TeleAgent, no Hermes ledger."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import load_charter  # noqa: E402
from execution_backend import (  # noqa: E402
    BackendStatus,
    InProcessExecutionBackend,
    get_execution_backend,
    run_file_job_via_public_api,
    unsupported,
)


REPO = SRC.parent


class TestUnsupported(unittest.TestCase):
    def test_never_invents_reply(self):
        body = unsupported("reply_permission", "no channel")
        self.assertEqual(body["status"], BackendStatus.UNSUPPORTED.value)
        self.assertIsNone(body["reply"])
        self.assertFalse(body["ok"])


class TestInProcessBackend(unittest.TestCase):
    def test_factory(self):
        be = get_execution_backend("inprocess")
        self.assertIsInstance(be, InProcessExecutionBackend)
        self.assertEqual(be.backend_id, "inprocess.local_v1")

    def test_hermes_not_offered(self):
        with self.assertRaises(Exception):
            get_execution_backend("hermes")

    def test_permission_reply_unsupported(self):
        be = InProcessExecutionBackend()
        code, body = be.reply_permission("per_x", "once")
        self.assertEqual(code, 501)
        self.assertEqual(body["status"], "unsupported")
        self.assertIsNone(body["reply"])

    def test_list_pending_empty(self):
        be = InProcessExecutionBackend()
        code, items = be.list_pending_actions()
        self.assertEqual(code, 200)
        self.assertEqual(items, [])

    def test_hello_public_api_only(self):
        charter = load_charter(REPO / "jobs/examples/hello.charter.yaml")
        with tempfile.TemporaryDirectory() as td:
            result = run_file_job_via_public_api(workdir=td, charter=charter)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["used_public_api_only"])
            self.assertEqual(result["pending_count"], 0)
            self.assertTrue(result["run_observation"]["finish_successful"])
            self.assertEqual(result["run_observation"]["activity"], "idle")
            out = Path(td) / "hello-from-worker.txt"
            self.assertTrue(out.is_file())
            line = out.read_text(encoding="utf-8").strip()
            self.assertTrue(line)
            self.assertEqual(len(line.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
