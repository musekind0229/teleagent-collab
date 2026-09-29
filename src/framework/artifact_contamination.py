"""Scan artifact bytes for AIGC watermarks and invisible-character steganography.

The scanner is pure: it does not read charters, goals, or ``allow_aigc_marks``.
Callers decide whether a positive result blocks acceptance. Opt-out lives on
the Windows charter (``allow_aigc_marks: true``) and, for the service gate, on
``goal.acceptance.allow_aigc_marks`` or ``task.inputs.allow_aigc_marks``.

BOM rule
--------
A single U+FEFF as the first character of the decoded text is a legitimate
encoding marker and is not contamination when it comes from a BOM at byte 0:

- UTF-8 BOM ``EF BB BF``
- UTF-16 LE BOM ``FF FE``
- UTF-16 BE BOM ``FE FF``

Any other U+FEFF — not at character index 0, or a second U+FEFF after that
leading marker — is contamination. Decoding keeps the BOM in the text (UTF-8,
not UTF-8-SIG; UTF-16-LE / UTF-16-BE, not the BOM-stripping UTF-16 codec) so
the index rule can see it.

UTF-16 is used only when a UTF-16 BOM is at byte 0. Otherwise the bytes are
strict UTF-8 (optional UTF-8 BOM). If they do not decode, ``encoding`` is
``"binary"``, ``scanned`` is false, and ``contaminated`` is false: binary
payloads are not scanned.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Order is the stable display order used by summarize().
_INVISIBLE_CHARS: tuple[str, ...] = (
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
    "\u180e",  # MONGOLIAN VOWEL SEPARATOR
    "\u2061",  # FUNCTION APPLICATION
    "\u2062",  # INVISIBLE TIMES
    "\u2063",  # INVISIBLE SEPARATOR
    "\u2064",  # INVISIBLE PLUS
)
_INVISIBLE_SET = frozenset(_INVISIBLE_CHARS)
_LABEL = {char: f"U+{ord(char):04X}" for char in _INVISIBLE_CHARS}

# Labels actually observed. Optional whitespace covers "AI 生成".
# "AI generated" is intentionally absent: it false-positives on English prose.
_AIGC_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AI生成", re.compile(r"AI\s*生成")),
    ("人工智能生成", re.compile(r"人工智能生成")),
)

_DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """Return ``(text, encoding, leading_bom)``.

    ``leading_bom`` is true when byte 0 is an encoding BOM, so a U+FEFF at
    character index 0 is not contamination.
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encoding = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be"
        try:
            text = raw.decode(encoding)
        except UnicodeError:
            return "", "binary", False
        return text, encoding, True
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return "", "binary", False
    if raw.startswith(b"\xef\xbb\xbf"):
        return text, "utf-8-bom", True
    return text, "utf-8", False


def _empty_binary() -> dict[str, Any]:
    return {
        "contaminated": False,
        "encoding": "binary",
        "invisible": {},
        "aigc_marks": {},
        "first_offset": None,
        "scanned": False,
    }


def _count_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def format_parts(scan: Mapping[str, Any]) -> list[str]:
    """Stable ``labelxN`` pieces: AIGC marks, then invisible code points."""
    parts: list[str] = []
    marks = scan.get("aigc_marks") if isinstance(scan.get("aigc_marks"), Mapping) else {}
    for label, _pattern in _AIGC_RES:
        count = _count_value(marks.get(label))
        if count:
            parts.append(f"{label}x{count}")
    invisible = scan.get("invisible") if isinstance(scan.get("invisible"), Mapping) else {}
    for char in _INVISIBLE_CHARS:
        label = _LABEL[char]
        count = _count_value(invisible.get(label))
        if count:
            parts.append(f"{label}x{count}")
    return parts


def _is_contaminated(scan: Mapping[str, Any]) -> bool:
    flag = scan.get("contaminated")
    if flag is True:
        return True
    if flag is False:
        return False
    return bool(format_parts(scan))


def scan_bytes(raw: bytes) -> dict[str, Any]:
    """Scan one artifact payload.

    Returns ``contaminated``, ``encoding`` (``utf-8``, ``utf-8-bom``,
    ``utf-16-le``, ``utf-16-be``, or ``binary``), ``invisible`` counts keyed
    ``U+200B`` and so on, ``aigc_marks`` counts keyed by the canonical label
    (``AI生成``, ``人工智能生成``), and ``first_offset`` (character index of the
    first contamination, or None). Binary input is not scanned.

    See the module docstring for the leading-BOM exception.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError("scan_bytes expects bytes")
    data = bytes(raw)
    text, encoding, leading_bom = _decode(data)
    if encoding == "binary":
        return _empty_binary()
    invisible_counts = {char: 0 for char in _INVISIBLE_CHARS}
    first: int | None = None
    for index, char in enumerate(text):
        if char not in _INVISIBLE_SET:
            continue
        if char == "\ufeff" and index == 0 and leading_bom:
            continue
        invisible_counts[char] += 1
        if first is None:
            first = index
    marks: dict[str, int] = {}
    for label, pattern in _AIGC_RES:
        count = 0
        for match in pattern.finditer(text):
            count += 1
            start = match.start()
            if first is None or start < first:
                first = start
        if count:
            marks[label] = count
    invisible = {
        _LABEL[char]: invisible_counts[char]
        for char in _INVISIBLE_CHARS
        if invisible_counts[char]
    }
    return {
        "contaminated": bool(invisible or marks),
        "encoding": encoding,
        "invisible": invisible,
        "aigc_marks": marks,
        "first_offset": first,
        "scanned": True,
    }


def _trim_partial_tail(blob: bytes) -> bytes:
    """Drop a code unit cut in half by truncation so text is not misread as binary."""
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        return blob[: len(blob) - (len(blob) % 2)]
    for cut in range(0, 4):
        try:
            blob[: len(blob) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return blob[: len(blob) - cut]
    return blob


def scan_file(path: str | Path, max_bytes: int = _DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Scan a file, or only its first ``max_bytes`` when larger.

    Sets ``truncated: True`` when the file is longer than ``max_bytes``.
    The truncated prefix is what ``scan_bytes`` sees; a prefix that is not
    valid text is reported as binary / not scanned, same as ``scan_bytes``.
    """
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("max_bytes must be >= 0")
    file_path = Path(path)
    with file_path.open("rb") as handle:
        blob = handle.read(limit + 1 if limit else 1)
    truncated = len(blob) > limit
    if truncated:
        blob = _trim_partial_tail(blob[:limit])
    found = scan_bytes(blob)
    found["truncated"] = truncated
    return found


def summarize(findings_by_name: Mapping[str, Any] | dict) -> str:
    """One line of contaminated artifacts, or ``""`` when every entry is clean.

    Example: ``CONTAMINATED hello.txt: AI生成x1, U+200Bx1926, U+200Dx1658``.
    Several artifacts are joined with ``"; "``.
    """
    if not isinstance(findings_by_name, Mapping):
        return ""
    chunks: list[str] = []
    for name, scan in findings_by_name.items():
        if not isinstance(scan, Mapping) or not _is_contaminated(scan):
            continue
        parts = format_parts(scan)
        label = str(name)
        if parts:
            chunks.append(f"CONTAMINATED {label}: {', '.join(parts)}")
        else:
            chunks.append(f"CONTAMINATED {label}")
    return "; ".join(chunks)


__all__ = [
    "format_parts",
    "scan_bytes",
    "scan_file",
    "summarize",
]
