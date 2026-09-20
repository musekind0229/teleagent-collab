"""Copy declared, accepted files from succeeded direct deps into a successor workspace.

Only ordinary files. No shared directories, credentials, traversal, absolute
names, escaping links, or silent overwrite.
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

MAX_HANDOFF_FILES = 32
MAX_HANDOFF_BYTES = 512 * 1024
_CREDENTIAL_RE = re.compile(
    r'\.env(?:[.\s"/]|$)|\.ssh|\.netrc|auth\.json|credentials|cookies|id_ed25519|id_rsa|login data',
    re.IGNORECASE,
)


class HandoffError(ValueError):
    """Illegal or incomplete dependency artifact handoff."""


def credential_like(value: Any) -> bool:
    text = str(value or "").lower().replace("\\", "/")
    return bool(_CREDENTIAL_RE.search(text))


def safe_relative_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HandoffError("artifact name must be a non-empty relative path")
    name = raw.strip().replace("\\", "/")
    if "\x00" in name or ":" in name or name.startswith("/") or name.startswith("//"):
        raise HandoffError(f"illegal artifact path: {raw!r}")
    path = Path(name)
    if path.is_absolute() or PureWindowsPath(name).is_absolute() or ".." in path.parts or not path.name:
        raise HandoffError(f"illegal artifact path: {raw!r}")
    if credential_like(name):
        raise HandoffError(f"refusing credential-like artifact name: {raw!r}")
    return path.as_posix()


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _norm_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _within(child: Path, root: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _walk_no_links(start: Path, root: Path) -> None:
    current = Path(start)
    root = Path(root)
    for _ in range(64):
        if _is_link(current):
            raise HandoffError("links and junctions are not accepted as artifacts")
        if _norm_key(current) == _norm_key(root):
            return
        parent = current.parent
        if _norm_key(parent) == _norm_key(current):
            raise HandoffError("artifact escapes workspace")
        current = parent
    raise HandoffError("artifact escapes workspace")


def _workspace_root(raw: Any) -> Path:
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        raise HandoffError("dependency artifact workspace is missing")
    root = Path(str(raw))
    if _is_link(root) or not root.is_dir():
        raise HandoffError("handoff workspace must be a real directory")
    return root


def contained_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    rel = safe_relative_name(relative)
    base = Path(root)
    if _is_link(base):
        raise HandoffError("links and junctions are not accepted as artifacts")
    current = base
    for part in Path(rel).parts:
        current = current / part
        if _is_link(current):
            raise HandoffError("links and junctions are not accepted as artifacts")
    if must_exist:
        if not current.is_file() or _is_link(current):
            raise HandoffError(f"missing ordinary file: {rel}")
        try:
            size = current.stat().st_size
        except OSError as e:
            raise HandoffError(f"cannot stat {rel}") from e
        if size > MAX_HANDOFF_BYTES:
            raise HandoffError(f"artifact too large: {rel}")
        if credential_like(current) or credential_like(current.name):
            raise HandoffError(f"refusing credential-like file: {rel}")
        _walk_no_links(current, base)
        resolved = current.resolve()
        if not resolved.is_file() or _is_link(resolved) or not _within(resolved, base):
            raise HandoffError(f"artifact escapes workspace: {rel}")
        return resolved
    if current.exists():
        _walk_no_links(current, base)
        if not _within(current, base):
            raise HandoffError(f"artifact escapes workspace: {rel}")
    return current


def _ordinary_source(path: Path, workspace: Path | None = None) -> Path:
    src = Path(path)
    if _is_link(src) or not src.is_file():
        raise HandoffError(f"handoff source is not an ordinary file: {src}")
    if credential_like(src) or credential_like(src.name):
        raise HandoffError(f"refusing credential-like file: {src.name}")
    try:
        size = src.stat().st_size
    except OSError as e:
        raise HandoffError("cannot stat handoff source") from e
    if size > MAX_HANDOFF_BYTES:
        raise HandoffError(f"artifact too large: {src.name}")
    if workspace is not None:
        root = _workspace_root(workspace)
        _walk_no_links(src, root)
        resolved = src.resolve()
        if _is_link(resolved) or not resolved.is_file() or not _within(resolved, root):
            raise HandoffError(f"handoff source escapes workspace: {src}")
        return resolved
    _walk_no_links(src, src.parent)
    resolved = src.resolve()
    if _is_link(resolved) or not resolved.is_file():
        raise HandoffError(f"handoff source is not an ordinary file: {src}")
    if not _within(resolved, src.parent):
        raise HandoffError(f"handoff source escapes parent directory: {src}")
    return resolved


def _same_file_bytes(left: Path, right: Path) -> bool:
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError as e:
        raise HandoffError("cannot stat handoff file") from e
    if left_stat.st_size != right_stat.st_size:
        return False
    return left.read_bytes() == right.read_bytes()


def collect_direct_dep_artifacts(
    task: Mapping[str, Any],
    siblings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return declared+accepted files from succeeded direct deps only.

    Sources must live in that run's real workspace. Tail-matching an accepted
    absolute path is not enough: a same-named file outside the workspace is
    rejected, as are parent-directory links and junctions.
    """
    deps = [str(x).strip() for x in (task.get("depends_on") or []) if str(x).strip()]
    by_id = {str(row.get("task_id") or ""): row for row in siblings if isinstance(row, Mapping)}
    items: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for dep_id in deps:
        src_task = by_id.get(dep_id)
        if src_task is None:
            raise HandoffError(f"unknown direct dependency {dep_id}")
        if str(src_task.get("status") or "") != "succeeded":
            continue
        declared: list[str] = []
        for raw in src_task.get("expected_artifacts") or []:
            declared.append(safe_relative_name(str(raw)))
        if not declared:
            continue
        result = src_task.get("result") if isinstance(src_task.get("result"), Mapping) else {}
        workspace = _workspace_root(result.get("workspace") or src_task.get("workspace"))
        accepted_ok: set[str] = set()
        for raw in result.get("artifacts") or []:
            if not str(raw).strip():
                continue
            try:
                accepted_ok.add(_norm_key(_ordinary_source(Path(raw), workspace)))
            except HandoffError:
                continue
        for rel in declared:
            source = contained_path(workspace, rel, must_exist=True)
            if _norm_key(source) not in accepted_ok:
                raise HandoffError(f"declared artifact was not accepted in its workspace: {rel}")
            name_key = os.path.normcase(rel)
            if name_key in seen:
                raise HandoffError(f"conflicting handoff name {rel}")
            seen[name_key] = dep_id
            items.append(
                {
                    "relative": rel,
                    "source": source,
                    "from_task": dep_id,
                    "workspace": str(workspace),
                }
            )
    if len(items) > MAX_HANDOFF_FILES:
        raise HandoffError(f"at most {MAX_HANDOFF_FILES} handoff files are allowed")
    return items


def stage_handoff_files(items: Sequence[Mapping[str, Any]], dest_root: Path) -> list[dict[str, Any]]:
    dest_base = Path(dest_root)
    dest_base.mkdir(parents=True, exist_ok=True)
    if _is_link(dest_base):
        raise HandoffError("links and junctions are not accepted as artifacts")
    staged: list[dict[str, Any]] = []
    for item in items:
        rel = safe_relative_name(item.get("relative"))
        dest = contained_path(dest_base, rel, must_exist=False)
        workspace = Path(item["workspace"]) if item.get("workspace") else None
        source = _ordinary_source(Path(item["source"]), workspace)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.parent.exists() and _is_link(dest.parent):
            raise HandoffError("links and junctions are not accepted as artifacts")
        if dest.exists() or dest.is_symlink() or _is_link(dest):
            existing = contained_path(dest_base, rel, must_exist=True)
            if not _same_file_bytes(source, existing):
                raise HandoffError(f"refusing to overwrite existing {rel}")
            staged.append(
                {
                    "relative": rel,
                    "source": str(source),
                    "dest": str(existing),
                    "from_task": str(item.get("from_task") or ""),
                }
            )
            continue
        tmp = dest.with_name(dest.name + ".handoff.tmp")
        if _is_link(tmp) or (tmp.exists() and not tmp.is_file()):
            raise HandoffError(f"refusing to use non-file staging path for {rel}")
        if tmp.exists():
            tmp.unlink()
        try:
            shutil.copyfile(source, tmp, follow_symlinks=False)
            os.replace(tmp, dest)
        except OSError as e:
            try:
                if tmp.exists() and tmp.is_file() and not _is_link(tmp):
                    tmp.unlink()
            except OSError:
                pass
            raise HandoffError(f"failed to stage {rel}") from e
        copied = contained_path(dest_base, rel, must_exist=True)
        if not _same_file_bytes(source, copied):
            raise HandoffError(f"staged content mismatch for {rel}")
        staged.append(
            {
                "relative": rel,
                "source": str(source),
                "dest": str(copied),
                "from_task": str(item.get("from_task") or ""),
            }
        )
    return staged


def copy_staged_inputs(source_root: Path, dest_root: Path, names: Sequence[Any]) -> list[str]:
    """Copy listed relative files from the protocol directory into the real workspace."""
    rels: list[str] = []
    for raw in names or []:
        if isinstance(raw, Mapping):
            rels.append(safe_relative_name(raw.get("relative")))
        else:
            rels.append(safe_relative_name(raw))
    if not rels:
        return []
    if len(rels) != len(set(rels)):
        raise HandoffError("input_files must be unique")
    if len(rels) > MAX_HANDOFF_FILES:
        raise HandoffError(f"at most {MAX_HANDOFF_FILES} handoff files are allowed")
    src_root = _workspace_root(source_root)
    items = [
        {
            "relative": rel,
            "source": contained_path(src_root, rel, must_exist=True),
            "from_task": "",
            "workspace": str(src_root),
        }
        for rel in rels
    ]
    return [row["relative"] for row in stage_handoff_files(items, Path(dest_root))]


def handoff_direct_dependency_artifacts(
    *,
    task: Mapping[str, Any],
    siblings: Sequence[Mapping[str, Any]],
    dest_root: Path,
) -> list[dict[str, Any]]:
    return stage_handoff_files(collect_direct_dep_artifacts(task, siblings), dest_root)


__all__ = [
    "HandoffError",
    "MAX_HANDOFF_BYTES",
    "MAX_HANDOFF_FILES",
    "collect_direct_dep_artifacts",
    "copy_staged_inputs",
    "credential_like",
    "handoff_direct_dependency_artifacts",
    "safe_relative_name",
    "stage_handoff_files",
]
