import unittest
import json
import re
import hashlib
import base64
from pathlib import Path

from src.chat_local_report import generate_html_report, run_report


class TestDisplayFields(unittest.TestCase):
    def setUp(self):
        self.output_dir = Path("test_display_output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.html_path = self.output_dir / "display_report.html"

    def tearDown(self):
        if self.html_path.exists():
            self.html_path.unlink()
        for p in self.output_dir.glob("*"):
            if p.is_file():
                p.unlink()
        if self.output_dir.exists():
            try:
                self.output_dir.rmdir()
            except OSError:
                pass

    def test_synthetic_display_json_rendering_and_csp(self):
        """Test rendering records from inputs/SYNTHETIC-DISPLAY.json."""
        display_file = Path("inputs/SYNTHETIC-DISPLAY.json")
        self.assertTrue(display_file.exists(), "inputs/SYNTHETIC-DISPLAY.json must exist")
        records = json.loads(display_file.read_text(encoding="utf-8"))

        generate_html_report(records, self.html_path)
        content = self.html_path.read_text(encoding="utf-8")

        # 1. CSP check: exact hash, default-src 'none', script-src-attr 'none'
        csp_match = re.search(r'<meta\s+http-equiv=["\']Content-Security-Policy["\']\s+content="([^"]+)"', content)
        self.assertIsNotNone(csp_match, "CSP meta tag must be present")
        csp_policy = csp_match.group(1)

        directives = {}
        for part in csp_policy.split(";"):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            directives[tokens[0]] = tokens[1:]

        self.assertEqual(directives.get("default-src"), ["'none'"])
        self.assertEqual(directives.get("script-src-attr"), ["'none'"])
        self.assertIn("style-src", directives)
        self.assertIn("'unsafe-inline'", directives["style-src"])
        self.assertNotIn("'unsafe-inline'", directives.get("script-src", []))

        # Check script hash validity
        script_match = re.search(r'<script>(.*?)</script>', content, flags=re.DOTALL)
        self.assertIsNotNone(script_match)
        script_text = script_match.group(1)
        computed_hash = base64.b64encode(hashlib.sha256(script_text.encode("utf-8")).digest()).decode("ascii")
        self.assertIn(f"'sha256-{computed_hash}'", directives.get("script-src", []))

        # 2. Safe JSON embedding check
        data_match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', content, flags=re.DOTALL)
        self.assertIsNotNone(data_match)
        embedded_json = data_match.group(1)
        roundtrip_records = json.loads(embedded_json)
        self.assertEqual(len(roundtrip_records), len(records))
        self.assertEqual(roundtrip_records, records)

        # 3. Exactly two script tags
        script_closing_tags = re.findall(r"<\s*/\s*script\s*>", content, flags=re.IGNORECASE)
        self.assertEqual(len(script_closing_tags), 2)

    def test_dom_inertness_and_no_inner_html(self):
        """Verify DOM creation uses textContent and createElement, never innerHTML or anchor tags."""
        generate_html_report([], self.html_path)
        content = self.html_path.read_text(encoding="utf-8")

        # No innerHTML in client script
        self.assertNotIn(".innerHTML", content)
        # Ensure textContent is used
        self.assertIn(".textContent", content)
        self.assertIn("createElement", content)

        # Source locator must be inert text, never a link or source opener
        self.assertNotIn("createElement(\"a\")", content)
        self.assertNotIn("href", content)
        self.assertNotIn("window.open", content)

        # No inline handlers in HTML
        self.assertNotIn("onclick=", content.lower())
        self.assertNotIn("onload=", content.lower())
        self.assertNotIn("onerror=", content.lower())

    def test_six_filters_and_record_count_present(self):
        """Ensure all 6 filters and recordCount exist in the generated viewer."""
        generate_html_report([], self.html_path)
        content = self.html_path.read_text(encoding="utf-8")

        expected_filter_ids = [
            "filterTopic",
            "filterSubject",
            "filterNegation",
            "filterConfidence",
            "filterTruncated",
            "filterSensitive",
        ]
        for fid in expected_filter_ids:
            self.assertIn(f'id="{fid}"', content)
            self.assertIn(f'getElementById("{fid}")', content)

        self.assertIn('id="recordCount"', content)
        self.assertIn('id="tableBody"', content)
        self.assertIn('id="dataTable"', content)

    def test_field_display_empty_raw_time_and_multiline_context(self):
        """Test empty time/locator/context clear fallback, and raw time preservation."""
        record_with_values = {
            "topic": "testing",
            "summary_local_rule_inference": "Local rule summary",
            "quote_verbatim": "Test quote",
            "minimal_context": "Line 1\nLine 2\nLine 3 & <escape-test>",
            "source_locator_LOCAL_ONLY": "relative/path/to/source.jsonl:10",
            "source_line": 10,
            "event_time_raw": "2026-10-17T12:34:56.789Z",
            "subject": "first-person",
            "negation": False,
            "time_qualification": "recent",
            "contradiction_review": False,
            "confidence": "high",
            "truncated": False,
            "sensitive": False,
        }
        record_empty = {
            "topic": "testing-empty",
            "summary_local_rule_inference": "",
            "quote_verbatim": "",
            "minimal_context": "",
            "source_locator_LOCAL_ONLY": "",
            "source_line": 11,
            "event_time_raw": "",
            "subject": "third-party",
            "negation": True,
            "time_qualification": "historical",
            "contradiction_review": True,
            "confidence": "low",
            "truncated": True,
            "sensitive": True,
        }

        generate_html_report([record_with_values, record_empty], self.html_path)
        content = self.html_path.read_text(encoding="utf-8")

        # Verify fallback strings are present in script logic
        self.assertIn("Time not provided", content)
        self.assertIn("Context not provided", content)
        self.assertIn("Locator not provided", content)
        self.assertIn("Unverified local inference:", content)
        self.assertIn("Quote verbatim:", content)
        self.assertIn("Source locator (inert text):", content)

        # Verify roundtrip preserved exact raw time without mtime conversion
        match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', content, flags=re.DOTALL)
        parsed = json.loads(match.group(1))
        self.assertEqual(parsed[0]["event_time_raw"], "2026-10-17T12:34:56.789Z")
        self.assertEqual(parsed[1]["event_time_raw"], "")

    def test_injection_strings_inertness(self):
        """Test dangerous injection strings: </script>, onclick, <svg>, ampersands, delimiters."""
        injection_record = {
            "topic": "inject & ' \" <tag>",
            "summary_local_rule_inference": "</script><script>alert(1)</script>",
            "quote_verbatim": "onclick=\"alert(2)\" <img src=x onerror=alert(3)>",
            "minimal_context": "<svg onload=alert(4)>\n&amp;\n;DROP TABLE chats;--",
            "source_locator_LOCAL_ONLY": "path/with/\"quotes\"/and/<brackets>/chat.jsonl:99",
            "source_line": 99,
            "event_time_raw": "2026-01-01 <script>eval('bad')</script>",
            "subject": "synthetic injection",
            "negation": False,
            "time_qualification": "historical",
            "contradiction_review": False,
            "confidence": "test",
            "truncated": False,
            "sensitive": False,
        }

        generate_html_report([injection_record], self.html_path)
        content = self.html_path.read_text(encoding="utf-8")

        # Must have exactly 2 closing script tags
        script_closing_tags = re.findall(r"<\s*/\s*script\s*>", content, flags=re.IGNORECASE)
        self.assertEqual(len(script_closing_tags), 2)

        # Embedded JSON must round-trip exactly
        match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', content, flags=re.DOTALL)
        parsed = json.loads(match.group(1))
        self.assertEqual(parsed[0]["summary_local_rule_inference"], injection_record["summary_local_rule_inference"])
        self.assertEqual(parsed[0]["quote_verbatim"], injection_record["quote_verbatim"])
        self.assertEqual(parsed[0]["minimal_context"], injection_record["minimal_context"])
        self.assertEqual(parsed[0]["source_locator_LOCAL_ONLY"], injection_record["source_locator_LOCAL_ONLY"])
        self.assertEqual(parsed[0]["event_time_raw"], injection_record["event_time_raw"])

    def test_run_report_integration_output(self):
        """Test actual run_report direct caller and verify viewer HTML generation and control summary."""
        # Create a small valid manifest pointing to SYNTHETIC-CHAT.json
        manifest_path = self.output_dir / "test_manifest.txt"
        chat_path = Path("inputs/SYNTHETIC-CHAT.json").resolve()
        self.assertTrue(chat_path.exists())

        manifest_data = {
            "allowlisted_roots": [str(chat_path.parent)],
            "files": [str(chat_path)],
        }
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        report_out_dir = self.output_dir / "report_run"
        control = run_report(manifest_path, report_out_dir)

        self.assertTrue(control["is_success"])
        self.assertGreater(control["records_extracted_count"], 0)

        # Verify viewer was generated
        viewer_html = report_out_dir / "report_local_viewer.html"
        self.assertTrue(viewer_html.exists())
        viewer_text = viewer_html.read_text(encoding="utf-8")

        # Verify new viewer features present in integration output
        self.assertIn("Event Time (Raw)", viewer_text)
        self.assertIn("Details (Summary / Quote / Context / Locator)", viewer_text)
        self.assertIn("Unverified local inference:", viewer_text)
        self.assertIn("Quote verbatim:", viewer_text)
        self.assertIn("Source locator (inert text):", viewer_text)
        self.assertIn("Context not provided", viewer_text)


if __name__ == "__main__":
    unittest.main()
