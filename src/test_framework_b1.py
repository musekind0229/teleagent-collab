#!/usr/bin/env python3
"""Path-B knife1: charter→Goal map + lifecycle skeleton + opaque TA handle."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from charter import load_charter  # noqa: E402
from framework.charter_map import CharterMapError, map_charter_to_goal_task  # noqa: E402
from framework.lifecycle import LifecycleError, assert_transition  # noqa: E402
from framework.models import CONTRACT_VERSION, contract_fingerprint, make_run  # noqa: E402
from teleagent_adapter.native_handle import session_id_of, wrap_session_handle  # noqa: E402


class TestCharterMap(unittest.TestCase):
    def test_mini_live_maps(self):
        path = SRC.parent / "jobs/examples/mini-live-hello.charter.yaml"
        if not path.exists():
            self.skipTest("mini-live charter not present")
        ch = load_charter(path)
        out = map_charter_to_goal_task(ch)
        g, t = out["goal"], out["task"]
        self.assertEqual(g["contract_version"], CONTRACT_VERSION)
        self.assertEqual(g["boundaries"]["allow_secret_globs"], [])
        self.assertEqual(g["boundaries"]["allow_paths"], [])
        self.assertIn("hello-mini.txt", t["expected_artifacts"])
        self.assertEqual(t["status"], "queued")
        self.assertEqual(out["_allow_fields_missing"], [])

    def test_missing_allow_not_filled_as_allow_all(self):
        ch = {
            "name": "x",
            "goal": "do thing",
            "must": ["a"],
            "must_not": ["b"],
            # deliberately omit allow_*
        }
        out = map_charter_to_goal_task(ch)
        b = out["goal"]["boundaries"]
        self.assertNotIn("allow_secret_globs", b)
        self.assertNotIn("allow_paths", b)
        self.assertNotIn("allow_keys", b)
        self.assertEqual(set(out["_allow_fields_missing"]), {"allow_secret_globs", "allow_paths", "allow_keys"})
        self.assertTrue(any("not filled" in w for w in out["warnings"]))

    def test_empty_allow_stays_empty(self):
        ch = {
            "goal": "g",
            "must": [],
            "must_not": [],
            "allow_secret_globs": [],
            "allow_paths": [],
            "allow_keys": [],
        }
        out = map_charter_to_goal_task(ch)
        self.assertEqual(out["goal"]["boundaries"]["allow_secret_globs"], [])
        self.assertEqual(out["_allow_fields_missing"], [])

    def test_requires_goal_must(self):
        with self.assertRaises(CharterMapError):
            map_charter_to_goal_task({"must": [], "must_not": []})


class TestLifecycle(unittest.TestCase):
    def test_ok_edge(self):
        assert_transition("task", "queued", "running")

    def test_bad_edge(self):
        with self.assertRaises(LifecycleError):
            assert_transition("task", "succeeded", "running")


class TestNativeHandle(unittest.TestCase):
    def test_opaque(self):
        h = wrap_session_handle("ses_abc", extra={"permission": {"id": "per_1"}})
        self.assertEqual(session_id_of(h), "ses_abc")
        self.assertIn("native", h)
        fp = contract_fingerprint({"goal_id": "g1"})
        run = make_run(
            task_id="task_1",
            attempt=1,
            backend="teleagent.linux.local_v1",
            contract_fingerprint_value=fp,
            native_handle=h["session_id"],
        )
        self.assertEqual(run["native_handle"], "ses_abc")
        self.assertNotIn("permission", run)


if __name__ == "__main__":
    unittest.main()
