import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from survey_reliability import source_boundary, inspect_python, location_relevance, capture_binary


class CollectorReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(os.environ['TEST_WORKSPACE'])
        cls.base.mkdir(exist_ok=True)
        cls.marker = cls.base / 'plain-marker.txt'
        cls.marker.write_text('SYNTHETIC DATA ONLY')
        target = cls.base / 'redirect-target'
        target.mkdir(exist_ok=True)
        (target / 'marker.txt').write_text('SYNTHETIC DATA ONLY')
        link = cls.base / 'redirect-link'
        if not link.exists():
            if os.name == 'nt':
                capture_binary(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)])
            else:
                os.symlink(target, link, target_is_directory=True)
        cls.redirect_ready = link.exists()


    def test_normal_existing_source(self):
        self.assertTrue(source_boundary(self.marker, [self.base])[0])

    def test_missing_parent_traversal_rejected_before_probe(self):
        p = self.base / 'nonexistent-parent' / '..' / 'redirect-link' / 'marker.txt'
        self.assertEqual(source_boundary(p, [self.base]), (False, 'nonlocal_or_parent_traversal'))

    def test_actual_junction_direct_rejected(self):
        self.assertEqual(source_boundary(self.base/'redirect-link'/'marker.txt', [self.base]), (False, 'redirect_rejected'))

    def test_missing_source_fail_closed(self):
        self.assertEqual(source_boundary(self.base/'missing-file.txt', [self.base]), (False, 'missing_source'))

    def test_outside_root_rejected(self):
        self.assertEqual(source_boundary(self.marker, [self.base/'unrelated']), (False, 'outside_caller_roots'))

    def test_relative_rejected(self):
        self.assertFalse(source_boundary(Path('relative.txt'), [self.base])[0])

    def test_network_rejected(self):
        self.assertFalse(source_boundary(Path(r'\\synthetic-server\share\item'), [self.base])[0])

    def test_invalid_caller_root_rejected(self):
        self.assertEqual(source_boundary(self.marker, [self.base/'..'/'other']), (False, 'invalid_caller_root'))

    def test_protected_name(self):
        self.assertEqual(source_boundary(self.base/'.env.synthetic', [self.base]), (False, 'protected_location'))

    def test_ast_warning_captured_not_printed(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            result = inspect_python("def f():\n return '\\p'\n")
        self.assertIsNotNone(result.tree)
        self.assertGreater(result.warning_count, 0)
        self.assertEqual(stream.getvalue(), '')

    def test_ast_error_structured(self):
        result = inspect_python('def broken(:\n')
        self.assertEqual(result.error_kind, 'SyntaxError')
        self.assertIsNone(result.tree)

    def test_source_never_executed(self):
        self.assertIsNotNone(inspect_python("raise RuntimeError('synthetic code must not run')").tree)

    def test_system_prefix_not_product_absence(self):
        self.assertEqual(location_relevance('C:/Windows/System32/synthetic-personal-project/index.html'),
                         'system_or_installed_location_relevance_unknown')

    def test_explicit_runtime_subtree_component(self):
        self.assertEqual(location_relevance('C:/Windows/System32/WindowsPowerShell/v1.0/Modules/Synthetic/script.ps1'),
                         'runtime_component_location_not_authorship_proof')

    def test_normal_project_location_candidate(self):
        self.assertEqual(location_relevance(self.base/'normal-project/index.html'),
                         'ordinary_location_candidate_not_authorship_proof')

    def test_non_utf8_native_capture(self):
        result = capture_binary([sys.executable, '-B', '-c', 'import os;os.write(1,bytes([180,250,0]));os.write(2,bytes([255]))'])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, bytes([180,250,0]))
        self.assertEqual(result.stderr, bytes([255]))


if __name__ == '__main__':
    unittest.main()
