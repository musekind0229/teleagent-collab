"""Plan-time allow-path / glob coverage for Goal-contract ceilings.

Wildcard set inclusion is path-segment aware. String prefix / generic
``fnmatch`` is not used as a substitute (``/safe/../outside/**`` is not
inside ``/safe/**``).

Paths are interpreted for the **execution target platform** (posix vs
windows): separators, case, drive/UNC, lexical ``.`` / ``..``. Plan-time
checks do **not** follow symlinks or Windows junctions — those require the
target filesystem and are re-checked at execution.

Public kernel path only. No TeleAgent HTTP. No Hermes ledger.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Iterable

PLATFORM_POSIX = "posix"
PLATFORM_WINDOWS = "windows"

REASON_READY = "ready"
REASON_PATH_ESCAPE = "path_escape"
REASON_NOT_COVERED = "not_covered"


def normalize_platform(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"win", "windows", "win32", "nt", "windows_nt"}:
        return PLATFORM_WINDOWS
    return PLATFORM_POSIX


def _is_wild_segment(seg: str) -> bool:
    return bool(seg) and ("*" in seg or "?" in seg or "[" in seg)


def _fold(seg: str, *, ignore_case: bool) -> str:
    return seg.lower() if ignore_case else seg


def _split_windows(raw: str) -> tuple[str, str, bool]:
    """Return (drive_or_unc, rest, absolute). rest uses ``/``."""
    s = raw.replace("\\", "/")
    if s.startswith("//"):
        # UNC: //server/share/rest
        body = s[2:]
        parts = [p for p in body.split("/") if p]
        if len(parts) >= 2:
            unc = "//" + parts[0] + "/" + parts[1]
            rest = "/" + "/".join(parts[2:]) if len(parts) > 2 else "/"
            return unc, rest, True
        return "", s, True
    if len(s) >= 2 and s[1] == ":":
        drive = s[0] + ":"
        rest = s[2:] or "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        return drive, rest, True
    absolute = s.startswith("/")
    return "", s, absolute


def _raw_segments(rest: str) -> list[str]:
    # Keep ``..`` / ``**``; drop empty and ``.``.
    out: list[str] = []
    for seg in rest.split("/"):
        if not seg or seg == ".":
            continue
        out.append(seg)
    return out


@dataclass(frozen=True)
class PathPattern:
    original: str
    platform: str
    absolute: bool
    drive: str
    parts: tuple[str, ...]
    ambiguous_dotdot: bool
    escaped_above_root: bool

    @property
    def has_wildcard(self) -> bool:
        return any(_is_wild_segment(p) for p in self.parts)

    @property
    def had_dotdot(self) -> bool:
        norm = self.original.replace("\\", "/")
        return any(seg == ".." for seg in norm.split("/")) or "\\.." in self.original.replace("/", "\\")


def parse_path_pattern(raw: str, *, platform: str = PLATFORM_POSIX) -> PathPattern:
    plat = normalize_platform(platform)
    s = str(raw or "").strip()
    if not s or "\x00" in s:
        return PathPattern(
            original=s,
            platform=plat,
            absolute=False,
            drive="",
            parts=(),
            ambiguous_dotdot=bool(s) and "\x00" in s,
            escaped_above_root=bool(s) and "\x00" in s,
        )
    if plat == PLATFORM_WINDOWS:
        drive, rest, absolute = _split_windows(s)
    else:
        drive, rest, absolute = "", s, s.startswith("/")
        # Posix: backslash is not a separator; leave in the segment.
    segs = _raw_segments(rest)
    collapsed: list[str] = []
    ambiguous = False
    escaped = False
    for seg in segs:
        if seg == "..":
            if collapsed and _is_wild_segment(collapsed[-1]):
                ambiguous = True
                collapsed.append("..")
            elif collapsed:
                collapsed.pop()
            elif absolute:
                escaped = True
            else:
                collapsed.append("..")
        else:
            collapsed.append(seg)
    if absolute and any(p == ".." for p in collapsed):
        escaped = True
    return PathPattern(
        original=s,
        platform=plat,
        absolute=absolute,
        drive=drive,
        parts=tuple(collapsed),
        ambiguous_dotdot=ambiguous,
        escaped_above_root=escaped,
    )


def _parts_eq(a: tuple[str, ...] | list[str], b: tuple[str, ...] | list[str], *, ignore_case: bool) -> bool:
    if len(a) != len(b):
        return False
    return all(_fold(x, ignore_case=ignore_case) == _fold(y, ignore_case=ignore_case) for x, y in zip(a, b))


def _is_prefix(ancestor: tuple[str, ...], descendant: tuple[str, ...], *, ignore_case: bool) -> bool:
    if len(descendant) < len(ancestor):
        return False
    return _parts_eq(ancestor, descendant[: len(ancestor)], ignore_case=ignore_case)


def _segment_match(name: str, pat: str, *, ignore_case: bool) -> bool:
    if pat == "**":
        return True
    n = _fold(name, ignore_case=ignore_case)
    p = _fold(pat, ignore_case=ignore_case)
    if p == n:
        return True
    return fnmatch.fnmatchcase(n, p)


def _parts_glob_match(item: tuple[str, ...], pat: tuple[str, ...], *, ignore_case: bool) -> bool:
    """Path-segment glob match. ``**`` matches zero or more segments."""
    n, m = len(item), len(pat)
    dp = [[False] * (m + 1) for _ in range(n + 1)]
    dp[n][m] = True
    for j in range(m - 1, -1, -1):
        if pat[j] == "**":
            dp[n][j] = dp[n][j + 1]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if pat[j] == "**":
                dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
            elif _segment_match(item[i], pat[j], ignore_case=ignore_case):
                dp[i][j] = dp[i + 1][j + 1]
    return dp[0][0]


def _universal(parts: tuple[str, ...]) -> bool:
    return parts in {("**",), ("*",), ("/**".strip("/"),)} or list(parts) == ["**"]


def _trailing_globstar_prefix(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    if not parts or parts[-1] != "**":
        return None
    head = parts[:-1]
    if any(_is_wild_segment(p) for p in head):
        return None
    return head


def pattern_covered(item: str, ceiling: str, *, platform: str = PLATFORM_POSIX) -> tuple[bool, str]:
    """Whether every path matched by ``item`` is matched by ``ceiling``.

    Unprovable inclusion is refused (not covered). Ambiguous ``..`` next to
    a wildcard, or ``..`` that leaves the ceiling, is ``path_escape``.
    """
    plat = normalize_platform(platform)
    ign = plat == PLATFORM_WINDOWS
    it = parse_path_pattern(item, platform=plat)
    ce = parse_path_pattern(ceiling, platform=plat)
    if it.ambiguous_dotdot or it.escaped_above_root:
        return False, REASON_PATH_ESCAPE
    if not it.parts and not it.absolute:
        return True, REASON_READY
    if _universal(ce.parts) and not (ce.absolute and it.escaped_above_root):
        return True, REASON_READY

    if plat == PLATFORM_WINDOWS and it.drive and ce.drive and _fold(it.drive, ignore_case=True) != _fold(
        ce.drive, ignore_case=True
    ):
        return False, REASON_NOT_COVERED

    if it.absolute != ce.absolute and not _universal(ce.parts):
        # Relative vs absolute are different sets unless ceiling is universal.
        if ce.absolute and not it.absolute:
            return False, REASON_NOT_COVERED
        if it.absolute and not ce.absolute:
            return False, REASON_NOT_COVERED

    if _parts_eq(it.parts, ce.parts, ignore_case=ign) and it.absolute == ce.absolute:
        return True, REASON_READY

    if not it.has_wildcard:
        if not ce.has_wildcard:
            if _is_prefix(ce.parts, it.parts, ignore_case=ign):
                return True, REASON_READY
            return False, REASON_NOT_COVERED
        if _parts_glob_match(it.parts, ce.parts, ignore_case=ign):
            return True, REASON_READY
        return False, REASON_NOT_COVERED

    # Item is itself a glob. Only prove inclusion for concrete-prefix/** under a tree.
    item_tree = _trailing_globstar_prefix(it.parts)
    if item_tree is not None:
        if not ce.has_wildcard:
            if _is_prefix(ce.parts, item_tree, ignore_case=ign) or _parts_eq(item_tree, ce.parts, ignore_case=ign):
                return True, REASON_READY
            return False, REASON_NOT_COVERED
        ceil_tree = _trailing_globstar_prefix(ce.parts)
        if ceil_tree is not None:
            if _is_prefix(ceil_tree, item_tree, ignore_case=ign) or _parts_eq(item_tree, ceil_tree, ignore_case=ign):
                return True, REASON_READY
            return False, REASON_NOT_COVERED
        if _parts_glob_match(item_tree, ce.parts, ignore_case=ign):
            return True, REASON_READY
        return False, REASON_NOT_COVERED

    return False, REASON_NOT_COVERED


def allow_item_covered(
    item: str,
    ceiling: Iterable[str],
    *,
    platform: str = PLATFORM_POSIX,
    path_like: bool = True,
) -> tuple[bool, str]:
    """Coverage of one allow-list entry against a ceiling list."""
    item_n = str(item or "").strip()
    if not item_n:
        return True, REASON_READY
    ceil_items = [str(x).strip() for x in ceiling if str(x).strip()]
    if not ceil_items:
        return False, REASON_NOT_COVERED
    if not path_like:
        ign = normalize_platform(platform) == PLATFORM_WINDOWS
        for raw in ceil_items:
            if raw in {"*", "**", "/**"}:
                return True, REASON_READY
            if _fold(item_n, ignore_case=ign) == _fold(raw, ignore_case=ign):
                return True, REASON_READY
        return False, REASON_NOT_COVERED

    reasons: list[str] = []
    for raw in ceil_items:
        ok, why = pattern_covered(item_n, raw, platform=platform)
        if ok:
            return True, REASON_READY
        reasons.append(why)
    if REASON_PATH_ESCAPE in reasons or parse_path_pattern(item_n, platform=platform).had_dotdot:
        # ``..`` that does not stay inside any ceiling is an escape, not a
        # harmless restatement of a covered subtree.
        it = parse_path_pattern(item_n, platform=platform)
        if it.had_dotdot or it.ambiguous_dotdot or it.escaped_above_root:
            return False, REASON_PATH_ESCAPE
    return False, REASON_NOT_COVERED


def execution_path_allowed(
    path: str,
    allow_patterns: Iterable[str],
    *,
    platform: str | None = None,
) -> bool:
    """Re-validate a concrete path at execution time.

    When the requested platform is this host, follow realpath (posix
    symlinks). Windows junctions / reparse points are **not** resolved on
    a Linux host — callers must not treat this as NTFS proof.
    """
    plat = normalize_platform(platform) if platform else (
        PLATFORM_WINDOWS if os.name == "nt" else PLATFORM_POSIX
    )
    patterns = [str(x).strip() for x in allow_patterns if str(x).strip()]
    if not path or not patterns:
        return False
    host_is_windows = os.name == "nt"
    if (plat == PLATFORM_WINDOWS) != host_is_windows:
        ok, _why = allow_item_covered(path, patterns, platform=plat, path_like=True)
        return ok
    try:
        from pathutil import canonicalize, is_path_within
    except Exception:
        canonicalize = None  # type: ignore[assignment]
        is_path_within = None  # type: ignore[assignment]
    concrete = str(path)
    if canonicalize is not None:
        try:
            concrete = canonicalize(path) or concrete
        except Exception:
            concrete = str(path)
    ok, _why = allow_item_covered(concrete, patterns, platform=plat, path_like=True)
    if ok:
        return True
    if is_path_within is not None:
        for raw in patterns:
            parsed = parse_path_pattern(raw, platform=plat)
            if parsed.has_wildcard:
                continue
            parent = raw
            if canonicalize is not None:
                try:
                    parent = canonicalize(raw) or raw
                except Exception:
                    parent = raw
            try:
                if is_path_within(concrete, parent):
                    return True
            except Exception:
                continue
    return False


__all__ = [
    "PLATFORM_POSIX",
    "PLATFORM_WINDOWS",
    "REASON_NOT_COVERED",
    "REASON_PATH_ESCAPE",
    "REASON_READY",
    "PathPattern",
    "allow_item_covered",
    "execution_path_allowed",
    "normalize_platform",
    "parse_path_pattern",
    "pattern_covered",
]
