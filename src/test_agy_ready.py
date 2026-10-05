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
    if os.environ.get("FAKE_AGY_SIGNED_IN") == "1" or os.path.exists(os.path.join(os.environ.get("HOME", ""), "signed")):
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


@unittest.skipUnless(_POSIX, "fake agy is a POSIX script")
class AgyReadyPoolTests(unittest.TestCase):
    """mde 2026-10-05: with a pool, --ready probed /home/teleagent (never signed in)."""

    def _run(self, signed, *, via_flag=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agy = root / "agy"
            agy.write_text(_FAKE_AGY.format(py=sys.executable), encoding="utf-8")
            agy.chmod(agy.stat().st_mode | stat.S_IEXEC)
            accounts = []
            for ident, state in (("primary", "available"), ("second", "available"), ("third", "unavailable")):
                home = root / "homes" / ident
                home.mkdir(parents=True)
                if ident in signed:
                    (home / "signed").write_text("1")
                accounts.append({"id": ident, "home": str(home), "state": state})
            pool = root / "pool.json"
            pool.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
            service_home = root / "service-home"  # the service user's own HOME: never signed in
            service_home.mkdir()
            env = {"AGY_BIN": str(agy), "PATH": os.environ.get("PATH", ""), "HOME": str(service_home), "FAKE_AGY_SIGNED_IN": "0"}
            argv = ["--backend", "antigravity", "--persist", str(root / "p"), "--ready"]
            if via_flag:
                argv += ["--agy-account-pool", str(pool)]
            else:
                env["COLLAB_AGY_ACCOUNT_POOL"] = str(pool)
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
                if via_flag:
                    os.environ.pop("COLLAB_AGY_ACCOUNT_POOL", None)
                rc = _service().main(argv)
            return rc, json.loads(out.getvalue())

    def test_one_signed_in_available_account_makes_login_pass(self):
        for via_flag in (False, True):
            with self.subTest(via_flag=via_flag):
                rc, res = self._run({"primary"}, via_flag=via_flag)
                names = {c["name"]: c for c in res["checks"]}
                self.assertTrue(names["agy_login"]["ok"], names["agy_login"])
                self.assertEqual(res["agy"]["login"], "logged_in")
                by_id = {a["id"]: a for a in res["accounts"]}
                self.assertEqual(by_id["primary"]["login"], "logged_in")
                self.assertEqual(by_id["second"]["login"], "not_logged_in")
                self.assertEqual(by_id["third"]["login"], "not_checked")
                self.assertIn("primary=logged_in", names["agy_login"]["detail"])
                self.assertIn("third=unavailable", names["agy_account_pool"]["detail"])
                blockers = [c["name"] for c in res["checks"] if not c["ok"] and c["blocks_dispatch"]]
                self.assertLessEqual(set(blockers), {"running_tip"})
                self.assertEqual(rc, 0 if not blockers else 1)

    def test_no_signed_in_available_account_reports_each(self):
        rc, res = self._run({"third"})  # only the unavailable account is signed in
        names = {c["name"]: c for c in res["checks"]}
        self.assertEqual(rc, 1)
        self.assertFalse(names["agy_login"]["ok"])
        self.assertEqual(names["agy_login"]["code"], "not_logged_in")
        self.assertIn("primary=not_logged_in", names["agy_login"]["detail"])
        self.assertIn("second=not_logged_in", names["agy_login"]["detail"])

    def test_unreadable_pool_blocks(self):
        from framework.agy_ready import assess_agy_readiness

        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "pool.json"
            bad.write_text("{not json", encoding="utf-8")
            res = assess_agy_readiness(
                environ={"PATH": "/bin"}, bin_path=sys.executable, pool_path=str(bad),
                runner=lambda argv, env, t: (0, "ok"), running_tip="a", head_tip="a",
            )
        names = {c["name"]: c for c in res["checks"]}
        self.assertFalse(names["agy_account_pool"]["ok"])
        self.assertEqual(names["agy_account_pool"]["code"], "pool_unreadable")
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
