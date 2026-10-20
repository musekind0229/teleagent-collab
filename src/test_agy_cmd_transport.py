"""Knife 10: agy launch argument transport contract (investigation only).

Scope (Hermes a36): does a single-line, multi-line, blank-line or Chinese
``--print=`` argument reach the child process verbatim?

* Explicit Python launch (``[sys.executable, probe.py, *args]``) is the control:
  every argument must arrive byte-for-byte on every platform.
* The batch forwarder (``probe.cmd`` with ``%*``) is the *test forwarder*
  already seen in round3 (knife 9). On Windows its multi-line truncation is a
  REPRODUCED behaviour and is pinned below as a characterization test, so the
  suite stays green while the defect is visible. A later product fix needs its
  own red/green gate.
* The installed ``agy.cmd`` launcher is NOT verified here; see
  ``InstalledAgyCmdPending``. No real agy, model, login or service is used.

No product code is changed. Offline, isolated temp dirs only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from execution_backend.antigravity_cli_v1 import PRINT_FLAG_PREFIX, build_agy_argv
from platform_services.cli import CliLaunchError
from platform_services.windows_cli import CmdWrapper

_WINDOWS = os.name == "nt"
_OUT_ENV = "AGY_TRANSPORT_ARGV_OUT"

# Records argv[1:] as JSON (ASCII-escaped, so no console code page is involved).
_PROBE = textwrap.dedent(
    """
    import json, os, sys
    with open(os.environ["AGY_TRANSPORT_ARGV_OUT"], "w", encoding="utf-8") as fh:
        json.dump(sys.argv[1:], fh, ensure_ascii=True)
    """
)

PROMPTS = {
    "single": "Job title: transport probe",
    "multi": "Job title: transport probe\nInstruction: keep every line\nTail: end",
    "blank": "first line\n\nthird line after a blank line",
    "chinese": "任务：中文参数逐字保留",
    "chinese_multi": "第一行中文\n第二行中文",
}
SINGLE_LINE = ("single", "chinese")
MULTI_LINE = ("multi", "blank", "chinese_multi")


def _product_args(bin_path: str, prompt: str) -> list[str]:
    """Product-shaped argv (``--output-format``, ``--model``, ``--print=``) minus bin."""
    return build_agy_argv(bin_path=bin_path, model="probe-model", prompt=prompt)[1:]


class _ProbeMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.probe = self.root / "probe.py"
        self.probe.write_text(_PROBE, encoding="utf-8")
        self.out = self.root / "argv.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> list[str]:
        if self.out.exists():
            self.out.unlink()
        env = dict(os.environ)
        env[_OUT_ENV] = str(self.out)
        # Same Popen shape as the product's agy spawn (list argv, no shell).
        proc = subprocess.run(
            argv, cwd=str(self.root), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"probe failed: {proc.stderr!r}")
        self.assertTrue(self.out.exists(), "probe child never ran / wrote argv")
        return json.loads(self.out.read_text(encoding="utf-8"))


class ExplicitPythonTransportTests(_ProbeMixin, unittest.TestCase):
    """Control: explicit interpreter launch keeps every argument verbatim."""

    def test_every_prompt_arrives_verbatim(self):
        for name, prompt in PROMPTS.items():
            with self.subTest(prompt=name):
                args = _product_args(str(self.probe), prompt)
                got = self._run([sys.executable, str(self.probe), *args])
                self.assertEqual(got, args)
                self.assertEqual(got[-1], PRINT_FLAG_PREFIX + prompt)


@unittest.skipUnless(_WINDOWS, "Windows system cmd batch forwarding only")
class BatchForwarderTransportTests(_ProbeMixin, unittest.TestCase):
    """REPRODUCED test-forwarder behaviour: ``probe.cmd`` forwarding ``%*``.

    This is the knife-9 fake_agy.cmd shape, not the installed agy.cmd.
    """

    def setUp(self):
        super().setUp()
        for p in (sys.executable, str(self.probe)):
            if not p.isascii():
                self.skipTest("non-ASCII interpreter/temp path; batch text encoding out of scope")
        self.cmd = self.root / "probe.cmd"
        self.cmd.write_text(f'@"{sys.executable}" "{self.probe}" %*\r\n', encoding="ascii")

    def test_single_line_prompts_arrive_verbatim(self):
        for name in SINGLE_LINE:
            with self.subTest(prompt=name):
                args = _product_args(str(self.cmd), PROMPTS[name])
                self.assertEqual(self._run([str(self.cmd), *args]), args)

    def test_multiline_prompt_is_truncated_at_batch_boundary(self):
        """Characterization: the child sees only the first line of ``--print=``.

        If this starts failing, the forwarding behaviour changed; re-check the
        claim doc before touching it. Do not widen it into a pass-anything test.
        """
        for name in MULTI_LINE:
            with self.subTest(prompt=name):
                prompt = PROMPTS[name]
                args = _product_args(str(self.cmd), prompt)
                got = self._run([str(self.cmd), *args])
                self.assertEqual(got[:-1], args[:-1], "flags before --print= must survive")
                first = PRINT_FLAG_PREFIX + prompt.split("\n", 1)[0]
                self.assertEqual(got[-1], first)
                self.assertNotEqual(got[-1], args[-1])

    def test_explicit_python_contrast_on_same_args(self):
        args = _product_args(str(self.cmd), PROMPTS["multi"])
        direct = self._run([sys.executable, str(self.probe), *args])
        batch = self._run([str(self.cmd), *args])
        self.assertEqual(direct, args)
        self.assertNotEqual(batch, args)


class CmdWrapperBoundaryTests(unittest.TestCase):
    """Existing product wrapper (windows_cli.CmdWrapper): construction only.

    It refuses CR/LF in args instead of letting cmd truncate them. Nothing is
    launched; the injected cmd path need not exist.
    """

    def setUp(self):
        self.w = CmdWrapper(cmd_exe=os.path.join(tempfile.gettempdir(), "cmd.exe"))
        self.script = os.path.join(tempfile.gettempdir(), "agy.cmd")

    def test_multiline_args_are_rejected_not_truncated(self):
        for name in MULTI_LINE:
            with self.subTest(prompt=name):
                with self.assertRaises(CliLaunchError):
                    self.w.wrap(self.script, (PRINT_FLAG_PREFIX + PROMPTS[name],), None, None)

    def test_single_line_args_pass_through_unchanged(self):
        for name in SINGLE_LINE:
            with self.subTest(prompt=name):
                arg = PRINT_FLAG_PREFIX + PROMPTS[name]
                spec = self.w.wrap(self.script, (arg,), None, None)
                self.assertEqual(spec.argv[-1], arg)
                self.assertFalse(spec.shell)


class InstalledAgyCmdPending(unittest.TestCase):
    @unittest.skip("PENDING: installed agy.cmd launcher text/version not reviewed; "
                   "do not run the real agy (no version/doctor/login). See claim doc.")
    def test_installed_agy_cmd_multiline_transport(self):
        pass


if __name__ == "__main__":
    unittest.main()
