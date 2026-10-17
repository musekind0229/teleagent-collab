"""
Unit tests for chat_local_report.py
Tests run against synthetic fixtures, inputs/SYNTHETIC-CHAT.json, and temporary synthetic files.
All tests standard library only (unittest).
"""

from pathlib import Path
import json
import shutil
import tempfile
import unittest

from src.chat_local_report import run_report, is_credential_text, is_file_credential_mixed


class TestChatLocalReport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="chat_report_test_")
        self.temp_path = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_synthetic_chat_json_direct_manifest(self):
        """Test parsing of inputs/SYNTHETIC-CHAT.json via explicit manifest."""
        input_file = Path("inputs/SYNTHETIC-CHAT.json").resolve()
        self.assertTrue(input_file.is_file(), "inputs/SYNTHETIC-CHAT.json must exist")

        manifest_file = self.temp_path / "manifest.json"
        out_dir = self.temp_path / "out"

        manifest_data = {
            "files": [str(input_file)],
            "allowlisted_roots": [str(input_file.parent.parent.resolve())],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        control = run_report(manifest_file, out_dir)

        # Assertions on CONTROL format (only fixed counts, booleans, hashes, categories)
        self.assertTrue(control["is_success"])
        self.assertEqual(control["files_manifest_count"], 1)
        self.assertEqual(control["files_credential_excluded_count"], 0)
        self.assertEqual(control["records_extracted_count"], 12)
        self.assertEqual(control["lines_malformed_skipped_count"], 0)
        self.assertTrue(control["has_contradictions_flagged"])
        self.assertTrue(control["has_truncation_uncertainty"])
        self.assertGreaterEqual(control["quotes_sensitive_suppressed_count"], 1)

        # Check CONTROL keys: absolutely no source text, paths, or names
        self.assertIn("artifact_hashes", control)
        self.assertIn("categories", control)
        control_str = json.dumps(control)
        self.assertNotIn("password=xxxxxxxxxxxx", control_str)
        self.assertNotIn("SYNTHETIC-CHAT.json", control_str)
        self.assertNotIn("我喜欢绿色", control_str)

        # Check local files exist
        local_json = out_dir / "report_local_records.json"
        local_csv = out_dir / "report_local_records.csv"
        local_html = out_dir / "report_local_viewer.html"
        control_json = out_dir / "control_summary.json"

        self.assertTrue(local_json.is_file())
        self.assertTrue(local_csv.is_file())
        self.assertTrue(local_html.is_file())
        self.assertTrue(control_json.is_file())

        with open(local_json, "r", encoding="utf-8") as f:
            records = json.load(f)

        self.assertEqual(len(records), 12)

        # Verify record 1 (s01): first-person
        s01 = records[0]
        self.assertEqual(s01["subject"], "first-person")
        self.assertFalse(s01["negation"])
        self.assertEqual(s01["confidence"], "user_dialogue_statement")

        # Verify record 2 (s02): mixed-subject & negation
        s02 = records[1]
        self.assertEqual(s02["subject"], "mixed-subject")
        self.assertTrue(s02["negation"])

        # Verify record 3 (s03): mixed-subject (oracle independence: expected=quoted_other has no effect)
        s03 = records[2]
        self.assertEqual(s03["subject"], "mixed-subject")

        # Verify record 4 (s04): historical vs current negation & contradiction
        s04 = records[3]
        self.assertEqual(s04["time_qualification"], "historical")
        self.assertTrue(s04["negation"])
        self.assertTrue(s04["contradiction_review"])

        # Verify record 5 (s05): assistant not user fact
        s05 = records[4]
        self.assertEqual(s05["confidence"], "origin_assistant")
        self.assertIn("Assistant dialogue", s05["summary_local_rule_inference"])

        # Verify record 7 (s07): secret suppressed
        s07 = records[6]
        self.assertTrue(s07["sensitive"])
        self.assertEqual(s07["quote_verbatim"], "[REDACTED_SENSITIVE]")
        self.assertNotIn("xxxxxxxxxxxx", s07["quote_verbatim"])

        # Verify record 10 (s10): truncated
        s10 = records[9]
        self.assertTrue(s10["truncated"])
        self.assertEqual(s10["confidence"], "low_truncated")

        # Verify record 11 (s11): tool not user
        s11 = records[10]
        self.assertEqual(s11["confidence"], "origin_tool")

        # Verify record 12 (s12): generated context compaction not user
        s12 = records[11]
        self.assertEqual(s12["confidence"], "origin_non_user")

    def test_claude_codex_jsonl_fixtures(self):
        """Test Claude and Codex ordinary JSONL schemas and malformed line skips."""
        claude_file = self.temp_path / "claude.jsonl"
        codex_file = self.temp_path / "codex.jsonl"

        claude_content = [
            json.dumps({"message": {"role": "user", "content": "I like blue."}}),
            json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]}}),
            "INVALID_JSON_LINE_MALFORMED",
            json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": "He said it's raining outside."}]}}),
        ]
        with open(claude_file, "w", encoding="utf-8") as f:
            f.write("\n".join(claude_content))

        codex_content = [
            json.dumps({"event_msg": "user_message", "role": "user", "text": "Can you help me with python?", "time": "2026-01-01T10:00:00Z"}),
            json.dumps({"response_item": {"status": "ok"}, "role": "assistant", "text": "Sure!"}),
            "{corrupted-json",
            json.dumps({"event_msg": "user_message", "role": "user", "text": "I do not use this tool anymore..."}),
        ]
        with open(codex_file, "w", encoding="utf-8") as f:
            f.write("\n".join(codex_content))

        manifest_file = self.temp_path / "manifest.json"
        out_dir = self.temp_path / "out_fixtures"

        manifest_data = {
            "files": [str(claude_file), str(codex_file)],
            "allowlisted_roots": [str(self.temp_path)],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        control = run_report(manifest_file, out_dir)

        self.assertEqual(control["files_manifest_count"], 2)
        self.assertEqual(control["lines_malformed_skipped_count"], 2)
        self.assertEqual(control["records_extracted_count"], 6)

        local_json = out_dir / "report_local_records.json"
        with open(local_json, "r", encoding="utf-8") as f:
            records = json.load(f)

        # 3 from claude (user, assistant, user third-party), 3 from codex (user query, assistant, user negated/truncated)
        self.assertEqual(len(records), 6)
        self.assertEqual(records[0]["subject"], "first-person")
        self.assertEqual(records[1]["confidence"], "origin_assistant")
        self.assertEqual(records[2]["subject"], "third-party")
        self.assertEqual(records[3]["confidence"], "user_request_or_query")
        self.assertEqual(records[4]["confidence"], "origin_assistant")
        self.assertTrue(records[5]["negation"])
        self.assertTrue(records[5]["truncated"])

    def test_manifest_authorization_and_allowlist_enforcement(self):
        """Test that paths outside allowlisted roots or non-ordinary files are strictly rejected."""
        manifest_file = self.temp_path / "manifest_escape.json"
        out_dir = self.temp_path / "out_auth"

        dummy_file = self.temp_path / "dummy.jsonl"
        dummy_file.write_text('{"role": "user", "text": "hello"}\n', encoding="utf-8")

        # Allowlisted root is /some/other/path
        subfolder = self.temp_path / "subfolder"
        subfolder.mkdir()
        manifest_data = {
            "files": [str(dummy_file)],
            "allowlisted_roots": [str(subfolder)],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        with self.assertRaises(PermissionError):
            run_report(manifest_file, out_dir)

    def test_credential_mixed_file_excluded(self):
        """Test that a file with credential mixtures is excluded entirely."""
        cred_file = self.temp_path / "cred_chat.jsonl"
        lines = [
            json.dumps({"role": "user", "text": "Here is my secret"}),
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIEowIBAAKCAQEA0...",
            "-----END RSA PRIVATE KEY-----",
            json.dumps({"role": "user", "text": "password=supersecretpassword123"}),
        ]
        cred_file.write_text("\n".join(lines), encoding="utf-8")

        manifest_file = self.temp_path / "manifest_cred.json"
        out_dir = self.temp_path / "out_cred"

        manifest_data = {
            "files": [str(cred_file)],
            "allowlisted_roots": [str(self.temp_path)],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        control = run_report(manifest_file, out_dir)
        self.assertEqual(control["files_credential_excluded_count"], 1)
        self.assertEqual(control["records_extracted_count"], 0)

    def test_defect1_actual_codex_records_and_claude_positive(self):
        """Test defect 1: actual Codex shapes produce records and tool_result is not user text."""
        codex_file = self.temp_path / "defect1_codex.jsonl"
        lines = [
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "I prefer synthetic blue."}, "timestamp": "2001-01-01T00:00:00Z"}),
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "I use synthetic tool A."}]}}),
            json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": "I prefer synthetic blue."}]}}),
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"type": "tool_result", "text": "tool output ignored"}]}}),
        ]
        codex_file.write_text("\n".join(lines), encoding="utf-8")

        manifest_file = self.temp_path / "manifest_defect1.json"
        out_dir = self.temp_path / "out_defect1"
        manifest_data = {
            "files": [str(codex_file)],
            "allowlisted_roots": [str(self.temp_path)],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        control = run_report(manifest_file, out_dir)
        self.assertEqual(control["records_extracted_count"], 3)
        self.assertEqual(control["lines_malformed_skipped_count"], 1)

    def test_defect2_manifest_file_suffix_and_root_validation(self):
        """Test defect 2: require nonempty roots, reject unsupported suffixes before read, and fake .db."""
        db_file = self.temp_path / "test.db"
        db_file.write_text("FAKE_DB_MARKER", encoding="utf-8")
        manifest_file = self.temp_path / "manifest_defect2.json"
        out_dir = self.temp_path / "out_defect2"

        # Reject .db suffix
        manifest_data = {
            "files": [str(db_file)],
            "allowlisted_roots": [str(self.temp_path)],
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)

        with self.assertRaises((ValueError, PermissionError)):
            run_report(manifest_file, out_dir)

        # Reject empty allowlisted_roots
        valid_jsonl = self.temp_path / "valid.jsonl"
        valid_jsonl.write_text('{"role": "user", "text": "hi"}\n', encoding="utf-8")
        manifest_empty_roots = self.temp_path / "manifest_empty_roots.json"
        with open(manifest_empty_roots, "w", encoding="utf-8") as f:
            json.dump({"files": [str(valid_jsonl)], "allowlisted_roots": []}, f)

        with self.assertRaises((ValueError, PermissionError)):
            run_report(manifest_empty_roots, out_dir)

    def test_defect3_symlink_ancestor_and_lexical_dots_rejection(self):
        """Test defect 3: reject symlinks/junctions in ancestors and lexical .. traversal."""
        sub_dir = self.temp_path / "sub"
        sub_dir.mkdir()
        real_file = sub_dir / "chat.jsonl"
        real_file.write_text('{"role": "user", "text": "hello"}\n', encoding="utf-8")

        # Lexical .. test
        manifest_dots = self.temp_path / "manifest_dots.json"
        with open(manifest_dots, "w", encoding="utf-8") as f:
            json.dump({
                "files": [f"sub/../sub/chat.jsonl"],
                "allowlisted_roots": [str(self.temp_path)],
            }, f)
        out_dir = self.temp_path / "out_defect3"
        with self.assertRaises(ValueError):
            run_report(manifest_dots, out_dir)

        # Symlink ancestor test (owned synthetic symlink in test workspace)
        link_dir = self.temp_path / "link_dir"
        try:
            import os
            os.symlink(sub_dir, link_dir, target_is_directory=True)
            link_file = link_dir / "chat.jsonl"
            manifest_link = self.temp_path / "manifest_link.json"
            with open(manifest_link, "w", encoding="utf-8") as f:
                json.dump({
                    "files": [str(link_file)],
                    "allowlisted_roots": [str(self.temp_path)],
                }, f)
            with self.assertRaises((ValueError, PermissionError)):
                run_report(manifest_link, out_dir)
        finally:
            if link_dir.exists():
                os.rmdir(link_dir)

    def test_defect4_oracle_independence(self):
        """Test defect 4: expected/oracle labels have NO effect on production fields."""
        plain_first = "I prefer synthetic blue."
        rec_no_expected = run_report.__globals__["analyze_dialogue_record"](
            raw_text=plain_first,
            role="user",
            event_time_raw="",
            source_locator="test",
            source_line=1,
            override_expected=None,
        )
        rec_quoted_other = run_report.__globals__["analyze_dialogue_record"](
            raw_text=plain_first,
            role="user",
            event_time_raw="",
            source_locator="test",
            source_line=1,
            override_expected="quoted_other",
        )
        rec_contra = run_report.__globals__["analyze_dialogue_record"](
            raw_text=plain_first,
            role="user",
            event_time_raw="",
            source_locator="test",
            source_line=1,
            override_expected="contradiction_retained",
        )

        self.assertEqual(rec_no_expected["subject"], "first-person")
        self.assertEqual(rec_quoted_other["subject"], "first-person")
        self.assertFalse(rec_no_expected["contradiction_review"])
        self.assertFalse(rec_contra["contradiction_review"])
        self.assertEqual(rec_no_expected, rec_no_expected)
        self.assertEqual(rec_no_expected, rec_quoted_other)
        self.assertEqual(rec_no_expected, rec_contra)

    def test_manifest_entry_junction_must_reject_before_open(self):
        """Test that manifest path entrance junction is rejected before opening content in parse_manifest and run_report."""
        real_dir = self.temp_path / "real_dir"
        real_dir.mkdir()
        chat_file = real_dir / "source.jsonl"
        chat_file.write_text('{"message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}}\n', encoding="utf-8")
        manifest_file = real_dir / "manifest.json"
        manifest_file.write_text(json.dumps({"files": ["source.jsonl"], "allowlisted_roots": ["."]}), encoding="utf-8")

        alias_dir = self.temp_path / "alias_dir"
        import _winapi
        _winapi.CreateJunction(str(real_dir), str(alias_dir))
        alias_manifest = alias_dir / "manifest.json"

        # Observe parse_manifest calls
        realopen = open
        realioopen = __import__("io").open
        events = []

        def same(p):
            try:
                import os
                return os.path.normcase(os.path.abspath(os.fspath(p))) == os.path.normcase(os.path.abspath(os.fspath(alias_manifest)))
            except TypeError:
                return False

        def op(p, *args, **kwargs):
            if same(p):
                events.append({"kind": "content_open", "api": "builtins.open"})
            return realopen(p, *args, **kwargs)

        def iop(p, *args, **kwargs):
            if same(p):
                events.append({"kind": "content_open", "api": "io.open"})
            return realioopen(p, *args, **kwargs)

        from unittest.mock import patch
        from src.chat_local_report import parse_manifest

        with patch("builtins.open", op), patch("io.open", iop):
            with self.assertRaises(ValueError):
                parse_manifest(alias_manifest)

        # Assert no content open before rejection
        content_opens = [e for e in events if e["kind"] == "content_open"]
        self.assertEqual(len(content_opens), 0, "Manifest content must not be opened before entrance junction rejection")

        # Test run_report alias-path reject directly (not parse_manifest alone)
        out_dir = self.temp_path / "out_alias"
        events_run = []

        def op_run(p, *args, **kwargs):
            if same(p):
                events_run.append({"kind": "content_open", "api": "builtins.open"})
            return realopen(p, *args, **kwargs)

        with patch("builtins.open", op_run), patch("io.open", iop):
            with self.assertRaises(ValueError):
                run_report(alias_manifest, out_dir)

        content_opens_run = [e for e in events_run if e["kind"] == "content_open"]
        self.assertEqual(len(content_opens_run), 0, "run_report must reject alias manifest before opening content")

    def test_ordinary_relative_manifest_and_positive_explicit_blocks(self):
        """Test ordinary relative manifest with . and explicit text / input_text blocks."""
        rel_dir = self.temp_path / "ordinary_rel"
        rel_dir.mkdir()
        chat_file = rel_dir / "chat.jsonl"
        chat_file.write_text(
            json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": "I like blue."}]}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "I use synthetic tool A."}]}}) + "\n",
            encoding="utf-8"
        )
        manifest_file = rel_dir / "manifest.json"
        manifest_file.write_text(json.dumps({"files": ["chat.jsonl"], "allowlisted_roots": ["."]}), encoding="utf-8")

        import os
        orig_cwd = os.getcwd()
        try:
            os.chdir(str(rel_dir))
            out_dir = Path("out_rel")
            control = run_report(Path("manifest.json"), out_dir)
            self.assertTrue(control["is_success"])
            self.assertEqual(control["files_manifest_count"], 1)
            self.assertEqual(control["records_extracted_count"], 2)

            local_json = out_dir / "report_local_records.json"
            with open(local_json, "r", encoding="utf-8") as f:
                records = json.load(f)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["quote_verbatim"], "I like blue.")
            self.assertEqual(records[1]["quote_verbatim"], "I use synthetic tool A.")
        finally:
            os.chdir(orig_cwd)

    def test_claude_and_codex_typeless_blocks_rejected(self):
        """Test that Claude and Codex typeless {text:...} blocks and tool_result never become dialogue."""
        chat_file = self.temp_path / "typeless_test.jsonl"
        lines = [
            # Claude typeless block - should be rejected as dialogue
            json.dumps({"message": {"role": "user", "content": [{"text": "SYNTHETIC_TYPELESS_SHOULD_NOT_PARSE"}]}}),
            # Claude explicit block - should be accepted
            json.dumps({"message": {"role": "user", "content": [{"type": "text", "text": "Claude explicit positive."}]}}),
            # Codex typeless block - should be rejected
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"text": "SYNTHETIC_TYPELESS_SHOULD_NOT_PARSE"}]}}),
            # Codex explicit input_text block - should be accepted
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "Codex explicit positive."}]}}),
            # Codex tool_result block - should not become human text
            json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"type": "tool_result", "text": "tool output ignored"}]}}),
        ]
        chat_file.write_text("\n".join(lines), encoding="utf-8")

        manifest_file = self.temp_path / "manifest_typeless.json"
        out_dir = self.temp_path / "out_typeless"
        manifest_file.write_text(json.dumps({"files": [str(chat_file)], "allowlisted_roots": [str(self.temp_path)]}), encoding="utf-8")

        control = run_report(manifest_file, out_dir)
        self.assertEqual(control["records_extracted_count"], 2)
        self.assertEqual(control["lines_malformed_skipped_count"], 3)

        local_json = out_dir / "report_local_records.json"
        with open(local_json, "r", encoding="utf-8") as f:
            records = json.load(f)
        self.assertEqual(len(records), 2)
        extracted_quotes = [r["quote_verbatim"] for r in records]
        self.assertNotIn("SYNTHETIC_TYPELESS_SHOULD_NOT_PARSE", extracted_quotes)
        self.assertNotIn("tool output ignored", extracted_quotes)
        self.assertIn("Claude explicit positive.", extracted_quotes)
        self.assertIn("Codex explicit positive.", extracted_quotes)


if __name__ == "__main__":
    unittest.main()


