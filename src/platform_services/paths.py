"""Path-safety contract: is root/relative inside root, following links?

This is a pre-flight check, NOT an OS sandbox (races remain possible).
Verdict "unknown" (permission error, race, unrecognised reparse point,
link loop) must be treated as unsafe for protected intents.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass

INSIDE = "inside"
ESCAPE = "escape"
UNKNOWN = "unknown"

INTENT_READ = "read"
PROTECTED_INTENTS = frozenset({"write", "delete", "exec"})
_INTENTS = PROTECTED_INTENTS | {INTENT_READ}
_MAX_LINKS = 40
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PathSafetyError(PermissionError):
    pass


@dataclass(frozen=True)
class PathInspection:
    root: str
    normalized: str
    link_chain: tuple[str, ...]
    verdict: str
    intent: str
    reason: str = ""


def _inside(root: str, p: str) -> bool:
    try:
        return os.path.commonpath([root, p]) == root
    except ValueError:  # different drives on Windows
        return False


def inspect_path(root: str, relative: str, intent: str) -> PathInspection:
    if intent not in _INTENTS:
        raise ValueError(f"unknown intent {intent!r}")
    root_n = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    chain: list[str] = []

    def out(path: str, verdict: str, reason: str = "") -> PathInspection:
        return PathInspection(root_n, path, tuple(chain), verdict, intent, reason)

    if os.path.isabs(relative) or os.path.splitdrive(relative)[0]:
        return out(os.path.normpath(relative), ESCAPE, "absolute path")
    pending = [c for c in relative.replace("\\", "/").split("/") if c not in ("", ".")]
    cur = root_n
    hops = 0
    while pending:
        comp = pending.pop(0)
        nxt = os.path.dirname(cur) if comp == ".." else os.path.join(cur, comp)
        if comp == "..":
            cur = nxt
            if not _inside(root_n, cur):
                return out(cur, ESCAPE, "'..' leaves root")
            continue
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            cur = nxt  # not created yet: lexical from here
            continue
        except OSError as e:
            return out(nxt, UNKNOWN, f"lstat failed: {e}")
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > _MAX_LINKS:
                return out(nxt, UNKNOWN, "link loop")
            try:
                target = os.readlink(nxt)
            except OSError as e:
                return out(nxt, UNKNOWN, f"readlink failed: {e}")
            chain.append(f"{nxt} -> {target}")
            if os.path.isabs(target) or os.path.splitdrive(target)[0]:
                tgt = os.path.normcase(os.path.realpath(target))
                if not _inside(root_n, tgt):
                    return out(tgt, ESCAPE, "link target outside root")
                cur = tgt
                continue
            tparts = [c for c in target.replace("\\", "/").split("/") if c not in ("", ".")]
            pending = tparts + pending
            continue
        if getattr(st, "st_file_attributes", 0) & _REPARSE:
            return out(nxt, UNKNOWN, "non-symlink reparse point (e.g. junction)")
        cur = nxt
    cur = os.path.normcase(cur)
    if not _inside(root_n, cur):
        return out(cur, ESCAPE, "outside root")
    return out(cur, INSIDE)


def require_safe(insp: PathInspection) -> str:
    """Return normalized path or raise. Escape always fails; unknown fails for protected intents."""
    if insp.verdict == ESCAPE or (
        insp.verdict == UNKNOWN and insp.intent in PROTECTED_INTENTS
    ):
        raise PathSafetyError(f"{insp.intent} refused ({insp.verdict}): {insp.reason}")
    return insp.normalized


__all__ = [
    "ESCAPE", "INSIDE", "UNKNOWN", "PROTECTED_INTENTS", "PathInspection",
    "PathSafetyError", "inspect_path", "require_safe",
]
