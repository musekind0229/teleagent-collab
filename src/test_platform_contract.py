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


if __name__ == "__main__":
    unittest.main()
