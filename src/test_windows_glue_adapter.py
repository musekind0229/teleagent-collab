"""Windows glue must use and preserve the platform adapter's live endpoint."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glue


class _MovingAdapter:
    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1:4398"
        self.calls = 0

    def call(self, method, path, body=None, extra_headers=None, timeout=120):
        self.calls += 1
        self.base_url = "http://127.0.0.1:4401"
        return 200, {"ok": True}


class WindowsGlueAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_adapter = glue._ADAPTER
        self.old_base = glue.BASE

    def tearDown(self) -> None:
        glue._ADAPTER = self.old_adapter
        glue.BASE = self.old_base

    def test_factory_uses_current_platform(self):
        fake = _MovingAdapter()
        glue._ADAPTER = None
        with mock.patch("teleagent_adapter.get_adapter", return_value=fake) as factory:
            self.assertIs(glue.get_ta_adapter(), fake)
        factory.assert_called_once_with(platform=mock.ANY)
        self.assertEqual(factory.call_args.kwargs["platform"], glue.sys.platform)

    def test_windows_does_not_reset_adapter_after_endpoint_move(self):
        fake = _MovingAdapter()
        glue._ADAPTER = fake
        glue.BASE = "http://127.0.0.1:4398"
        with mock.patch.object(glue.sys, "platform", "win32"), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(glue.call("GET", "/version")[0], 200)
            self.assertEqual(glue.BASE, "http://127.0.0.1:4401")
            self.assertEqual(glue.call("GET", "/permission")[0], 200)
        self.assertEqual(fake.base_url, "http://127.0.0.1:4401")
        self.assertEqual(fake.calls, 2)

    def test_status_writer_creates_collab_directory(self):
        old_collab = glue.COLLAB
        try:
            with tempfile.TemporaryDirectory() as td:
                glue.COLLAB = Path(td) / "nested" / "runs"
                glue._write_status("pilot", {"ok": False, "state": "fail"})
                self.assertTrue((glue.COLLAB / "status-pilot.json").is_file())
        finally:
            glue.COLLAB = old_collab

    def test_charter_projection_uses_windows_backend(self):
        from framework import charter_map

        charter = {
            "name": "win-pilot",
            "goal": "create a file",
            "must": ["stay in workspace"],
            "must_not": ["network"],
            "allow_secret_globs": [],
            "allow_paths": [],
            "allow_keys": [],
            "done_when": {"artifacts": ["a.txt"]},
        }
        with mock.patch.object(charter_map.sys, "platform", "win32"):
            mapped = charter_map.map_charter_to_goal_task(charter)
        self.assertEqual(mapped["goal"]["platform_allowlist"], ["windows"])
        self.assertEqual(mapped["task"]["backend_requirement"], "teleagent.windows.local_v1")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
