"""--ready with --backend antigravity checks agy, not TeleAgent (mde, 2026-10-05).

RED on dda69d3: the agy backend got the TeleAgent gate (":4399 is not
listening", TeleAgent creds) and never said whether agy was signed in.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_POSIX = os.name == "posix"

_FAKE_AGY = """#!{py}
import os, sys
if "--version" in sys.argv:
    print("1.2.16"); sys.exit(0)
if sys.argv[1:2] == ["models"]:
    if os.environ.get("FAKE_AGY_SIGNED_IN") == "1":
        print("Fetching available models..."); print("gemini-3-pro"); print("gemini-3-flash"); sys.exit(0)
    print("Fetching available models...")
    print("Error: Please sign in to view available models. Launch the CLI without arguments to sign in.", file=sys.stderr)
    sys.exit(1)
sys.exit(2)
"""


def _service():
    spec = importlib.util.spec_from_file_location("collab_service_agy_ready", ROOT / "bin" / "collab-service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(_POSIX, "fake agy is a POSIX script")
class AgyReadyGateTests(unittest.TestCase):
    def _ready(self, signed_in: bool, backend: str = "antigravity"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agy = root / "agy"
            agy.write_text(_FAKE_AGY.format(py=sys.executable), encoding="utf-8")
            agy.chmod(agy.stat().st_mode | stat.S_IEXEC)
            env = {
                "AGY_BIN": str(agy),
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root),
                "FAKE_AGY_SIGNED_IN": "1" if signed_in else "0",
            }
            svc = _service()
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
                rc = svc.main(["--backend", backend, "--persist", str(root / "p"), "--ready"])
            return rc, json.loads(out.getvalue())

    def test_not_signed_in_is_reported_and_no_teleagent_checks(self):
        rc, res = self._ready(signed_in=False)
        self.assertEqual(rc, 1)
        text = json.dumps(res)
        self.assertNotIn("4399", text)
        self.assertNotIn("TeleAgent", text)
        names = {c["name"]: c for c in res["checks"]}
        self.assertNotIn("port", names)
        self.assertNotIn("creds", names)
        self.assertTrue(names["agy_bin"]["ok"])
        self.assertEqual(names["agy_version"]["detail"], "1.2.16")
        self.assertFalse(names["agy_login"]["ok"])
        self.assertEqual(names["agy_login"]["code"], "not_logged_in")
        self.assertIn("Please sign in", names["agy_login"]["detail"])
        self.assertEqual(res["agy"]["login"], "not_logged_in")
        self.assertIn("agy", res["hints"][0])

    def test_signed_in_passes_login_check(self):
        rc, res = self._ready(signed_in=True, backend="agy")
        names = {c["name"]: c for c in res["checks"]}
        self.assertTrue(names["agy_login"]["ok"], names["agy_login"])
        self.assertEqual(res["agy"]["login"], "logged_in")
        # Only the running tip may still block (it depends on the checkout).
        blockers = [c["name"] for c in res["checks"] if not c["ok"] and c["blocks_dispatch"]]
        self.assertLessEqual(set(blockers), {"running_tip"})
        self.assertEqual(rc, 0 if not blockers else 1)
        self.assertEqual(res["ready"], not blockers)

    def test_missing_binary_blocks(self):
        from framework.agy_ready import assess_agy_readiness

        res = assess_agy_readiness(environ={"PATH": "/nonexistent"}, bin_path="/nonexistent/agy", running_tip="a", head_tip="a")
        names = {c["name"]: c for c in res["checks"]}
        self.assertFalse(names["agy_bin"]["ok"])
        self.assertEqual(names["agy_bin"]["code"], "agy_missing")
        self.assertFalse(res["ready"])


class ClassifyLoginTests(unittest.TestCase):
    def test_states(self):
        from framework.agy_ready import classify_login

        self.assertEqual(classify_login(1, "Error: Please sign in to view available models.")["state"], "not_logged_in")
        self.assertEqual(classify_login(0, "Fetching available models...\nm1\nm2")["state"], "logged_in")
        self.assertEqual(classify_login(None, "timed out after 30s")["state"], "unknown")
        self.assertEqual(classify_login(3, "boom")["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
