#!/usr/bin/env python3
"""Path-B knife3: public→native for rules; PendingItem stores public ids."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from state_store import PendingItem, StateStore  # noqa: E402
from teleagent_adapter.permission_view import (  # noqa: E402
    prepare_permission,
    to_native_for_rules,
    to_public_permission,
)


class TestPreparePermission(unittest.TestCase):
    def test_raw_ta_yields_public_and_native(self):
        raw = {
            "id": "per_k3",
            "sessionID": "ses_k3",
            "permission": "external_directory",
            "patterns": ["/tmp/*"],
            "metadata": {"filepath": "/tmp/x"},
            "tool": {"name": "Read"},
        }
        public, native = prepare_permission(raw)
        self.assertEqual(public["request_id"], "per_k3")
        self.assertEqual(public["session_id"], "ses_k3")
        self.assertNotIn("sessionID", public)
        self.assertEqual(native["id"], "per_k3")
        self.assertEqual(native["sessionID"], "ses_k3")
        # hard_rules must not be fed public-only keys as primary id source
        self.assertTrue(native.get("id") or native.get("requestID"))

    def test_public_input_roundtrip(self):
        raw = {
            "id": "per_2",
            "sessionID": "ses_2",
            "permission": "bash",
            "patterns": [],
            "tool": "Bash",
        }
        public = to_public_permission(raw)
        again_pub, native = prepare_permission(public)
        self.assertEqual(again_pub["request_id"], "per_2")
        self.assertEqual(native["id"], "per_2")
        self.assertEqual(to_native_for_rules(public)["sessionID"], "ses_2")


class TestPendingItemPublicIds(unittest.TestCase):
    def test_request_id_defaults_and_roundtrip(self):
        item = PendingItem(
            permission_id="per_a",
            job_id="job_1",
            session_id="ses_a",
            request_id="per_a",
            summary="path",
        )
        self.assertEqual(item.request_id, "per_a")
        d = item.to_dict()
        self.assertEqual(d["request_id"], "per_a")
        self.assertEqual(d["session_id"], "ses_a")
        back = PendingItem.from_dict(d)
        self.assertEqual(back.request_id, "per_a")
        self.assertEqual(back.permission_id, "per_a")

    def test_old_row_without_request_id(self):
        back = PendingItem.from_dict(
            {
                "permission_id": "old_pid",
                "job_id": "j",
                "session_id": "s",
                "summary": "",
                "created_at": 1.0,
                "status": "open",
            }
        )
        self.assertEqual(back.request_id, "old_pid")

    def test_store_persists_request_id(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(root=td, run_id="k3")
            store.add_pending(
                PendingItem(
                    permission_id="per_s",
                    request_id="per_s",
                    job_id="job_s",
                    session_id="ses_s",
                    summary="sum",
                )
            )
            got = store.get_pending("per_s")
            self.assertIsNotNone(got)
            self.assertEqual(got.request_id, "per_s")
            self.assertEqual(got.session_id, "ses_s")
            # reload
            store2 = StateStore(root=td, run_id="k3")
            got2 = store2.get_pending("per_s")
            self.assertIsNotNone(got2)
            self.assertEqual(got2.request_id, "per_s")
            self.assertEqual(got2.session_id, "ses_s")


if __name__ == "__main__":
    unittest.main()
