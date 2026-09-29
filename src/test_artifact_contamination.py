"""Content scan for AIGC marks and invisible-character watermarks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from framework.artifact_contamination import scan_bytes, scan_file, summarize


class ArtifactContaminationTests(unittest.TestCase):
    def test_clean_ascii_and_chinese(self) -> None:
        ascii_scan = scan_bytes(b"hello decision\n")
        self.assertFalse(ascii_scan["contaminated"])
        self.assertEqual(ascii_scan["encoding"], "utf-8")
        self.assertEqual(ascii_scan["invisible"], {})
        self.assertEqual(ascii_scan["aigc_marks"], {})
        self.assertIsNone(ascii_scan["first_offset"])
        self.assertTrue(ascii_scan["scanned"])
        chinese = scan_bytes("春眠不觉晓，处处闻啼鸟。\n".encode("utf-8"))
        self.assertFalse(chinese["contaminated"])
        self.assertEqual(chinese["encoding"], "utf-8")
        self.assertTrue(chinese["scanned"])

    def test_observed_watermark_pattern(self) -> None:
        n = 4
        text = "hello decision\n\nAI生成\n" + ("\u200b\u200d" * n)
        found = scan_bytes(text.encode("utf-8"))
        self.assertTrue(found["contaminated"])
        self.assertEqual(found["encoding"], "utf-8")
        self.assertEqual(found["aigc_marks"], {"AI生成": 1})
        self.assertEqual(found["invisible"], {"U+200B": n, "U+200D": n})
        self.assertEqual(found["first_offset"], text.index("AI生成"))
        self.assertEqual(
            summarize({"hello.txt": found}),
            f"CONTAMINATED hello.txt: AI生成x1, U+200Bx{n}, U+200Dx{n}",
        )

    def test_leading_utf8_bom_is_clean_until_a_later_fef(self) -> None:
        leading = scan_bytes(b"\xef\xbb\xbf" + "hello".encode("utf-8"))
        self.assertEqual(leading["encoding"], "utf-8-bom")
        self.assertFalse(leading["contaminated"])
        self.assertEqual(leading["invisible"], {})
        self.assertIsNone(leading["first_offset"])
        only_bom = scan_bytes(b"\xef\xbb\xbf")
        self.assertFalse(only_bom["contaminated"])
        self.assertEqual(only_bom["encoding"], "utf-8-bom")
        later = scan_bytes(b"\xef\xbb\xbf" + "a".encode("utf-8") + "\ufeff".encode("utf-8"))
        self.assertEqual(later["encoding"], "utf-8-bom")
        self.assertTrue(later["contaminated"])
        self.assertEqual(later["invisible"], {"U+FEFF": 1})
        self.assertEqual(later["first_offset"], 2)

    def test_utf16_le_bom_is_clean(self) -> None:
        raw = b"\xff\xfe" + "hello".encode("utf-16-le")
        self.assertTrue(raw.startswith(b"\xff\xfe"))
        found = scan_bytes(raw)
        self.assertEqual(found["encoding"], "utf-16-le")
        self.assertFalse(found["contaminated"])
        self.assertIsNone(found["first_offset"])
        self.assertTrue(found["scanned"])

    def test_binary_is_not_scanned(self) -> None:
        found = scan_bytes(b"\xff\x00\x81\xfe")
        self.assertEqual(found["encoding"], "binary")
        self.assertFalse(found["contaminated"])
        self.assertFalse(found["scanned"])
        self.assertEqual(found["invisible"], {})
        self.assertEqual(found["aigc_marks"], {})
        self.assertIsNone(found["first_offset"])

    def test_ai_space_generated_variant_counts_and_english_does_not(self) -> None:
        variant = scan_bytes("prefix AI 生成 suffix".encode("utf-8"))
        self.assertTrue(variant["contaminated"])
        self.assertEqual(variant["aigc_marks"], {"AI生成": 1})
        self.assertEqual(variant["first_offset"], "prefix AI 生成 suffix".index("AI"))
        prose = scan_bytes(b"This file was AI generated for the demo.\n")
        self.assertFalse(prose["contaminated"])
        self.assertEqual(prose["aigc_marks"], {})
        long_form = scan_bytes("说明：人工智能生成\n".encode("utf-8"))
        self.assertEqual(long_form["aigc_marks"], {"人工智能生成": 1})
        self.assertTrue(long_form["contaminated"])

    def test_summarize_observed_counts_and_clean_is_empty(self) -> None:
        line = summarize(
            {
                "hello.txt": {
                    "contaminated": True,
                    "aigc_marks": {"AI生成": 1},
                    "invisible": {"U+200D": 1658, "U+200B": 1926},
                },
                "notes.txt": {"contaminated": False, "aigc_marks": {}, "invisible": {}},
            }
        )
        self.assertEqual(line, "CONTAMINATED hello.txt: AI生成x1, U+200Bx1926, U+200Dx1658")
        self.assertEqual(summarize({}), "")
        self.assertEqual(summarize({"a.txt": {"contaminated": False}}), "")

    def test_scan_file_truncates_to_max_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.txt"
            path.write_bytes("AI生成".encode("utf-8") + b"x" * 40)
            found = scan_file(path, max_bytes=len("AI生成".encode("utf-8")))
            self.assertTrue(found["truncated"])
            self.assertTrue(found["contaminated"])
            self.assertEqual(found["aigc_marks"]["AI生成"], 1)
            small = scan_file(path, max_bytes=2 * 1024 * 1024)
            self.assertFalse(small["truncated"])


    def test_scan_file_truncation_mid_multibyte_char_still_scans(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cut.txt"
            body = ("好" * 4 + "\u200b").encode("utf-8") + "好".encode("utf-8") * 10
            path.write_bytes(body)
            limit = len(("好" * 4 + "\u200b").encode("utf-8")) + 1  # cut inside next char
            found = scan_file(path, max_bytes=limit)
            self.assertTrue(found["truncated"])
            self.assertTrue(found["scanned"])
            self.assertEqual(found["invisible"].get("U+200B"), 1)


if __name__ == "__main__":
    unittest.main()
