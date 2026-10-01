"""Bounded multi-file manifests and consistent SQLite / JSONL snapshots.

The client records content at hash time (streamed sha256 and byte size).
Dispatch copies those exact relative paths into the task workspace and hashes
again: that is content at run time. A mismatch fails the task with
``hash_changed: <relative>`` before the worker starts.

A hash is not permission isolation. The worker sees the staged copies under
``inputs/manifest/`` (and any snapshot file pinned separately). It does not
receive the manifest root as a directory grant. Listing a directory is not
recursive unless the client manifest sets ``recursive`` true, and even then
the file-count and byte caps still apply. ``**`` is refused by default.
Symlinks and junctions are not followed.
"""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

try:
    from framework.artifact_handoff import HandoffError, credential_like, safe_relative_name
except ImportError:  # pragma: no cover - package import is the supported path
    import re

    class HandoffError(ValueError):
        pass

    _CREDENTIAL_RE = re.compile(
        r'\.env(?:[.\s"/]|$)|\.ssh|\.netrc|auth\.json|credentials|cookies|id_ed25519|id_rsa|login data',
        re.IGNORECASE,
    )

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


MAX_MANIFEST_FILES = 256
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024
_MANIFEST_FILE_CAP = 2 * 1024 * 1024
_SQLITE_MAGIC = b"SQLite format 3\x00"
_TAKEN_AT_RE_TEXT = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_CLIENT_KEYS = frozenset({"root", "include", "max_files", "max_total_bytes", "recursive"})
_WIRE_KEYS = frozenset({"root", "entries", "max_files", "max_total_bytes"})
_ENTRY_KEYS = frozenset({"relative", "sha256", "size"})
_SNAPSHOT_META_KEYS = frozenset({"kind", "source", "taken_at"})
_SNAPSHOT_KINDS = frozenset({"sqlite_snapshot", "file_snapshot"})

import re

_TAKEN_AT_RE = re.compile(_TAKEN_AT_RE_TEXT)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class InputManifestError(ValueError):
    """Manifest, snapshot, or pin refused. ``code`` is a stable machine string."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_input_manifest",
        snapshot_paths: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.snapshot_paths = list(snapshot_paths or [])


class SnapshotFile:
    """A client-side copy that may be pinned. The live source is not pinned."""

    __slots__ = ("path", "metadata")

    def __init__(self, path: Path, metadata: dict[str, str]) -> None:
        self.path = path
        self.metadata = metadata


def capability_document() -> dict[str, Any]:
    """Fields merged into ``GET /v1/capabilities``. No secrets."""
    return {
        "input_manifest": {
            "max_files": MAX_MANIFEST_FILES,
            "max_total_bytes": MAX_MANIFEST_BYTES,
            "staging": "copy_into_workspace",
        },
        "snapshots": {
            "sqlite": "client_backup_api",
            "file": "client_prefix_copy",
        },
    }


def hash_file(path: Path) -> str:
    """Stream SHA-256. Callers must already have rejected links and caps."""
    digest = hashlib.sha256()
    with _open_read(path) as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_metadata(value: Any) -> dict[str, str]:
    """Closed metadata object for a pinned snapshot. No directory paths."""
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_META_KEYS:
        raise InputManifestError(
            "snapshot metadata requires kind, source, and taken_at",
            code="invalid_external_inputs",
        )
    kind = value.get("kind")
    source = value.get("source")
    taken = value.get("taken_at")
    if kind not in _SNAPSHOT_KINDS:
        raise InputManifestError(
            "snapshot metadata kind is not supported",
            code="invalid_external_inputs",
        )
    if not isinstance(source, str) or not _basename_only(source) or credential_like(source):
        raise InputManifestError(
            "snapshot metadata source must be a non-sensitive file name",
            code="invalid_external_inputs",
        )
    if not isinstance(taken, str) or _TAKEN_AT_RE.fullmatch(taken) is None:
        raise InputManifestError(
            "snapshot metadata taken_at must be UTC ISO-8601 (YYYY-MM-DDTHH:MM:SSZ)",
            code="invalid_external_inputs",
        )
    return {"kind": str(kind), "source": source, "taken_at": taken}


def project_input_manifest(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate a request ``input_manifest``. Does not open files or hash them.

    Absent or null means no manifest. The returned object is safe to persist.
    Bytes and hashes are claims about content at hash time; dispatch checks
    content at run time.
    """
    src = payload if isinstance(payload, Mapping) else {}
    if "input_manifest" not in src or src.get("input_manifest") is None:
        return None
    raw = src.get("input_manifest")
    if not isinstance(raw, Mapping):
        raise InputManifestError("input_manifest must be an object", code="invalid_input_manifest")
    extra = sorted(str(key) for key in raw.keys() if key not in _WIRE_KEYS)
    if extra or not _WIRE_KEYS <= set(raw):
        raise InputManifestError(
            "input_manifest requires root, entries, max_files, and max_total_bytes",
            code="invalid_input_manifest",
        )
    root = raw.get("root")
    if not isinstance(root, str) or not root.strip() or "\x00" in root:
        raise InputManifestError("input_manifest root must be an absolute path", code="invalid_input_manifest")
    root_text = root.strip()
    if not Path(root_text).is_absolute():
        raise InputManifestError("input_manifest root must be an absolute path", code="invalid_input_manifest")
    max_files = _bounded_int(
        raw.get("max_files"),
        field="max_files",
        upper=MAX_MANIFEST_FILES,
        code="invalid_input_manifest",
    )
    max_bytes = _bounded_int(
        raw.get("max_total_bytes"),
        field="max_total_bytes",
        upper=MAX_MANIFEST_BYTES,
        code="invalid_input_manifest",
    )
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise InputManifestError("input_manifest entries must be a list", code="invalid_input_manifest")
    if len(entries) > max_files or len(entries) > MAX_MANIFEST_FILES:
        raise InputManifestError(
            f"input_manifest has more than max_files {min(max_files, MAX_MANIFEST_FILES)}",
            code="invalid_input_manifest",
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for item in entries:
        if not isinstance(item, Mapping) or set(item) != _ENTRY_KEYS:
            raise InputManifestError(
                "each manifest entry requires relative, sha256, and size",
                code="invalid_input_manifest",
            )
        try:
            relative = safe_relative_name(item.get("relative"))
        except HandoffError as exc:
            raise InputManifestError(
                f"illegal manifest entry: {exc}",
                code="invalid_input_manifest",
            ) from exc
        key = _dup_key(relative)
        if key in seen:
            raise InputManifestError(
                f"duplicate manifest entry: {relative}",
                code="invalid_input_manifest",
            )
        seen.add(key)
        digest = item.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest.strip()) is None:
            raise InputManifestError(
                "manifest entry sha256 must be 64 hex characters",
                code="invalid_input_manifest",
            )
        size = _bounded_int(
            item.get("size"),
            field="size",
            upper=max_bytes,
            code="invalid_input_manifest",
        )
        total += size
        if total > max_bytes or total > MAX_MANIFEST_BYTES:
            raise InputManifestError(
                f"input_manifest totals more than max_total_bytes {min(max_bytes, MAX_MANIFEST_BYTES)}",
                code="invalid_input_manifest",
            )
        normalized.append({"relative": relative, "sha256": digest.strip().lower(), "size": size})
    return {
        "root": root_text,
        "entries": normalized,
        "max_files": max_files,
        "max_total_bytes": max_bytes,
    }


def stage_input_manifest(manifest: Mapping[str, Any], workspace: Path) -> list[str]:
    """Copy manifest entries into ``workspace/inputs/manifest/<relative>``.

    Re-resolves every path. Links and paths outside the manifest root are
    refused. Hash and size are checked while streaming. On mismatch the copy
    is removed and ``hash_changed: <relative>`` is raised. Returns workspace-
    relative names for the worker contract. Does not start a worker.
    """
    checked = project_input_manifest({"input_manifest": dict(manifest)})
    if not checked or not checked["entries"]:
        return []
    root = Path(checked["root"])
    if _is_link(root) or not root.is_dir():
        raise InputManifestError(
            "input manifest root is not a real directory",
            code="invalid_input_manifest",
        )
    workspace = Path(workspace)
    if _is_link(workspace) or not workspace.is_dir():
        raise InputManifestError(
            "task workspace is not a real directory",
            code="invalid_input_manifest",
        )
    inputs = workspace / "inputs"
    if _is_link(inputs):
        raise InputManifestError("manifest_symlink_refused: inputs", code="manifest_symlink_refused")
    inputs.mkdir(parents=True, exist_ok=True)
    final = inputs / "manifest"
    if _is_link(final) or final.exists():
        raise InputManifestError(
            "manifest destination already exists",
            code="invalid_input_manifest",
        )
    partial = inputs / f".manifest-partial-{secrets.token_hex(4)}"
    placed = False
    names: list[str] = []
    try:
        partial.mkdir(mode=0o700)
        for entry in checked["entries"]:
            relative = str(entry["relative"])
            src = _contained_file(root, relative)
            dest = partial.joinpath(*PurePosixPath(relative).parts)
            if _is_link(dest):
                raise InputManifestError(
                    f"manifest_symlink_refused: {relative}",
                    code="manifest_symlink_refused",
                )
            _stream_copy_check(
                src,
                dest,
                relative=relative,
                expected_sha=str(entry["sha256"]),
                expected_size=int(entry["size"]),
            )
            names.append(f"inputs/manifest/{relative}")
        partial.rename(final)
        placed = True
    except InputManifestError:
        raise
    except OSError as exc:
        raise InputManifestError(
            "input manifest copy failed",
            code="invalid_input_manifest",
        ) from exc
    finally:
        if not placed:
            _remove_tree_inside(partial, workspace)
    return names


def merge_manifest_input_files(task: dict[str, Any], goal: Mapping[str, Any], workspace: Path) -> None:
    """Stage the goal manifest, if any, and append workspace-local input names.

    Existing handoff names stay. Manifest names are not external pins and do
    not widen the read root.
    """
    raw = goal.get("input_manifest") if isinstance(goal, Mapping) else None
    if not isinstance(raw, Mapping):
        return
    names = stage_input_manifest(raw, workspace)
    if not names:
        return
    inputs = dict(task.get("inputs") or {})
    current: list[str] = []
    listed = inputs.get("input_files")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, str) and item.strip():
                current.append(item.strip())
    for name in names:
        if name not in current:
            current.append(name)
    inputs["input_files"] = current
    task["inputs"] = inputs


def load_client_manifest(raw_path: str) -> dict[str, Any]:
    """Read a manifest JSON file and expand it. Count and size run before hashing."""
    text = str(raw_path or "").strip()
    if not text:
        raise InputManifestError("input manifest path is empty", code="bad_input_manifest")
    path = Path(text).expanduser()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise InputManifestError(
            "input manifest path cannot be resolved",
            code="bad_input_manifest",
        ) from exc
    if not resolved.is_file():
        raise InputManifestError("input manifest file does not exist", code="bad_input_manifest")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise InputManifestError("input manifest file cannot be read", code="bad_input_manifest") from exc
    if size > _MANIFEST_FILE_CAP:
        raise InputManifestError("input manifest file is too large", code="bad_input_manifest")
    try:
        doc = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputManifestError("input manifest is not a JSON object", code="bad_input_manifest") from exc
    if not isinstance(doc, dict):
        raise InputManifestError("input manifest must be a JSON object", code="bad_input_manifest")
    return build_client_manifest(doc)


def build_client_manifest(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Expand ``include`` under ``root``. Hash only after count and size pass.

    Wire shape: ``{root, entries: [{relative, sha256, size}], max_files, max_total_bytes}``.
    ``recursive`` is a client-only switch. It is not sent, and it is not a
    server-side directory grant.
    """
    if not isinstance(doc, Mapping):
        raise InputManifestError("input manifest must be an object", code="bad_input_manifest")
    unknown = sorted(str(key) for key in doc.keys() if key not in _CLIENT_KEYS)
    if unknown:
        raise InputManifestError(
            "input manifest has unknown keys: " + ", ".join(unknown),
            code="bad_input_manifest",
        )
    if "recursive" in doc and not isinstance(doc.get("recursive"), bool):
        raise InputManifestError("input manifest recursive must be a boolean", code="bad_input_manifest")
    recursive = doc.get("recursive") is True
    root = _manifest_root(doc.get("root"))
    include = doc.get("include")
    if not isinstance(include, list) or not include or not all(isinstance(item, str) and item.strip() for item in include):
        raise InputManifestError(
            "input manifest include must be a non-empty list of relative patterns",
            code="bad_input_manifest",
        )
    max_files = _optional_cap(doc, "max_files", MAX_MANIFEST_FILES)
    max_bytes = _optional_cap(doc, "max_total_bytes", MAX_MANIFEST_BYTES)
    selected = _select_files(
        root,
        [str(item) for item in include],
        recursive=recursive,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    entries: list[dict[str, Any]] = []
    for relative, path, size in selected:
        entries.append(
            {
                "relative": relative,
                "sha256": hash_file(path),
                "size": size,
            }
        )
    return {
        "root": str(root),
        "entries": entries,
        "max_files": max_files,
        "max_total_bytes": max_bytes,
    }


def assert_sqlite_pin_allowed(path: Path) -> None:
    """Refuse live WAL databases and direct ``-wal`` / ``-shm`` pins.

    A non-empty sibling ``PATH-wal`` means the file is not a stable snapshot.
    Callers should use ``--sqlite-snapshot`` instead. This does not hash.
    """
    name = path.name.lower()
    if name.endswith("-wal") or name.endswith("-shm"):
        raise InputManifestError(
            "refusing to pin a SQLite -wal or -shm sidecar",
            code="sqlite_sidecar_refused",
        )
    if _is_sqlite_db(path) and _wal_sibling_nonempty(path):
        raise InputManifestError(
            "SQLite database has a non-empty WAL sibling; use --sqlite-snapshot",
            code="sqlite_live_wal",
        )


def sqlite_snapshot(raw: str, dest_dir: Path) -> SnapshotFile:
    """Consistent copy via ``Connection.backup()`` of a read-only source.

    The snapshot is switched to ``journal_mode=DELETE`` and must pass
    ``PRAGMA integrity_check``. WAL and SHM files are not part of the result.
    Uncommitted source rows are not visible. The returned path is the snapshot
    file only.
    """
    source = _explicit_file(raw)
    _refuse_sidecar_name(source)
    if credential_like(source) or credential_like(source.name):
        raise InputManifestError(
            "refusing credential-like SQLite snapshot source name",
            code="snapshot_refused",
        )
    directory = _real_dir(dest_dir)
    dest = _unique_child(directory, source.name)
    uri = source.resolve().as_uri() + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(os.fspath(dest), timeout=30.0)
        try:
            _backup_retry(src, dst)
            mode_row = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            mode = str(mode_row[0]).lower() if mode_row and mode_row[0] is not None else ""
            if mode != "delete":
                raise InputManifestError(
                    "SQLite snapshot did not leave journal_mode=DELETE",
                    code="snapshot_failed",
                    snapshot_paths=[str(dest)],
                )
            rows = dst.execute("PRAGMA integrity_check").fetchall()
            if [tuple(row) for row in rows] != [("ok",)]:
                raise InputManifestError(
                    "SQLite snapshot failed integrity_check",
                    code="snapshot_failed",
                    snapshot_paths=[str(dest)],
                )
        finally:
            dst.close()
    except InputManifestError:
        raise
    except sqlite3.Error as exc:
        raise InputManifestError(
            "SQLite snapshot backup failed",
            code="snapshot_failed",
            snapshot_paths=[str(dest)] if dest.exists() else [],
        ) from exc
    finally:
        src.close()
    _drop_own_sidecars(dest, directory)
    for suffix in ("-wal", "-shm"):
        side = Path(str(dest) + suffix)
        try:
            side.lstat()
        except OSError:
            continue
        raise InputManifestError(
            "SQLite snapshot still has a WAL or SHM sidecar",
            code="snapshot_failed",
            snapshot_paths=[str(dest)],
        )
    return SnapshotFile(dest, _metadata("sqlite_snapshot", source))


def file_snapshot(raw: str, dest_dir: Path) -> SnapshotFile:
    """Copy the prefix measured at open so a concurrent append cannot tear it.

    ``.jsonl`` copies drop a trailing partial line. The live file is not pinned.
    """
    source = _explicit_file(raw)
    _refuse_sidecar_name(source)
    if credential_like(source) or credential_like(source.name):
        raise InputManifestError(
            "refusing credential-like snapshot source name",
            code="snapshot_refused",
        )
    directory = _real_dir(dest_dir)
    dest = _unique_child(directory, source.name)
    try:
        _copy_prefix(source, dest, jsonl=source.suffix.lower() == ".jsonl")
    except InputManifestError:
        raise
    except OSError as exc:
        raise InputManifestError(
            "file snapshot copy failed",
            code="snapshot_failed",
            snapshot_paths=[str(dest)] if dest.exists() else [],
        ) from exc
    return SnapshotFile(dest, _metadata("file_snapshot", source))


def snapshot_root() -> Path:
    """``COLLAB_SNAPSHOT_DIR`` or ``~/.cache/teleagent-collab/snapshots``."""
    raw = (os.environ.get("COLLAB_SNAPSHOT_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "teleagent-collab" / "snapshots"


def make_snapshot_dir() -> Path:
    """Create ``<root>/<utc>-<rand>`` with mode 0700 where the OS supports it."""
    root = snapshot_root()
    if root.exists() and _is_link(root):
        raise InputManifestError("snapshot root is a link", code="bad_snapshot_root")
    created_root = not root.exists()
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise InputManifestError("snapshot root cannot be created", code="bad_snapshot_root") from exc
    if created_root:
        _chmod_private(root)
    if _is_link(root) or not root.is_dir():
        raise InputManifestError("snapshot root is not a real directory", code="bad_snapshot_root")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = root / f"{stamp}-{secrets.token_hex(4)}"
    dest.mkdir(mode=0o700)
    _chmod_private(dest)
    return dest


def clean_snapshots(*, hours: float = 24.0) -> dict[str, Any]:
    """Delete snapshot directories older than ``hours`` under the snapshot root only."""
    try:
        window = float(hours)
    except (TypeError, ValueError) as exc:
        raise InputManifestError("hours must be a number", code="bad_snapshot_root") from exc
    if window < 0 or window != window:
        raise InputManifestError("hours must be zero or positive", code="bad_snapshot_root")
    root = snapshot_root()
    if _is_link(root):
        raise InputManifestError("snapshot root is a link", code="bad_snapshot_root")
    if not root.exists():
        return {"ok": True, "deleted": [], "root": str(root), "hours": window}
    if not root.is_dir():
        raise InputManifestError("snapshot root is not a real directory", code="bad_snapshot_root")
    root_res = root.resolve()
    cutoff = time.time() - (window * 3600.0)
    deleted: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if _is_link(child) or not child.is_dir():
            continue
        try:
            resolved = child.resolve()
            modified = child.lstat().st_mtime
        except OSError:
            continue
        if not resolved.is_relative_to(root_res) or resolved == root_res:
            continue
        if modified >= cutoff:
            continue
        _remove_tree_inside(child, root)
        deleted.append(str(child))
    return {"ok": True, "deleted": deleted, "root": str(root), "hours": window}


def _metadata(kind: str, source: Path) -> dict[str, str]:
    return {
        "kind": kind,
        "source": source.name,
        "taken_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _chmod_private(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        return


def _basename_only(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return Path(name).name == name


def _dup_key(relative: str) -> str:
    return os.path.normcase(relative.replace("\\", "/"))


def _bounded_int(value: Any, *, field: str, upper: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputManifestError(f"{field} must be an integer", code=code)
    if value < 0 or value > upper:
        raise InputManifestError(f"{field} is outside the allowed range", code=code)
    return value


def _optional_cap(doc: Mapping[str, Any], field: str, hard: int) -> int:
    if field not in doc or doc.get(field) is None:
        return hard
    return _bounded_int(doc.get(field), field=field, upper=hard, code="bad_input_manifest")


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
    except OSError:
        return True
    return False


def _open_read(path: Path):
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        eloop = getattr(errno, "ELOOP", None)
        if eloop is not None and exc.errno == eloop:
            raise InputManifestError(
                f"manifest_symlink_refused: {path.name}",
                code="manifest_symlink_refused",
            ) from exc
        raise
    return os.fdopen(fd, "rb")


def _manifest_root(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InputManifestError("input manifest root must be an absolute directory", code="bad_input_manifest")
    text = value.strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise InputManifestError("input manifest root must be an absolute directory", code="bad_input_manifest")
    if _is_link(path):
        raise InputManifestError(
            "input manifest root is a symlink or junction",
            code="manifest_symlink_refused",
        )
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise InputManifestError("input manifest root cannot be resolved", code="bad_input_manifest") from exc
    if _is_link(resolved) or not resolved.is_dir():
        raise InputManifestError(
            "input manifest root must be a real directory",
            code="bad_input_manifest",
        )
    return resolved


def _pattern_parts(pattern: str) -> tuple[str, ...]:
    text = pattern.strip().replace("\\", "/")
    if not text or "\x00" in text:
        raise InputManifestError("input manifest include entry is empty", code="bad_input_manifest")
    if text.startswith("/") or PureWindowsPath(text).is_absolute():
        raise InputManifestError(
            "input manifest include entries must be relative to root",
            code="bad_input_manifest",
        )
    parts = tuple(part for part in text.split("/") if part != "")
    if not parts or any(part in {".", ".."} for part in parts):
        raise InputManifestError(
            f"manifest_outside_root: {pattern.strip()}",
            code="manifest_outside_root",
        )
    return parts


def _has_glob(part: str) -> bool:
    return any(ch in part for ch in "*?[")


def _select_files(
    root: Path,
    patterns: list[str],
    *,
    recursive: bool,
    max_files: int,
    max_bytes: int,
) -> list[tuple[str, Path, int]]:
    selected: list[tuple[str, Path, int]] = []
    seen: set[str] = set()
    total = 0
    for pattern in patterns:
        parts = _pattern_parts(pattern)
        if any("**" in part for part in parts) and not recursive:
            raise InputManifestError(
                "** is refused unless the manifest sets recursive true",
                code="manifest_recursive_refused",
            )
        explicit = bool(parts) and parts[-1] != "**" and not _has_glob(parts[-1])
        matches = _walk(root, root, parts, recursive=recursive)
        kept = 0
        for match in matches:
            relative = _relative_of(root, match)
            if _is_link(match):
                raise InputManifestError(
                    f"manifest_symlink_refused: {relative}",
                    code="manifest_symlink_refused",
                )
            if credential_like(relative) or credential_like(match.name):
                raise InputManifestError(
                    f"manifest_credential_refused: {relative}",
                    code="manifest_credential_refused",
                )
            try:
                relative = safe_relative_name(relative)
            except HandoffError as exc:
                text = str(exc).lower()
                if "credential" in text:
                    raise InputManifestError(
                        f"manifest_credential_refused: {relative}",
                        code="manifest_credential_refused",
                    ) from exc
                raise InputManifestError(
                    f"manifest_outside_root: {pattern.strip()}",
                    code="manifest_outside_root",
                ) from exc
            if not _regular_file(match):
                if explicit and match.is_dir():
                    raise InputManifestError(
                        f"manifest_directory_refused: {relative}",
                        code="manifest_directory_refused",
                    )
                if explicit:
                    raise InputManifestError(
                        f"manifest entry is not a file: {relative}",
                        code="manifest_not_a_file",
                    )
                continue
            resolved = _resolved_inside(root, match, relative)
            key = _dup_key(relative)
            if key in seen:
                raise InputManifestError(
                    f"duplicate manifest entry: {relative}",
                    code="bad_input_manifest",
                )
            seen.add(key)
            try:
                size = match.lstat().st_size
            except OSError as exc:
                raise InputManifestError(
                    f"manifest entry cannot be stat: {relative}",
                    code="bad_input_manifest",
                ) from exc
            if size < 0 or size > max_bytes:
                raise InputManifestError(
                    f"input manifest totals more than max_total_bytes {max_bytes}",
                    code="manifest_too_large",
                )
            selected.append((relative, resolved, int(size)))
            kept += 1
            total += int(size)
            if len(selected) > max_files:
                raise InputManifestError(
                    f"input manifest has more than max_files {max_files}",
                    code="manifest_too_many_files",
                )
            if total > max_bytes:
                raise InputManifestError(
                    f"input manifest totals more than max_total_bytes {max_bytes}",
                    code="manifest_too_large",
                )
        if kept == 0:
            raise InputManifestError(
                f"input manifest pattern matched nothing: {pattern.strip()}",
                code="bad_input_manifest",
            )
    selected.sort(key=lambda item: item[0])
    return selected


def _walk(origin: Path, base: Path, parts: tuple[str, ...], *, recursive: bool) -> list[Path]:
    if not parts:
        return [base]
    head, rest = parts[0], parts[1:]
    if head == "**":
        if not recursive:
            raise InputManifestError(
                "** is refused unless the manifest sets recursive true",
                code="manifest_recursive_refused",
            )
        found: list[Path] = []
        seen: set[str] = set()
        for directory in _directories_under(base):
            branched = _walk(origin, directory, rest, recursive=True) if rest else _files_in(origin, directory)
            for item in branched:
                key = _dup_key(str(item))
                if key in seen:
                    continue
                seen.add(key)
                found.append(item)
        return found
    if _is_link(base) or not base.is_dir():
        return []
    if _has_glob(head):
        found = []
        for child in _iter_dir(base):
            if not fnmatch.fnmatch(child.name, head):
                continue
            if _is_link(child):
                raise InputManifestError(
                    f"manifest_symlink_refused: {_relative_of(origin, child)}",
                    code="manifest_symlink_refused",
                )
            found.extend(_walk(origin, child, rest, recursive=recursive))
        return found
    child = base / head
    if _is_link(child):
        raise InputManifestError(
            f"manifest_symlink_refused: {_relative_of(origin, child)}",
            code="manifest_symlink_refused",
        )
    try:
        exists = child.exists()
    except OSError:
        return []
    if not exists:
        return []
    return _walk(origin, child, rest, recursive=recursive)


def _directories_under(base: Path) -> list[Path]:
    found = [base]
    stack = [base]
    while stack:
        current = stack.pop()
        if _is_link(current) or not current.is_dir():
            continue
        for child in _iter_dir(current):
            if _is_link(child):
                continue
            if child.is_dir():
                found.append(child)
                stack.append(child)
    return found


def _files_in(origin: Path, directory: Path) -> list[Path]:
    if _is_link(directory) or not directory.is_dir():
        return []
    found = []
    for child in _iter_dir(directory):
        if _is_link(child):
            raise InputManifestError(
                f"manifest_symlink_refused: {_relative_of(origin, child)}",
                code="manifest_symlink_refused",
            )
        if _regular_file(child):
            found.append(child)
    return found


def _iter_dir(directory: Path) -> list[Path]:
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise InputManifestError("input manifest directory cannot be listed", code="bad_input_manifest") from exc
    children.sort(key=lambda item: item.name)
    return children


def _relative_of(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _regular_file(path: Path) -> bool:
    if _is_link(path):
        return False
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)


def _resolved_inside(root: Path, path: Path, relative: str) -> Path:
    """Re-check containment. A link or a resolved path outside root is refused."""
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if _is_link(current):
            raise InputManifestError(
                f"manifest_symlink_refused: {relative}",
                code="manifest_symlink_refused",
            )
    try:
        resolved = current.resolve()
    except OSError as exc:
        raise InputManifestError(
            f"manifest_outside_root: {relative}",
            code="manifest_outside_root",
        ) from exc
    try:
        root_res = root.resolve()
    except OSError as exc:
        raise InputManifestError("input manifest root cannot be resolved", code="bad_input_manifest") from exc
    if _is_link(resolved) or not resolved.is_relative_to(root_res):
        raise InputManifestError(
            f"manifest_outside_root: {relative}",
            code="manifest_outside_root",
        )
    # Inside the root but no longer the regular file that was hashed.
    if not _regular_file(resolved):
        raise InputManifestError(f"hash_changed: {relative}", code="hash_changed")
    return resolved


def _contained_file(root: Path, relative: str) -> Path:
    safe = safe_relative_name(relative)
    if credential_like(safe):
        raise InputManifestError(
            f"manifest_credential_refused: {safe}",
            code="manifest_credential_refused",
        )
    return _resolved_inside(root, root.joinpath(*PurePosixPath(safe).parts), safe)


def _stream_copy_check(
    src: Path,
    dest: Path,
    *,
    relative: str,
    expected_sha: str,
    expected_size: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        src_fd = os.open(os.fspath(src), flags)
    except OSError as exc:
        eloop = getattr(errno, "ELOOP", None)
        if eloop is not None and exc.errno == eloop:
            raise InputManifestError(
                f"manifest_symlink_refused: {relative}",
                code="manifest_symlink_refused",
            ) from exc
        raise InputManifestError(f"hash_changed: {relative}", code="hash_changed") from exc
    out_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        out_fd = os.open(os.fspath(dest), out_flags, 0o600)
    except OSError as exc:
        os.close(src_fd)
        raise InputManifestError("input manifest copy failed", code="invalid_input_manifest") from exc
    try:
        with os.fdopen(src_fd, "rb") as inp, os.fdopen(out_fd, "wb") as out:
            src_fd = -1
            out_fd = -1
            while True:
                chunk = inp.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                copied += len(chunk)
    finally:
        if src_fd >= 0:
            os.close(src_fd)
        if out_fd >= 0:
            os.close(out_fd)
    actual = digest.hexdigest()
    if copied != expected_size or actual != expected_sha.lower():
        try:
            dest.unlink()
        except OSError:
            pass
        raise InputManifestError(f"hash_changed: {relative}", code="hash_changed")


def _explicit_file(raw: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise InputManifestError("snapshot path is empty", code="bad_external_input")
    try:
        path = Path(text).expanduser().resolve()
    except OSError as exc:
        raise InputManifestError(
            f"snapshot path cannot be resolved: {Path(text).name}",
            code="bad_external_input",
        ) from exc
    if _is_link(path) or not path.is_file():
        raise InputManifestError(
            f"snapshot source is not a file: {path.name}",
            code="bad_external_input",
        )
    return path


def _refuse_sidecar_name(path: Path) -> None:
    name = path.name.lower()
    if name.endswith("-wal") or name.endswith("-shm"):
        raise InputManifestError(
            "refusing to pin a SQLite -wal or -shm sidecar",
            code="sqlite_sidecar_refused",
        )


def _real_dir(path: Path) -> Path:
    directory = Path(path)
    if _is_link(directory) or not directory.is_dir():
        raise InputManifestError("snapshot directory is not a real directory", code="snapshot_failed")
    return directory


def _unique_child(directory: Path, name: str) -> Path:
    if not _basename_only(name):
        raise InputManifestError("snapshot file name is not a single path segment", code="snapshot_failed")
    candidate = directory / name
    if not candidate.exists() and not _is_link(candidate):
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    for index in range(2, 100):
        nxt = directory / f"{stem}-{index}{suffix}"
        if not nxt.exists() and not _is_link(nxt):
            return nxt
    raise InputManifestError("snapshot directory has too many name collisions", code="snapshot_failed")


def _backup_retry(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    last: sqlite3.Error | None = None
    for _ in range(5):
        try:
            src.backup(dst)
            return
        except sqlite3.OperationalError as exc:
            last = exc
            time.sleep(0.05)
    raise InputManifestError("SQLite snapshot backup failed", code="snapshot_failed") from last


def _drop_own_sidecars(db_path: Path, root: Path) -> None:
    root_res = root.resolve()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        try:
            side.lstat()
        except OSError:
            continue
        parent = side.parent
        try:
            parent_res = parent.resolve()
        except OSError:
            continue
        if parent_res != root_res and not parent_res.is_relative_to(root_res):
            continue
        try:
            if _is_link(side):
                side.unlink()
                continue
            resolved = side.resolve()
            if resolved.is_relative_to(root_res):
                side.unlink()
        except OSError:
            continue


def _is_sqlite_db(path: Path) -> bool:
    try:
        with _open_read(path) as handle:
            header = handle.read(len(_SQLITE_MAGIC))
    except OSError:
        return False
    except InputManifestError:
        return False
    return header == _SQLITE_MAGIC


def _wal_sibling_nonempty(db: Path) -> bool:
    wal = Path(str(db) + "-wal")
    try:
        st = wal.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or _is_link(wal):
        return True
    if not stat.S_ISREG(st.st_mode):
        return False
    return st.st_size > 0


def _copy_prefix(src: Path, dest: Path, *, jsonl: bool) -> None:
    in_fd = os.open(os.fspath(src), os.O_RDONLY)
    try:
        out_fd = os.open(os.fspath(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        os.close(in_fd)
        raise
    copied = 0
    last_nl = -1
    try:
        with os.fdopen(in_fd, "rb") as inp, os.fdopen(out_fd, "wb") as out:
            in_fd = -1
            out_fd = -1
            remaining = os.fstat(inp.fileno()).st_size
            if remaining < 0:
                remaining = 0
            while remaining > 0:
                chunk = inp.read(min(_HASH_CHUNK, remaining))
                if not chunk:
                    break
                if jsonl:
                    idx = chunk.rfind(b"\n")
                    if idx != -1:
                        last_nl = copied + idx
                out.write(chunk)
                copied += len(chunk)
                remaining -= len(chunk)
    finally:
        if in_fd >= 0:
            os.close(in_fd)
        if out_fd >= 0:
            os.close(out_fd)
    if not jsonl:
        return
    if copied == 0:
        keep = 0
    elif last_nl == copied - 1:
        keep = copied
    elif last_nl < 0:
        keep = 0
    else:
        keep = last_nl + 1
    if keep != copied:
        os.truncate(dest, keep)


def _remove_tree_inside(path: Path, root: Path) -> None:
    """Delete ``path`` only when it stays inside ``root``. Links are unlinked, not followed."""
    if not path.exists() and not _is_link(path):
        return
    try:
        root_res = root.resolve()
    except OSError as exc:
        raise InputManifestError("snapshot root cannot be resolved", code="bad_snapshot_root") from exc
    _remove_inside(path, root_res)


def _remove_inside(path: Path, root_res: Path) -> None:
    if _is_link(path):
        parent = path.parent.resolve()
        if parent != root_res and not parent.is_relative_to(root_res):
            raise InputManifestError(
                "refusing to delete outside the snapshot root",
                code="bad_snapshot_root",
            )
        path.unlink()
        return
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise InputManifestError("snapshot path cannot be resolved", code="bad_snapshot_root") from exc
    if resolved != root_res and not resolved.is_relative_to(root_res):
        raise InputManifestError(
            "refusing to delete outside the snapshot root",
            code="bad_snapshot_root",
        )
    if path.is_dir():
        for child in list(path.iterdir()):
            _remove_inside(child, root_res)
        path.rmdir()
        return
    path.unlink()
