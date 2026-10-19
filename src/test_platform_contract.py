"""Platform contract tests (cli LaunchSpec, path inspection, capability report).

Runnable on POSIX and Windows. Uses sys.executable + fake .py scripts only.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

import platform_services as ps
from platform_services.cli import (
    WRAPPER_DIRECT, WRAPPER_INTERPRETER, WRAPPER_WINDOWS_CMD,
    CliLaunchError, LaunchSpec, PlatformWrapperRequired, prepare_cli,
)
from platform_services.paths import (
    ESCAPE, INSIDE, UNKNOWN, PathInspection, PathSafetyError, inspect_path, require_safe,
)

IS_WIN = sys.platform.startswith("win")


class CliContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.script = self.d / "fake_cli.py"
        self.script.write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_interpreter_argv_roundtrip_no_shell(self):
        prompt = 'he said "hi"; rm -rf / && $(x) `y` %PATH%'
        spec = prepare_cli(str(self.script), [prompt, "--x"], cwd=str(self.d),
                           interpreter=sys.executable)
        self.assertEqual(spec.wrapper_kind, WRAPPER_INTERPRETER)
        self.assertFalse(spec.shell)
        self.assertIsInstance(spec.argv, tuple)
        out = subprocess.run(list(spec.argv), cwd=spec.cwd, capture_output=True,
                             text=True, check=True)
        self.assertEqual(out.stdout.strip(), '["he said \\"hi\\"; rm -rf / && $(x) `y` %PATH%", "--x"]')

    def test_string_args_rejected(self):
        with self.assertRaises(CliLaunchError):
            prepare_cli(str(self.script), "a b", interpreter=sys.executable)
        with self.assertRaises(CliLaunchError):
            prepare_cli(str(self.script), ["ok", 3], interpreter=sys.executable)

    def test_missing_binary_rejected(self):
        with self.assertRaises(CliLaunchError):
            prepare_cli(str(self.d / "nope.py"), [], interpreter=sys.executable)

    def test_posix_direct_requires_exec_bit_and_shebang(self):
        if IS_WIN:
            self.skipTest("POSIX exec-bit semantics")
        s = self.d / "tool"
        s.write_text(f"#!{sys.executable}\nimport sys; print(sys.argv[1])\n")
        with self.assertRaises(CliLaunchError):
            prepare_cli(str(s), ["x"], platform="linux")
        s.chmod(s.stat().st_mode | stat.S_IXUSR)
        spec = prepare_cli(str(s), ["x y"], platform="linux")
        self.assertEqual(spec.wrapper_kind, WRAPPER_DIRECT)
        out = subprocess.run(list(spec.argv), capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "x y")
        n = self.d / "noshebang"
        n.write_text("echo hi\n")
        n.chmod(0o755)
        with self.assertRaises(CliLaunchError):
            prepare_cli(str(n), [], platform="linux")

    def test_windows_cmd_needs_wrapper(self):
        c = self.d / "agent.cmd"
        c.write_text("@echo off\n")
        with self.assertRaises(PlatformWrapperRequired):
            prepare_cli(str(c), ["p"], platform="win32")
        self.assertTrue(issubclass(PlatformWrapperRequired, NotImplementedError))

    def test_windows_cmd_hook_contract(self):
        c = self.d / "agent.bat"
        c.write_text("@echo off\n")

        class Fake:
            def wrap(self, script, args, cwd, env):
                return LaunchSpec(("wrapper.exe", script, *args), cwd, env, WRAPPER_WINDOWS_CMD)

        spec = prepare_cli(str(c), ["a&b"], platform="win32", cmd_wrapper=Fake())
        self.assertEqual(spec.wrapper_kind, WRAPPER_WINDOWS_CMD)
        self.assertEqual(spec.argv[-1], "a&b")

    def test_env_path_lookup_semantics(self):
        if IS_WIN:
            self.skipTest("POSIX exec-bit lookup")
        b = self.d / "bin"
        b.mkdir()
        t = b / "mytool"
        t.write_text(f"#!{sys.executable}\n")
        t.chmod(0o755)
        name = Path(sys.executable).name
        # env={} -> no PATH -> bare name not found even though parent PATH has it
        with self.assertRaises(CliLaunchError):
            prepare_cli(name, [], env={})
        with self.assertRaises(CliLaunchError):
            prepare_cli("mytool", [], env={"HOME": "/x"})
        spec = prepare_cli("mytool", [], env={"PATH": str(b)})
        self.assertEqual(spec.argv[0], str(t.absolute()))
        # absolute path still works with empty env
        self.assertEqual(prepare_cli(str(t), [], env={}).argv[0], str(t.absolute()))
        # env=None -> parent PATH
        prepare_cli(str(self.script), [], interpreter=name)

    def test_windows_non_exe_needs_interpreter(self):
        with self.assertRaises(CliLaunchError):
            prepare_cli(str(self.script), [], platform="win32")


class PathContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        (self.root / "sub").mkdir(parents=True)
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _symlink(self, link: Path, target, is_dir=True):
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlink creation not permitted on this host: {e}")

    def test_inside_plain_and_nonexistent(self):
        for rel in ("sub/a.txt", "sub/new/deeper.txt", "./sub/../sub/x"):
            r = inspect_path(str(self.root), rel, "write")
            self.assertEqual(r.verdict, INSIDE, rel)
            self.assertEqual(require_safe(r), r.normalized)

    def test_dotdot_and_absolute_escape(self):
        self.assertEqual(inspect_path(str(self.root), "../outside/x", "read").verdict, ESCAPE)
        self.assertEqual(inspect_path(str(self.root), "sub/../../x", "read").verdict, ESCAPE)
        self.assertEqual(inspect_path(str(self.root), str(self.outside), "read").verdict, ESCAPE)
        with self.assertRaises(PathSafetyError):
            require_safe(inspect_path(str(self.root), "../x", "read"))

    def test_bad_intent(self):
        with self.assertRaises(ValueError):
            inspect_path(str(self.root), "x", "chmod")

    def test_unknown_rejected_for_protected_only(self):
        u = PathInspection(str(self.root), str(self.root / "x"), (), UNKNOWN, "write", "race")
        with self.assertRaises(PathSafetyError):
            require_safe(u)
        for intent in ("delete", "exec"):
            with self.assertRaises(PathSafetyError):
                require_safe(PathInspection(u.root, u.normalized, (), UNKNOWN, intent))
        r = PathInspection(u.root, u.normalized, (), UNKNOWN, "read")
        self.assertEqual(require_safe(r), u.normalized)

    def test_symlink_escape_absolute(self):
        self._symlink(self.root / "evil", str(self.outside))
        r = inspect_path(str(self.root), "evil/x.txt", "write")
        self.assertEqual(r.verdict, ESCAPE)
        self.assertEqual(len(r.link_chain), 1)

    def test_symlink_escape_relative(self):
        self._symlink(self.root / "sub" / "up", os.path.join("..", "..", "outside"))
        self.assertEqual(inspect_path(str(self.root), "sub/up/x", "delete").verdict, ESCAPE)

    def test_symlink_inside_chain(self):
        self._symlink(self.root / "l1", "sub")
        self._symlink(self.root / "l2", "l1")
        r = inspect_path(str(self.root), "l2/f", "write")
        self.assertEqual(r.verdict, INSIDE)
        self.assertEqual(len(r.link_chain), 2)
        self.assertTrue(r.normalized.endswith(os.path.normcase(os.path.join("sub", "f"))))

    def test_simulated_junction_inside_absolute_symlink_target(self):
        # Simulated seam (no real symlink/junction needed): root/abs is a
        # symlink to <root>/j/x, and <root>/j is a non-symlink reparse point.
        root = os.path.normcase(os.path.realpath(str(self.root)))
        link, junction = os.path.join(root, "abs"), os.path.join(root, "j")
        (self.root / "j").mkdir()
        real_lstat = os.lstat

        def fake_lstat(p, *a, **k):
            p = os.path.normcase(str(p))
            if p == link:
                return types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
            if p == junction:
                return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
            return real_lstat(p, *a, **k)

        with mock.patch("platform_services.paths.os.lstat", fake_lstat), \
                mock.patch("platform_services.paths.os.readlink",
                           lambda p: os.path.join(root, "j", "x")), \
                mock.patch("platform_services.paths.os.path.realpath",
                           return_value=root) as rp:
            # realpath is allowed only for the trusted root, never link targets
            r = inspect_path(str(self.root), "abs/file.txt", "write")
            self.assertEqual(rp.call_count, 1)
        self.assertEqual(r.verdict, UNKNOWN, r)
        self.assertEqual(len(r.link_chain), 1)
        for intent in ("write", "delete", "exec"):
            with self.assertRaises(PathSafetyError):
                require_safe(PathInspection(r.root, r.normalized, r.link_chain, r.verdict, intent))

    def test_absolute_symlink_target_walked_inside(self):
        self._symlink(self.root / "absin", str(self.root / "sub"))
        r = inspect_path(str(self.root), "absin/f", "write")
        self.assertEqual(r.verdict, INSIDE)
        self.assertEqual(len(r.link_chain), 1)

    def test_absolute_symlink_target_chain_through_inner_link(self):
        self._symlink(self.root / "inner", str(self.outside))
        self._symlink(self.root / "outer", str(self.root / "inner"))
        r = inspect_path(str(self.root), "outer/f", "write")
        self.assertEqual(r.verdict, ESCAPE)
        self.assertEqual(len(r.link_chain), 2)

    def test_symlink_loop_unknown(self):
        self._symlink(self.root / "a", "b")
        self._symlink(self.root / "b", "a")
        r = inspect_path(str(self.root), "a/x", "write")
        self.assertEqual(r.verdict, UNKNOWN)
        with self.assertRaises(PathSafetyError):
            require_safe(r)


class CapabilityReportTests(unittest.TestCase):
    def test_current_host_report(self):
        rep = ps.capability_report()
        for k in ("platform", "kind", "file_lock", "process", "cli_cmd_wrapper", "path_inspect"):
            self.assertIn(k, rep)

    def test_unknown_platform_raises_no_fallback(self):
        with self.assertRaises(ps.FileLockUnsupported):
            ps.capability_report(platform="plan9")

    def test_posix_has_no_cmd_wrapper(self):
        self.assertIsNone(ps.get_cmd_wrapper(platform="linux"))

    def test_windows_cli_module_absent_is_wrapper_required(self):
        err = ModuleNotFoundError("No module named 'platform_services.windows_cli'",
                                  name="platform_services.windows_cli")
        with mock.patch("platform_services.importlib.import_module", side_effect=err):
            with self.assertRaises(PlatformWrapperRequired):
                ps.get_cmd_wrapper(platform="win32")

    def test_windows_cli_internal_dep_missing_propagates(self):
        err = ModuleNotFoundError("No module named 'pywintypes'", name="pywintypes")
        with mock.patch("platform_services.importlib.import_module", side_effect=err):
            with self.assertRaises(ModuleNotFoundError) as cm:
                ps.get_cmd_wrapper(platform="win32")
        self.assertNotIsInstance(cm.exception, PlatformWrapperRequired)
        self.assertEqual(cm.exception.name, "pywintypes")
        other = ImportError("cannot import name 'X'")
        with mock.patch("platform_services.importlib.import_module", side_effect=other):
            with self.assertRaises(ImportError):
                ps.get_cmd_wrapper(platform="win32")



# ---------------------------------------------------------------------------
# Knife 6: Windows CmdWrapper contract (Hermes a26).
# Until platform_services.windows_cli exists these tests MUST fail (red SHA),
# never skipTest on ImportError.
# ---------------------------------------------------------------------------

_DANGEROUS = ('\x00', '\r', '\n', '"', '^', '&', '|', '<', '>', '%', '!', '(', ')')


def _require_win_cmd_wrapper():
    """Load real CmdWrapper or fail hard (red until Windows delivers the module)."""
    try:
        from platform_services.windows_cli import CmdWrapper
    except ImportError as e:
        raise AssertionError(
            "platform_services.windows_cli.CmdWrapper missing; "
            "feat/windows-cmd-wrapper-06 expects RED until Windows delivers the module"
        ) from e
    return CmdWrapper


class WindowsCmdWrapperContractTests(unittest.TestCase):
    """Cross-platform contract for windows_cli.CmdWrapper (Hermes a26)."""

    def setUp(self):
        self.CmdWrapper = _require_win_cmd_wrapper()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.cmd = self.d / "agent.cmd"
        self.cmd.write_text("@echo off\r\necho ok\r\n", encoding="utf-8")
        self.bat = self.d / "agent.bat"
        self.bat.write_text("@echo off\r\necho ok\r\n", encoding="utf-8")
        # Fake cmd.exe path for Linux / structure assertions (no real System32).
        self.fake_cmd = self.d / "cmd.exe"
        self.fake_cmd.write_text("fake\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _wrap(self, script, args=(), cwd=None, env=None):
        return self.CmdWrapper(cmd_exe=str(self.fake_cmd)).wrap(
            str(script), tuple(args), cwd, env
        )

    def test_construct_and_signature(self):
        w = self.CmdWrapper(cmd_exe=str(self.fake_cmd))
        self.assertTrue(callable(w.wrap))
        spec = w.wrap(str(self.cmd), (), None, None)
        self.assertIsInstance(spec, LaunchSpec)
        self.assertEqual(spec.wrapper_kind, WRAPPER_WINDOWS_CMD)
        self.assertFalse(spec.shell)
        self.assertIsInstance(spec.argv, tuple)
        self.assertTrue(all(isinstance(a, str) for a in spec.argv))

    def test_argv_has_fixed_switches_no_call(self):
        spec = self._wrap(self.cmd, ("a", "b"))
        argv_l = [a.lower() for a in spec.argv]
        self.assertEqual(spec.argv[0], str(self.fake_cmd))
        for flag in ("/d", "/v:off", "/s", "/c"):
            self.assertIn(flag, spec.argv, flag)
        joined = " ".join(argv_l)
        self.assertNotIn("call", joined.split())
        # /c must appear; payload after it is Windows-owned spelling
        c_idx = list(spec.argv).index("/c")
        self.assertGreater(c_idx, 0)
        self.assertLess(c_idx, len(spec.argv) - 1)

    def test_cwd_env_preserved_identity(self):
        env = {"FOO": "bar", "PATH": "C:\\none"}
        cwd = str(self.d)
        spec = self._wrap(self.cmd, (), cwd=cwd, env=env)
        self.assertEqual(spec.cwd, cwd)
        self.assertIs(spec.env, env)
        spec_none = self._wrap(self.cmd, (), cwd=None, env=None)
        self.assertIsNone(spec_none.cwd)
        self.assertIsNone(spec_none.env)

    def test_accepts_cmd_and_bat_case_insensitive(self):
        for name in ("x.CMD", "y.BaT", "z.cmd", "w.bat"):
            p = self.d / name
            p.write_text("@echo off\n", encoding="utf-8")
            spec = self._wrap(p, ())
            self.assertEqual(spec.wrapper_kind, WRAPPER_WINDOWS_CMD)

    def test_rejects_non_cmd_suffix(self):
        for name in ("x.exe", "x.py", "x.ps1", "x", "x.cmd.bak"):
            p = self.d / name
            p.write_text("x\n", encoding="utf-8")
            with self.assertRaises(CliLaunchError):
                self._wrap(p, ())

    def test_rejects_dangerous_chars_in_script_and_args(self):
        for ch in _DANGEROUS:
            if ch == "\x00":
                # Path with NUL cannot be created as a file; call wrap with a
                # synthetic path string (wrapper must reject before filesystem).
                bad_script = str(self.d / f"bad{ch}x.cmd")
                with self.assertRaises(CliLaunchError):
                    self.CmdWrapper(cmd_exe=str(self.fake_cmd)).wrap(
                        bad_script, (), None, None
                    )
            else:
                # Prefer arg rejection (script path with some chars is awkward on disk)
                with self.subTest(ch=repr(ch)):
                    with self.assertRaises(CliLaunchError):
                        self._wrap(self.cmd, (f"a{ch}b",))

    def test_rejects_trailing_backslash_arg(self):
        with self.assertRaises(CliLaunchError):
            self._wrap(self.cmd, ("trail\\",))
        with self.assertRaises(CliLaunchError):
            self._wrap(self.cmd, ("ok", "end\\"))

    def test_rejects_empty_script_and_joined_args(self):
        with self.assertRaises(CliLaunchError):
            self.CmdWrapper(cmd_exe=str(self.fake_cmd)).wrap("", (), None, None)
        with self.assertRaises((CliLaunchError, TypeError)):
            self.CmdWrapper(cmd_exe=str(self.fake_cmd)).wrap(
                str(self.cmd), "a b", None, None  # type: ignore[arg-type]
            )
        with self.assertRaises(CliLaunchError):
            self.CmdWrapper(cmd_exe=str(self.fake_cmd)).wrap(
                str(self.cmd), ("ok", 3), None, None  # type: ignore[arg-type]
            )

    def test_no_temp_file_side_effects(self):
        before = {p.name for p in self.d.iterdir()}
        self._wrap(self.cmd, ("x", "y"))
        after = {p.name for p in self.d.iterdir()}
        self.assertEqual(before, after)

    def test_prepare_cli_explicit_inject_uses_wrapper(self):
        w = self.CmdWrapper(cmd_exe=str(self.fake_cmd))
        spec = prepare_cli(
            str(self.cmd), ["hello"], platform="win32", cmd_wrapper=w, cwd=str(self.d)
        )
        self.assertEqual(spec.wrapper_kind, WRAPPER_WINDOWS_CMD)
        self.assertFalse(spec.shell)
        self.assertEqual(spec.argv[0], str(self.fake_cmd))

    def test_prepare_cli_without_inject_still_requires_wrapper(self):
        # Module presence must not auto-wire prepare_cli.
        with self.assertRaises(PlatformWrapperRequired):
            prepare_cli(str(self.cmd), ["p"], platform="win32")

    def test_spaces_and_unicode_args_accepted_in_spec(self):
        spaced = self.d / "my agent.cmd"
        spaced.write_text("@echo off\n", encoding="utf-8")
        spec = self._wrap(spaced, ("你好", "a b", ""))
        self.assertEqual(spec.wrapper_kind, WRAPPER_WINDOWS_CMD)
        self.assertFalse(spec.shell)


@unittest.skipUnless(IS_WIN, "real cmd.exe roundtrip only on Windows")
class WindowsCmdWrapperLiveTests(unittest.TestCase):
    """Windows-only: real subprocess.run(spec.argv, shell=False) via cmd.exe."""

    def setUp(self):
        self.CmdWrapper = _require_win_cmd_wrapper()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.recorder = self.d / "record_argv.py"
        self.recorder.write_text(
            "import json, os, sys\n"
            "open(sys.argv[1], 'w', encoding='utf-8').write(\n"
            "  json.dumps({'argv': sys.argv[2:], 'cwd': os.getcwd(),\n"
            "              'FOO': os.environ.get('FOO'), 'marker': 'ran'}))\n",
            encoding="utf-8",
        )
        self.out = self.d / "out.json"
        # .cmd that forwards remaining args to python recorder
        self.script = self.d / "forward.cmd"
        # %* after shifting is fragile; use explicit python -c free form via args
        self.script.write_text(
            f'@echo off\r\n"{sys.executable}" "{self.recorder}" %*\r\n',
            encoding="utf-8",
        )
        self.marker = self.d / "should_not_exist.flag"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, script, args, cwd=None, env=None):
        spec = self.CmdWrapper().wrap(str(script), tuple(args), cwd, env)
        self.assertFalse(spec.shell)
        return subprocess.run(
            list(spec.argv), cwd=spec.cwd, env=spec.env, capture_output=True, text=True
        )

    def test_roundtrip_ordinary_empty_unicode_spaced(self):
        import json
        spaced = self.d / "path with spaces.cmd"
        spaced.write_text(self.script.read_text(encoding="utf-8"), encoding="utf-8")
        cwd = str(self.d / "work dir")
        Path(cwd).mkdir()
        for script, args in (
            (self.script, (str(self.out), "a", "b")),
            (self.script, (str(self.out), "")),
            (self.script, (str(self.out), "中文")),
            (spaced, (str(self.out), "x")),
        ):
            if self.out.exists():
                self.out.unlink()
            r = self._run(script, args, cwd=cwd)
            self.assertEqual(r.returncode, 0, msg=(r.stdout, r.stderr, args))
            data = json.loads(self.out.read_text(encoding="utf-8"))
            self.assertEqual(data["marker"], "ran")
            self.assertEqual(os.path.normcase(data["cwd"]), os.path.normcase(cwd))

    def test_explicit_env_passed(self):
        import json
        env = os.environ.copy()
        env["FOO"] = "from-test"
        if self.out.exists():
            self.out.unlink()
        r = self._run(self.script, (str(self.out),), env=env)
        self.assertEqual(r.returncode, 0, msg=(r.stdout, r.stderr))
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["FOO"], "from-test")

    def test_rejected_input_does_not_run_or_write_marker(self):
        bad = self.d / "bad&.cmd"
        # If we cannot create the file name, still call wrap with synthetic path.
        try:
            bad.write_text(
                f'@echo off\r\necho ran> "{self.marker}"\r\n', encoding="utf-8"
            )
            script = str(bad)
        except OSError:
            script = str(self.d / "synth&.cmd")
        with self.assertRaises(CliLaunchError):
            self.CmdWrapper().wrap(script, (), None, None)
        self.assertFalse(self.marker.exists())
        with self.assertRaises(CliLaunchError):
            self.CmdWrapper().wrap(str(self.script), ("a&b",), None, None)
        self.assertFalse(self.marker.exists())



if __name__ == "__main__":
    unittest.main()
