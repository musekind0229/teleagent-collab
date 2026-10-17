import unittest
import json
import re
import hashlib
import base64
from pathlib import Path
from src.chat_local_report import generate_html_report, _safe_json_script

class TestViewerSecurity(unittest.TestCase):
    def setUp(self):
        self.output_dir = Path("test_output")
        self.output_dir.mkdir(exist_ok=True)
        self.output_path = self.output_dir / "report.html"

    def tearDown(self):
        if self.output_path.exists():
            self.output_path.unlink()
        if self.output_dir.exists():
            try:
                self.output_dir.rmdir()
            except OSError:
                pass

    def test_synthetic_p0_payload_safe_embedding(self):
        """Test against inputs/SYNTHETIC-P0.json payload to ensure raw </script> is escaped."""
        p0_input_file = Path("inputs/SYNTHETIC-P0.json")
        if p0_input_file.exists():
            synthetic_data = json.loads(p0_input_file.read_text(encoding="utf-8"))
        else:
            synthetic_data = {
                "quote": "</script><script>window.SYNTHETIC_INJECTION_EXECUTED=true</script><script>"
            }

        record = {
            "source_line": 1,
            "topic": "security",
            "subject": "synthetic injection",
            "summary_local_rule_inference": "testing injection",
            "quote_verbatim": synthetic_data.get("quote"),
            "negation": False,
            "time_qualification": "now",
            "contradiction_review": "none",
            "confidence": "high",
            "truncated": False,
            "sensitive": False,
        }

        generate_html_report([record], self.output_path)
        content = self.output_path.read_text(encoding="utf-8")

        # 1. No unescaped closing </script> tags outside legitimate closing tags
        # Exactly two legitimate </script> closing tags: </script id="report-data" ...></script> and the runner <script>...</script>
        script_closing_tags = re.findall(r"<\s*/\s*script\s*>", content, flags=re.IGNORECASE)
        self.assertEqual(len(script_closing_tags), 2)

        # 2. Extract embedded JSON from the data script tag
        match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', content, flags=re.DOTALL)
        self.assertIsNotNone(match)
        json_blob = match.group(1)

        # 3. Ensure JSON parses back to the exact identical records without censorship
        parsed_records = json.loads(json_blob)
        self.assertEqual(len(parsed_records), 1)
        self.assertEqual(parsed_records[0]["quote_verbatim"], record["quote_verbatim"])

    def test_comprehensive_injection_vectors(self):
        """Test multiple pure synthetic injection strings: closing script, quotes, U+2028/U+2029, <img onerror>."""
        complex_record = {
            "source_line": 42,
            "topic": "<svg onload=alert(1)>",
            "subject": "\"><script>alert('xss')</script>",
            "summary_local_rule_inference": "Quotes & ampersands: ' \" & < > `",
            "quote_verbatim": "</script><img src=x onerror=alert(2)><!--\u2028\u2029-->",
            "negation": True,
            "time_qualification": "<marquee>2026</marquee>",
            "contradiction_review": "<b>review</b>",
            "confidence": "very <script> high",
            "truncated": False,
            "sensitive": True,
        }

        generate_html_report([complex_record], self.output_path)
        content = self.output_path.read_text(encoding="utf-8")

        # Check that no raw </script> appears other than the 2 standard tags
        script_closing_tags = re.findall(r"<\s*/\s*script\s*>", content, flags=re.IGNORECASE)
        self.assertEqual(len(script_closing_tags), 2)

        # Check CSP meta header
        self.assertIn("Content-Security-Policy", content)
        self.assertIn("default-src 'none'", content)

        # Check that static HTML markup does not use inline event handlers
        # (Filter controls should use addEventListener instead of inline onchange="...")
        self.assertNotIn("onchange=", content)

        # Verify exact round-trip preservation of all values
        match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', content, flags=re.DOTALL)
        self.assertIsNotNone(match)
        parsed = json.loads(match.group(1))
        self.assertEqual(parsed[0], complex_record)

        # Verify U+2028 and U+2029 are not present as raw unescaped code points in the script block
        self.assertNotIn("\u2028", match.group(1))
        self.assertNotIn("\u2029", match.group(1))

    def test_dom_inert_rendering_logic(self):
        """Verify that viewer.py does not use innerHTML for rendering records."""
        viewer_code = Path("src/chat_local_report.py").read_text(encoding="utf-8")
        self.assertNotIn(".innerHTML", viewer_code)
        self.assertIn(".textContent", viewer_code)
        self.assertIn("createElement", viewer_code)

    def test_csp_hash_trusted_script_integrity(self):
        """Verify that CSP allows only the trusted executable script by exact SHA-256 hash."""
        records = [
            {
                "source_line": 10,
                "topic": "alpha",
                "subject": "beta",
                "summary_local_rule_inference": "gamma",
                "quote_verbatim": "verbatim text with 'quotes' & symbols",
                "negation": False,
                "time_qualification": "recent",
                "contradiction_review": "none",
                "confidence": "high",
                "truncated": False,
                "sensitive": False,
            }
        ]
        generate_html_report(records, self.output_path)
        content = self.output_path.read_text(encoding="utf-8")

        # Extract CSP header
        csp_match = re.search(r'<meta\s+http-equiv=["\']Content-Security-Policy["\']\s+content="([^"]+)"', content)
        self.assertIsNotNone(csp_match, "Content-Security-Policy meta tag must be present")
        csp_policy = csp_match.group(1)

        # Parse CSP directives
        directives = {}
        for part in csp_policy.split(";"):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            directives[tokens[0]] = tokens[1:]

        # Verify default-src 'none'
        self.assertIn("default-src", directives)
        self.assertEqual(directives["default-src"], ["'none'"])

        # Verify style-src 'unsafe-inline'
        self.assertIn("style-src", directives)
        self.assertIn("'unsafe-inline'", directives["style-src"])

        # Verify script-src does NOT contain 'unsafe-inline'
        self.assertIn("script-src", directives)
        self.assertNotIn("'unsafe-inline'", directives["script-src"])

        # Verify script-src-attr 'none'
        self.assertIn("script-src-attr", directives)
        self.assertEqual(directives["script-src-attr"], ["'none'"])

        # Extract executable script text (second script tag)
        script_match = re.search(r'<script>(.*?)</script>', content, flags=re.DOTALL)
        self.assertIsNotNone(script_match, "Trusted executable script must be present")
        script_text = script_match.group(1)

        # Compute exact SHA-256 base64 hash of final rendered UTF-8 script text
        script_bytes = script_text.encode("utf-8")
        computed_hash = base64.b64encode(hashlib.sha256(script_bytes).digest()).decode("ascii")
        expected_hash_token = f"'sha256-{computed_hash}'"

        # Assert CSP script-src allows this exact hash
        self.assertIn(expected_hash_token, directives["script-src"])

    def test_synthetic_red_demonstration_untrusted_script_and_onclick(self):
        """Demonstrate that synthetic untrusted inline scripts and onclick handlers are disallowed by CSP policy."""
        generate_html_report([], self.output_path)
        content = self.output_path.read_text(encoding="utf-8")

        csp_match = re.search(r'<meta\s+http-equiv=["\']Content-Security-Policy["\']\s+content="([^"]+)"', content)
        self.assertIsNotNone(csp_match, "Content-Security-Policy meta tag must be present")
        csp_policy = csp_match.group(1)

        directives = {}
        for part in csp_policy.split(";"):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            directives[tokens[0]] = tokens[1:]

        # Untrusted injected script text
        injected_script = "window.INJECTED=true;"
        injected_bytes = injected_script.encode("utf-8")
        injected_hash = f"'sha256-{base64.b64encode(hashlib.sha256(injected_bytes).digest()).decode('ascii')}'"

        # With script-src not having 'unsafe-inline', only allowed hash runs; injected hash is not allowed
        self.assertNotIn("'unsafe-inline'", directives.get("script-src", []))
        self.assertNotIn(injected_hash, directives.get("script-src", []))

        # Inline event handler blocked by script-src-attr 'none'
        self.assertEqual(directives.get("script-src-attr", []), ["'none'"])

if __name__ == "__main__":
    unittest.main()
