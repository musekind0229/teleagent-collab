#!/usr/bin/env python3
"""Minimal Application API client for Hermes (or any terminal agent).

Thin HTTP wrapper over collab-service loopback API. Does NOT own account
pools, agy subprocesses, or Hermes ledger state.

Env (first non-empty wins). Only these two keys are read from dotenv;
other dotenv entries are not injected into the process environment:
  COLLAB_API_BASE   default http://127.0.0.1:8765
  COLLAB_API_TOKEN  optional; sent as Authorization: Bearer …

Lookup order:
  1. process environment (stripped; empty does not count)
  2. COLLAB_ENV_FILE, when set and non-empty
  3. HERMES_HOME/.env, when HERMES_HOME is set and non-empty
  4. Windows: %LOCALAPPDATA%\\hermes\\.env
     (skipped when LOCALAPPDATA is missing)
     other OS: ~/.hermes/.env

Subcommands: open | ping | status | report | wait | pending | decide | snapshots-clean
Stdout: one JSON object (single line). Default is pure ASCII
(ensure_ascii=True); non-ASCII becomes \\uXXXX so PowerShell 5.1 pipes
(any code page) and Hermes UTF-8 decoding both keep the text.
ConvertFrom-Json / json.loads restore the original characters.
--unicode (before the subcommand, same level as --http-timeout) or
COLLAB_JSON_UNICODE=1|true|yes|on emits raw UTF-8 and reconfigures
stdout to utf-8 when the stream has reconfigure. main() also sets
stdout/stderr errors=backslashreplace (encoding unchanged) when
reconfigure exists, so a mismatched code page does not raise
UnicodeEncodeError.

status, report, and wait print a stable summary by default. --full on
that subcommand, or COLLAB_OUTPUT_FULL=1|true|yes|on, prints the raw
payload. The summary never includes the goal contract, worker response
text, or stdout/stderr.

Exit codes:
  0  EXIT_OK               success (wait: state completed)
  1  EXIT_ERROR            HTTP/transport/API error, or payload ok=false
  2  EXIT_FAILED           wait ended in failed or cancelled
                            (wait.kind task_failed or task_cancelled;
                            wait.task_timeout when the goal wall budget fired,
                            or task_failed and primary_failure.source is
                            worker_timeout)
  3  EXIT_TIMEOUT          wait hit the client observation window
                            (wait.kind observation_timeout; not a task failure)
  4  EXIT_NEED_HUMAN       a human decision is required (lead-owned rows keep polling)
                            (wait.kind need_human)
  5  EXIT_DECISION_REFUSED decide: server refused the decision (HTTP 409).
                          Body keeps code, error, and contamination/hint when present.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PosixPath, WindowsPath
from typing import Any

# pathlib.Path re-reads os.name and cannot construct WindowsPath on POSIX
# (or PosixPath on Windows). Bind the flavour at import so tests can patch
# os.name and still receive a Path.
_Path = WindowsPath if os.name == "nt" else PosixPath

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from framework.input_manifest import (  # noqa: E402
    InputManifestError,
    assert_sqlite_pin_allowed,
    clean_snapshots,
    file_snapshot,
    load_client_manifest,
    make_snapshot_dir,
    sqlite_snapshot,
)

DEFAULT_BASE = "http://127.0.0.1:8765"
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
SUCCESS_STATES = frozenset({"completed"})
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAILED = 2
EXIT_TIMEOUT = 3
EXIT_NEED_HUMAN = 4
EXIT_DECISION_REFUSED = 5
_SUMMARY_LIMIT = 200
# Server enforces the same limit (src/framework/app_service.py MAX_EXTERNAL_INPUTS).
MAX_EXTERNAL_INPUTS = 8
_HASH_CHUNK = 1024 * 1024
_SUMMARY_TEXT_CAP = 300
_FULL_HINT = "rerun with --full"
_OBSERVATION_NOTE = (
    "Observation window expired. This is NOT a task failure; "
    "do not re-open or retry. Repeat wait on the same request_id."
)
_PROGRESS_KEYS = (
    "phase",
    "percent",
    "pct",
    "percentage",
    "step",
    "steps_done",
    "steps_total",
    "completed",
    "total",
    "message",
    "fraction",
    "eta_sec",
    "current",
    "label",
)


class ClientError(Exception):
    """Transport or API failure with a JSON-serializable payload."""

    def __init__(self, payload: dict[str, Any], *, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(str(payload.get("error") or payload.get("code") or "error"))
        self.payload = payload
        self.exit_code = exit_code


_DOTENV_KEYS = frozenset({"COLLAB_API_BASE", "COLLAB_API_TOKEN"})


def _dotenv_path() -> Path | None:
    """Dotenv file to consult, or None when the Windows default is unset."""
    explicit = (os.environ.get("COLLAB_ENV_FILE") or "").strip()
    if explicit:
        return _Path(explicit)
    hermes_home = (os.environ.get("HERMES_HOME") or "").strip()
    if hermes_home:
        return _Path(hermes_home) / ".env"
    if os.name == "nt":
        local_app = (os.environ.get("LOCALAPPDATA") or "").strip()
        if not local_app:
            return None
        return _Path(local_app) / "hermes" / ".env"
    home = _Path.home()
    return home.joinpath(".hermes", ".env")


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Missing or unreadable files yield {}."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    parsed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
            if not line or line.startswith("#"):
                continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            comment_at = value.find(" #")
            if comment_at != -1:
                value = value[:comment_at].rstrip()
        parsed[key] = value
    return parsed


def _setting(name: str) -> tuple[str, str]:
    """Return (value, source) where source is env, dotenv, or none.

    Reads the dotenv file on every call. Does not write os.environ.
    """
    env_val = (os.environ.get(name) or "").strip()
    if env_val:
        return env_val, "env"
    if name not in _DOTENV_KEYS:
        return "", "none"
    path = _dotenv_path()
    if path is None:
        return "", "none"
    file_val = _read_dotenv(path).get(name, "")
    if file_val.strip():
        return file_val, "dotenv"
    return "", "none"


def _base_url() -> str:
    value, _source = _setting("COLLAB_API_BASE")
    return (value or DEFAULT_BASE).rstrip("/")


def _token() -> str:
    value, _source = _setting("COLLAB_API_TOKEN")
    return value


_JSON_UNICODE_ON = frozenset({"1", "true", "yes", "on"})


def _reconfigure(stream: Any, **kwargs: Any) -> None:
    """Call stream.reconfigure(**kwargs) when present. Ignore failures."""
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(**kwargs)
    except Exception:
        return


def _json_unicode_enabled(flag: bool = False) -> bool:
    """True for --unicode or COLLAB_JSON_UNICODE=1|true|yes|on."""
    if flag:
        return True
    raw = (os.environ.get("COLLAB_JSON_UNICODE") or "").strip().lower()
    return raw in _JSON_UNICODE_ON


def _emit(
    payload: dict[str, Any],
    *,
    unicode: bool = False,
    stream: Any = None,
) -> None:
    """Print one JSON line.

    Default (unicode=False) is ASCII-only (\\uXXXX escapes). unicode=True
    reconfigures sys.stdout to utf-8 when possible and writes raw characters.
    """
    use_unicode = bool(unicode)
    if use_unicode:
        _reconfigure(sys.stdout, encoding="utf-8")
    text = json.dumps(payload, ensure_ascii=not use_unicode, default=str)
    print(text, file=sys.stdout if stream is None else stream)


def _headers(*, with_body: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if with_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """HTTP JSON call. path is absolute under base (e.g. /v1/requests/…)."""
    url = _base_url() + path
    data = None
    headers = _headers(with_body=body is not None)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw or e.reason, "code": "http_error"}
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "non-object error body", "code": "http_error"}
        payload.setdefault("ok", False)
        payload.setdefault("http_status", int(e.code))
        if int(e.code) in (401, 403):
            env_path = _dotenv_path()
            payload["auth"] = {
                "token_source": _setting("COLLAB_API_TOKEN")[1],
                "env_file": None if env_path is None else str(env_path),
            }
        raise ClientError(payload, exit_code=EXIT_ERROR) from e
    except urllib.error.URLError as e:
        raise ClientError(
            {
                "ok": False,
                "code": "transport_error",
                "error": str(getattr(e, "reason", e)),
            },
            exit_code=EXIT_ERROR,
        ) from e
    except TimeoutError as e:
        raise ClientError(
            {"ok": False, "code": "timeout", "error": "HTTP request timed out"},
            exit_code=EXIT_ERROR,
        ) from e

    if not raw.strip():
        return {"ok": True, "http_status": status}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClientError(
            {
                "ok": False,
                "code": "invalid_json",
                "error": f"response is not JSON: {e}",
                "http_status": status,
            },
            exit_code=EXIT_ERROR,
        ) from e
    if not isinstance(payload, dict):
        raise ClientError(
            {
                "ok": False,
                "code": "invalid_json",
                "error": "response JSON must be an object",
                "http_status": status,
            },
            exit_code=EXIT_ERROR,
        )
    payload.setdefault("http_status", status)
    return payload


def _encode_id(request_id: str) -> str:
    return urllib.parse.quote(str(request_id), safe="")


def _sha256_file(path: Path) -> str:
    """Stream SHA-256 so a large pin is not loaded with read_bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _external_input_record(raw: str) -> dict[str, str]:
    """Absolute path plus sha256. Missing files are a client error, not HTTP."""
    text = str(raw or "").strip()
    if not text:
        raise ClientError(
            {"ok": False, "code": "bad_external_input", "error": "external input path is empty"},
            exit_code=EXIT_ERROR,
        )
    try:
        resolved = Path(text).expanduser().resolve()
    except OSError as e:
        raise ClientError(
            {
                "ok": False,
                "code": "bad_external_input",
                "error": f"external input path cannot be resolved: {text}",
            },
            exit_code=EXIT_ERROR,
        ) from e
    if not resolved.is_file():
        raise ClientError(
            {
                "ok": False,
                "code": "bad_external_input",
                "error": f"external input file does not exist: {text}",
            },
            exit_code=EXIT_ERROR,
        )
    try:
        assert_sqlite_pin_allowed(resolved)
    except InputManifestError as exc:
        raise ClientError(
            {"ok": False, "code": exc.code, "error": str(exc)},
            exit_code=EXIT_ERROR,
        ) from exc
    try:
        digest = _sha256_file(resolved)
    except OSError as e:
        raise ClientError(
            {
                "ok": False,
                "code": "bad_external_input",
                "error": f"external input file cannot be read: {text}",
            },
            exit_code=EXIT_ERROR,
        ) from e
    return {"path": str(resolved), "sha256": digest}


def cmd_open(args: argparse.Namespace) -> dict[str, Any]:
    artifacts = list(args.artifact or [])
    if not artifacts:
        artifacts = ["delivery.md"]
    must = list(args.must or [])
    must_not = list(args.must_not or [])
    body: dict[str, Any] = {
        "client_id": args.client_id,
        "title": args.title or (args.goal[:80] if args.goal else "hermes-collab"),
        "goal": args.goal,
        "boundaries": {"must": must, "must_not": must_not},
        "acceptance": {
            "artifacts": artifacts,
            "text": args.acceptance_text
            or (", ".join(artifacts) + " exist"),
        },
        "budget": {
            "wall_sec": int(args.wall_sec),
            "max_reworks": int(args.max_reworks),
        },
    }
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key
    # Backend is selected when collab-service starts. Optional flag is a
    # caller annotation only (server ignores unknown fields today).
    if args.backend:
        body["caller_backend_hint"] = args.backend
    # Count before any path resolve, open, or hash. Snapshots share the pin cap.
    # The server rejects the same external_inputs limit. Manifest files are
    # separate and are staged into the workspace, not pinned.
    raw_inputs = list(getattr(args, "external_input", None) or [])
    raw_sqlite = list(getattr(args, "sqlite_snapshot", None) or [])
    raw_file_snaps = list(getattr(args, "file_snapshot", None) or [])
    if len(raw_inputs) + len(raw_sqlite) + len(raw_file_snaps) > MAX_EXTERNAL_INPUTS:
        raise ClientError(
            {
                "ok": False,
                "code": "too_many_external_inputs",
                "error": (
                    f"external_inputs must have at most {MAX_EXTERNAL_INPUTS} entries"
                ),
            },
            exit_code=EXIT_ERROR,
        )
    manifest_arg = str(getattr(args, "input_manifest", "") or "").strip()
    if manifest_arg:
        try:
            body["input_manifest"] = load_client_manifest(manifest_arg)
        except InputManifestError as exc:
            raise _manifest_client_error(exc) from exc
    pins: list[dict[str, Any]] = []
    snapshot_paths: list[str] = []
    snap_dir: Path | None = None

    def _one_snapshot_dir() -> Path:
        nonlocal snap_dir
        if snap_dir is None:
            snap_dir = make_snapshot_dir()
        return snap_dir

    try:
        for item in raw_inputs:
            pins.append(_external_input_record(item))
        for item in raw_sqlite:
            snap = sqlite_snapshot(item, _one_snapshot_dir())
            snapshot_paths.append(str(snap.path))
            rec = _external_input_record(str(snap.path))
            rec["metadata"] = dict(snap.metadata)
            pins.append(rec)
        for item in raw_file_snaps:
            snap = file_snapshot(item, _one_snapshot_dir())
            snapshot_paths.append(str(snap.path))
            rec = _external_input_record(str(snap.path))
            rec["metadata"] = dict(snap.metadata)
            pins.append(rec)
    except InputManifestError as exc:
        raise _manifest_client_error(exc, snapshot_paths) from exc
    except ClientError as exc:
        if snapshot_paths:
            merged = dict(exc.payload)
            prior = merged.get("snapshots")
            paths = [str(item) for item in prior] if isinstance(prior, list) else []
            for path in snapshot_paths:
                if path not in paths:
                    paths.append(path)
            merged["snapshots"] = paths
            exc.payload = merged
        raise
    if pins:
        body["external_inputs"] = pins
    required = [
        str(item).strip()
        for item in (getattr(args, "require_capability", None) or [])
        if str(item).strip()
    ]
    if required:
        body["required_capabilities"] = required
    if getattr(args, "ack_prompt_only_inputs", False):
        body["acknowledge_prompt_only_inputs"] = True
    try:
        payload = request_json("POST", "/v1/requests", body=body, timeout=float(args.http_timeout))
    except ClientError as exc:
        if snapshot_paths:
            merged = dict(exc.payload)
            merged["snapshots"] = list(snapshot_paths)
            exc.payload = merged
        raise
    if snapshot_paths:
        payload["snapshots"] = list(snapshot_paths)
    return payload


def _manifest_client_error(
    exc: InputManifestError,
    snapshot_paths: list[str] | None = None,
) -> ClientError:
    paths = list(snapshot_paths or [])
    paths.extend(getattr(exc, "snapshot_paths", []) or [])
    payload: dict[str, Any] = {"ok": False, "code": exc.code, "error": str(exc)}
    if paths:
        payload["snapshots"] = paths
    return ClientError(payload, exit_code=EXIT_ERROR)


def cmd_snapshots_clean(args: argparse.Namespace) -> dict[str, Any]:
    """Delete snapshot directories older than N hours. Never leaves the snapshot root."""
    try:
        return clean_snapshots(hours=float(args.hours))
    except InputManifestError as exc:
        raise _manifest_client_error(exc) from exc


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    rid = _encode_id(args.request_id)
    return request_json("GET", f"/v1/requests/{rid}", timeout=float(args.http_timeout))


def cmd_report(args: argparse.Namespace) -> dict[str, Any]:
    rid = _encode_id(args.request_id)
    return request_json("GET", f"/v1/requests/{rid}/report", timeout=float(args.http_timeout))


def cmd_decide(args: argparse.Namespace) -> dict[str, Any]:
    """POST a human verdict. HTTP 409 exits 5; other failures exit 1.

    The error payload is the server JSON (code, error, http_status) plus
    contamination / hint when the server sent them. Ids are percent-encoded.
    """
    rid = _encode_id(args.request_id)
    did = _encode_id(args.decision_id)
    body: dict[str, Any] = {"verdict": str(args.verdict)}
    reason = str(args.reason or "")
    if reason:
        body["reason"] = reason
    raw_answers = str(args.answers or "")
    if raw_answers:
        try:
            parsed = json.loads(raw_answers)
        except json.JSONDecodeError as e:
            raise ClientError(
                {"ok": False, "code": "invalid_answers", "error": "answers must be a JSON array"},
                exit_code=EXIT_ERROR,
            ) from e
        if not isinstance(parsed, list):
            raise ClientError(
                {"ok": False, "code": "invalid_answers", "error": "answers must be a JSON array"},
                exit_code=EXIT_ERROR,
            )
        body["answers"] = parsed
    try:
        return request_json(
            "POST",
            f"/v1/requests/{rid}/decisions/{did}",
            body=body,
            timeout=float(args.http_timeout),
        )
    except ClientError as exc:
        status = exc.payload.get("http_status")
        try:
            refused = int(status) == 409
        except (TypeError, ValueError):
            refused = False
        if refused:
            exc.exit_code = EXIT_DECISION_REFUSED
        raise


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _one_line(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    return " ".join(text.split())[:limit]


_NATIVE_DECISION_KINDS = frozenset({"permission", "question", "review", "system_action"})


def _stripped(value: Any) -> str | None:
    text = _nonempty_str(value)
    if text is None:
        return None
    return text.strip()


def _contamination_bits(scan: dict[str, Any]) -> str:
    """``AI生成x1, U+200Bx3`` from an already-scanned contamination object."""
    parts: list[str] = []
    marks = scan.get("aigc_marks")
    if isinstance(marks, dict):
        for key, count in marks.items():
            label = _stripped(key)
            if label is None:
                continue
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                parts.append(f"{label}x{count}")
    invisible = scan.get("invisible")
    if isinstance(invisible, dict):
        for key, count in invisible.items():
            label = _stripped(str(key)) if not isinstance(key, str) else _stripped(key)
            if label is None:
                continue
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                parts.append(f"{label}x{count}")
    return ", ".join(parts)


def _contamination_prefix(artifacts: dict[str, Any]) -> str:
    """``CONTAMINATED name: counts; `` for each contaminated artifact, or ``""``."""
    chunks: list[str] = []
    for name, meta in artifacts.items():
        if not isinstance(meta, dict):
            continue
        scan = meta.get("contamination")
        if not isinstance(scan, dict) or scan.get("contaminated") is not True:
            continue
        label = _stripped(name)
        if label is None:
            continue
        bits = _contamination_bits(scan)
        if bits:
            chunks.append(f"CONTAMINATED {label}: {bits}")
        else:
            chunks.append(f"CONTAMINATED {label}")
    if not chunks:
        return ""
    return "; ".join(chunks) + "; "


def _review_payload_summary(payload: dict[str, Any]) -> str | None:
    """review: artifacts name(bytesB), ...; tools a,b; finish=...; violations=N.

    Skip any part that is missing or the wrong shape. Artifact previews and
    tool inputs/outputs are never included (previews may carry watermarks
    or zero-width characters). When an artifact's ``contamination.contaminated``
    is true, the line is prefixed with ``CONTAMINATED <name>: <counts>; ``
    and then truncated to 200 characters so the prefix stays first.
    """
    parts: list[str] = []
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        rendered: list[str] = []
        for name, meta in artifacts.items():
            label = _stripped(name)
            if label is None:
                continue
            if isinstance(meta, dict):
                nbytes = meta.get("bytes")
                if isinstance(nbytes, int) and not isinstance(nbytes, bool):
                    label = f"{label}({nbytes}B)"
            rendered.append(label)
        if rendered:
            parts.append("artifacts " + ", ".join(rendered))
    tools = payload.get("tools")
    if isinstance(tools, list):
        names: list[str] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            tool = _stripped(item.get("tool"))
            if tool is not None:
                names.append(tool)
        if names:
            parts.append("tools " + ",".join(names))
    finish = _stripped(payload.get("finish"))
    if finish is not None:
        parts.append("finish=" + finish)
    violations = payload.get("policy_violations")
    if isinstance(violations, list):
        parts.append(f"violations={len(violations)}")
    prefix = _contamination_prefix(artifacts) if isinstance(artifacts, dict) else ""
    if not parts and not prefix:
        return None
    body = ("review: " + "; ".join(parts)) if parts else ""
    text = prefix + body if body else prefix.rstrip("; ").rstrip()
    if prefix:
        return _one_line(text)
    return text


def _shorten_middle(text: str, max_len: int) -> str:
    """Keep the head and tail of ``text`` within ``max_len``, joined by ``…``."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len == 1:
        return text[:1]
    inner = max_len - 1
    head_len = inner // 2
    tail_len = inner - head_len
    return text[:head_len] + "\u2026" + text[-tail_len:]


def _fit_pattern_lengths(lengths: list[int], budget: int) -> list[int]:
    """Shrink the longest lengths so they sum to at most ``budget``."""
    lengths = [max(0, int(size)) for size in lengths]
    if sum(lengths) <= budget or not lengths:
        return lengths
    if budget <= 0:
        return [0 for _ in lengths]
    lo, hi = 0, max(lengths)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if sum(min(size, mid) for size in lengths) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    fitted = [min(size, best) for size in lengths]
    extra = budget - sum(fitted)
    for index in sorted(range(len(lengths)), key=lambda i: lengths[i], reverse=True):
        if extra <= 0:
            break
        room = lengths[index] - fitted[index]
        if room <= 0:
            continue
        give = min(extra, room)
        fitted[index] += give
        extra -= give
    return fitted


def _scope_entry_parts(item: Any) -> tuple[str, str, str] | None:
    """``(pattern, protected, expendable)`` or None.

    ``protected`` is the flag, the file count and the first file name.
    ``expendable`` is the rest of the file list, then ``[truncated]`` if set.
    """
    if not isinstance(item, dict):
        return None
    pattern = _stripped(item.get("pattern"))
    if pattern is None:
        return None
    pattern = " ".join(pattern.split())
    names: list[str] = []
    files = item.get("files")
    if isinstance(files, list):
        for name in files:
            text = _stripped(name)
            if text is not None:
                names.append(" ".join(text.split()))
    flag = "only pinned" if item.get("only_pinned") is True else "NOT ONLY PINNED"
    count = len(names)
    trunc = " [truncated]" if item.get("truncated") is True else ""
    if names:
        protected = f" [{flag}] -> dir contains {count} file(s): {names[0]}"
        rest = ", ".join(names[1:])
        expendable = (", " + rest if rest else "") + trunc
    else:
        protected = f" [{flag}] -> dir contains {count} file(s)"
        expendable = trunc
    return pattern, protected, expendable


def _format_permission_scope(payload: dict[str, Any], scope: Any, limit: int = _SUMMARY_LIMIT) -> str | None:
    """Same line as the controller summary.

    ``permission: <perm> <pattern> [only pinned] -> dir contains N file(s): ...``.
    The flag always sits immediately after the pattern. Past ``limit``, patterns
    are shortened in the middle (head + ``…`` + tail, so a tail like ``/0/*``
    remains) so the flag, the file count and the first file name still fit.
    """
    if not isinstance(scope, list) or not scope:
        return None
    perm = _stripped(payload.get("permission")) or "external_directory"
    perm = " ".join(perm.split())
    entries: list[tuple[str, str, str]] = []
    for item in scope:
        parts = _scope_entry_parts(item)
        if parts:
            entries.append(parts)
    if not entries:
        return None
    prefix = f"permission: {perm} "
    separator = "; "

    def assemble(patterns: list[str], extras: list[str]) -> str:
        chunks: list[str] = []
        for pattern, (_original, protected, _expendable), extra in zip(patterns, entries, extras):
            chunks.append(pattern + protected + extra)
        return prefix + separator.join(chunks)

    full_patterns = [pattern for pattern, _protected, _expendable in entries]
    full_extras = [expendable for _pattern, _protected, expendable in entries]
    text = assemble(full_patterns, full_extras)
    if len(text) <= limit:
        return text
    protected_len = len(prefix) + len(separator) * (len(entries) - 1)
    protected_len += sum(len(protected) for _pattern, protected, _expendable in entries)
    pattern_budget = limit - protected_len
    if pattern_budget < 0:
        return assemble(["" for _ in entries], ["" for _ in entries])[:limit]
    lengths = _fit_pattern_lengths([len(pattern) for pattern in full_patterns], pattern_budget)
    fitted = [_shorten_middle(pattern, length) for pattern, length in zip(full_patterns, lengths)]
    room = limit - len(assemble(fitted, ["" for _ in entries]))
    extras: list[str] = []
    for expendable in full_extras:
        if room <= 0 or not expendable:
            extras.append("")
            continue
        if len(expendable) <= room:
            extras.append(expendable)
            room -= len(expendable)
            continue
        snippet = expendable[:room]
        cut = snippet.rfind(",")
        if cut > 0:
            snippet = snippet[:cut]
        else:
            snippet = ""
        marker = " [truncated]"
        if expendable.endswith(marker) and len(snippet) + len(marker) <= room:
            snippet += marker
        extras.append(snippet)
        room -= len(snippet)
    return assemble(fitted, extras)


def _permission_payload_summary(payload: dict[str, Any], scope: Any = None) -> str | None:
    """permission: <permission> <patterns>, or the scope line when scope is present."""
    if isinstance(scope, list) and scope:
        scoped = _format_permission_scope(payload, scope)
        if scoped:
            return scoped
    perm = _stripped(payload.get("permission"))
    patterns = payload.get("patterns")
    names: list[str] = []
    if isinstance(patterns, list):
        for item in patterns:
            text = _stripped(item)
            if text is not None:
                names.append(text)
    if perm is None and not names:
        return None
    body: list[str] = []
    if perm is not None:
        body.append(perm)
    if names:
        body.append(", ".join(names))
    return "permission: " + " ".join(body)


def _question_payload_summary(payload: dict[str, Any]) -> str | None:
    """question: first questions[0].question, else its header, else payload.question."""
    text: str | None = None
    questions = payload.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        if isinstance(first, dict):
            text = _stripped(first.get("question")) or _stripped(first.get("header"))
    if text is None:
        text = _stripped(payload.get("question"))
    if text is None:
        return None
    return "question: " + text


def _system_action_fields(obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    typ = _stripped(obj.get("type"))
    filename: str | None = None
    package = obj.get("package")
    if isinstance(package, dict):
        filename = _stripped(package.get("filename"))
    return typ, filename


def _system_action_payload_summary(payload: dict[str, Any]) -> str | None:
    """system_action: <type> <package.filename> from the payload or its proposal."""
    typ, filename = _system_action_fields(payload)
    ptyp, pfilename = _system_action_fields(payload.get("proposal"))
    bits = [bit for bit in (typ or ptyp, filename or pfilename) if bit]
    if not bits:
        return None
    return "system_action: " + " ".join(bits)


def _native_payload_summary(backend_kind: str, payload: dict[str, Any]) -> str | None:
    """One-line summary from a TeleAgent-native worker payload, or None.

    Odd shapes return None instead of raising so the caller keeps the
    previous candidate order.
    """
    builders = {
        "review": _review_payload_summary,
        "permission": _permission_payload_summary,
        "question": _question_payload_summary,
        "system_action": _system_action_payload_summary,
    }
    builder = builders.get(backend_kind)
    if builder is None:
        return None
    try:
        text = builder(payload)
    except (TypeError, ValueError, AttributeError, KeyError, IndexError):
        return None
    if _stripped(text) is None:
        return None
    return text


def _decision_summary(row: dict[str, Any]) -> str:
    """One-line summary, truncated to 200.

    Skip title when it is empty or equal to kind (escalate stores the kind
    slug in title). Then details.summary/message/reason/question. When
    details.backend_kind is permission/question/review/system_action and
    details.payload is a dict, a payload summary comes next and beats the
    generic title ``TeleAgent <kind>`` and the row reason. A review summary
    that starts with ``CONTAMINATED`` is returned before the title so a
    watermark is not hidden by ``TeleAgent review (CONTAMINATED)``. When
    ``details.scope`` is present, the permission scope line is returned next
    (already within 200 characters: the flag sits right after the pattern, and
    a too-long pattern is shortened in the middle so the flag, count and first
    file name survive). Then the
    row reason, lead_error.message, title, and finally kind.
    """
    title = row.get("title")
    kind = row.get("kind")
    candidates: list[Any] = []
    details = row.get("details")
    payload_summary: str | None = None
    generic_title = ""
    if isinstance(details, dict):
        backend_kind = details.get("backend_kind")
        payload = details.get("payload")
        if (
            isinstance(backend_kind, str)
            and backend_kind in _NATIVE_DECISION_KINDS
            and isinstance(payload, dict)
        ):
            payload_summary = _native_payload_summary(backend_kind, payload)
            if payload_summary:
                generic_title = f"TeleAgent {backend_kind}"
                if payload_summary.startswith("CONTAMINATED"):
                    return _one_line(payload_summary)
    if isinstance(details, dict) and isinstance(details.get("scope"), list) and details.get("scope"):
        scope_payload = payload if isinstance(payload, dict) else {}
        scope_line = _format_permission_scope(scope_payload, details.get("scope"))
        if scope_line and not (payload_summary or "").startswith("CONTAMINATED"):
            return scope_line
    title_text = _nonempty_str(title)
    if title_text is not None and title_text.strip() != str(kind or "").strip():
        if not generic_title or title_text.strip() != generic_title:
            candidates.append(title_text)
    if isinstance(details, dict):
        for key in ("summary", "message", "reason", "question"):
            candidates.append(details.get(key))
    if payload_summary:
        candidates.append(payload_summary)
    candidates.append(row.get("reason"))
    lead_error = row.get("lead_error")
    if isinstance(lead_error, dict):
        candidates.append(lead_error.get("message"))
    candidates.append(title)
    for value in candidates:
        text = _nonempty_str(value)
        if text is not None:
            return _one_line(text)
    return _one_line(str(kind or ""))


def _decision_brief(row: dict[str, Any]) -> dict[str, Any]:
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    brief = {
        "decision_id": _text(row.get("decision_id")),
        "kind": _text(row.get("kind")),
        "title": _text(row.get("title")),
        "task_id": _text(row.get("task_id")),
        "status": _text(row.get("status")),
        "reason": _text(row.get("reason")),
        "summary": _decision_summary(row),
    }
    if "awaiting" in row and row.get("awaiting") is not None:
        awaiting = str(row.get("awaiting"))
        if awaiting:
            brief["awaiting"] = awaiting
    return brief


def _decision_briefs(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [_decision_brief(row) for row in rows if isinstance(row, dict)]


def _partition_pending_rows(rows: Any) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Split pending rows into human vs lead.

    Returns ``(mode, human_rows, lead_ids)``:

    - ``all_lead``: at least one dict row, and every dict row has
      ``awaiting == "lead"`` (wait should keep polling).
    - ``rows``: human rows are everything that is not explicitly lead,
      including old-server rows that omit ``awaiting``. ``lead_ids`` are the
      lead-owned decision ids, in row order.
    - ``none``: no dict rows.
    """
    if not isinstance(rows, list):
        return "none", [], []
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return "none", [], []
    human: list[dict[str, Any]] = []
    lead_ids: list[str] = []
    for row in dict_rows:
        if row.get("awaiting") == "lead":
            lead_ids.append("" if row.get("decision_id") is None else str(row.get("decision_id")))
        else:
            human.append(row)
    if not human and lead_ids:
        return "all_lead", [], lead_ids
    return "rows", human, lead_ids


def _counts_say_lead_only(status: dict[str, Any]) -> bool:
    """True when the status says every pending decision is the lead's.

    Both counts must be present. A missing ``awaiting_human_count`` is an old
    server and must not be treated as zero.
    """
    if "awaiting_human_count" not in status or "awaiting_lead_count" not in status:
        return False
    try:
        human = int(status.get("awaiting_human_count"))
        lead = int(status.get("awaiting_lead_count"))
    except (TypeError, ValueError):
        return False
    return human == 0 and lead > 0


def need_human_view(status: dict[str, Any]) -> dict[str, Any] | None:
    """Human-decision snapshot, or None when wait should keep polling.

    Terminal states win: completed / failed / cancelled return None even if
    decision rows are still present. Otherwise the first matching signal is
    pending_decisions, then awaiting_decision / pending_decision_count, then
    a task whose status is awaiting_decision.

    Rows with ``awaiting == "lead"`` belong to the lead. If every pending row
    is lead-owned, return None so wait keeps polling. A mix returns only the
    human rows and ``lead_pending_ids`` for the rest. Rows without ``awaiting``
    (old server) stay human, same as before. When the status has no rows but
    ``awaiting_human_count == 0`` and ``awaiting_lead_count > 0``, keep polling.
    """
    if not isinstance(status, dict):
        return None
    state = str(status.get("state") or "")
    if state in TERMINAL_STATES:
        return None

    raw_pending = status.get("pending_decisions")
    pending_nonempty = isinstance(raw_pending, list) and len(raw_pending) > 0
    task_ids: list[str] | None = None
    lead_pending_ids: list[str] | None = None
    decision_source: Any = raw_pending
    if pending_nonempty:
        mode, human_rows, lead_ids = _partition_pending_rows(raw_pending)
        if mode == "all_lead":
            return None
        reason = "pending_decisions"
        if mode == "rows":
            decision_source = human_rows
            if lead_ids:
                lead_pending_ids = lead_ids
    else:
        try:
            count_n = int(status.get("pending_decision_count") or 0)
        except (TypeError, ValueError):
            count_n = 0
        if status.get("awaiting_decision") or count_n > 0:
            if _counts_say_lead_only(status):
                return None
            reason = "awaiting_decision"
        else:
            tasks = status.get("tasks")
            ids: list[str] = []
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict) and str(task.get("status") or "") == "awaiting_decision":
                        ids.append("" if task.get("task_id") is None else str(task.get("task_id")))
            if not ids:
                return None
            reason = "task_awaiting_decision"
            task_ids = ids

    decisions = _decision_briefs(decision_source)
    view: dict[str, Any] = {
        "reason": reason,
        "state": state,
        "decision_ids": [item["decision_id"] for item in decisions],
        "decisions": decisions,
    }
    if task_ids is not None:
        view["task_ids"] = task_ids
    if lead_pending_ids:
        view["lead_pending_ids"] = lead_pending_ids
    return view


def _format_seconds(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _wait_resume(request_id: str, timeout: float) -> str:
    return (
        "python bin/hermes-collab-request.py wait "
        f"{request_id} --timeout {_format_seconds(timeout)}"
    )


_WALL_TEXT_KEYS = (
    "error",
    "failure_reason",
    "reason",
    "message",
    "code",
    "error_class",
    "kind",
    "failure_code",
    "state",
)
# Phrases the server already writes for a goal wall-clock budget
# (win_collab.budget_exceeded_reason, charter timeout_sec → timed_out).
_WALL_MARKERS = (
    "budget_exceeded wall",
    "budget_exceeded: wall",
    "budget_exhausted",
    "timed_out",
    "wall clock",
    "wall_clock",
    "wall budget",
    "wall_budget",
    "wall_sec",
    "wall_s=",
    "deadline exhausted",
)


def _append_failure_text(chunks: list[str], obj: Any) -> None:
    if isinstance(obj, str):
        if obj.strip():
            chunks.append(obj)
        return
    if not isinstance(obj, dict):
        return
    for key in _WALL_TEXT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value)


def _goal_wall_budget(payload: dict[str, Any]) -> bool:
    """True when failure/error text is the goal's own wall budget, not a generic error.

    Looks only at failure and error fields the server already produces
    (budget_exceeded wall / wall clock / timed_out / budget_exhausted).
    A step budget (``budget_exceeded steps``) does not match.
    """
    chunks: list[str] = []
    _append_failure_text(chunks, payload)
    _append_failure_text(chunks, payload.get("failure"))
    report = payload.get("report") if isinstance(payload.get("report"), dict) else None
    if report is not None:
        _append_failure_text(chunks, report)
        _append_failure_text(chunks, report.get("failure"))
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) and report is not None:
        tasks = report.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            _append_failure_text(chunks, task)
            result = task.get("result")
            if isinstance(result, dict):
                _append_failure_text(chunks, result)
    blob = "\n".join(chunks).lower()
    if not blob:
        return False
    if any(marker in blob for marker in _WALL_MARKERS):
        return True
    if "budget_exceeded" in blob and "timeout" in blob:
        return True
    return False


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    rid = _encode_id(args.request_id)
    deadline = time.monotonic() + float(args.timeout)
    interval = max(0.2, float(args.interval))
    last: dict[str, Any] = {}
    while True:
        last = request_json("GET", f"/v1/requests/{rid}", timeout=float(args.http_timeout))
        state = str(last.get("state") or "")
        if state in TERMINAL_STATES:
            if state in SUCCESS_STATES:
                kind = "completed"
            elif state == "cancelled":
                kind = "task_cancelled"
            else:
                kind = "task_failed"
            terminal_wait: dict[str, Any] = {"terminal": True, "state": state, "kind": kind}
            if state not in SUCCESS_STATES and _goal_wall_budget(last):
                terminal_wait["task_timeout"] = True
            # A worker result whose error is exactly "timeout" is not a wall-budget
            # phrase. The server labels that primary_failure.source worker_timeout.
            if kind == "task_failed" and _primary_failure_source(last) == "worker_timeout":
                terminal_wait["task_timeout"] = True
            last["wait"] = terminal_wait
            if state not in SUCCESS_STATES:
                raise ClientError(last, exit_code=EXIT_FAILED)
            return last
        view = need_human_view(last)
        if view is not None:
            decisions = list(view.get("decisions") or [])
            decision_ids = list(view.get("decision_ids") or [])
            reason = str(view.get("reason") or "")
            lead_pending_ids = list(view.get("lead_pending_ids") or [])
            fetched_all_lead = False
            # Status can flag awaiting_decision before rows are copied onto it.
            if not decisions and reason != "pending_decisions":
                try:
                    extra = request_json(
                        "GET",
                        f"/v1/requests/{rid}/decisions",
                        timeout=float(args.http_timeout),
                    )
                except ClientError:
                    extra = None
                if isinstance(extra, dict):
                    mode, human_rows, extra_lead = _partition_pending_rows(
                        extra.get("pending_decisions")
                    )
                    if mode == "all_lead":
                        # The list endpoint caught up: still the lead's. Keep polling.
                        fetched_all_lead = True
                    else:
                        source = human_rows if mode == "rows" else extra.get("pending_decisions")
                        filled = _decision_briefs(source)
                        if filled:
                            decisions = filled
                            decision_ids = [item["decision_id"] for item in filled]
                        if extra_lead:
                            lead_pending_ids = extra_lead
            if not fetched_all_lead:
                last["code"] = "need_human"
                last["need_human"] = True
                wait_info: dict[str, Any] = {
                    "terminal": False,
                    "need_human": True,
                    "timed_out": False,
                    "kind": "need_human",
                    "reason": reason,
                    "state": view.get("state", state),
                    "decision_ids": decision_ids,
                    "decisions": decisions,
                }
                if "task_ids" in view:
                    wait_info["task_ids"] = list(view["task_ids"])
                if lead_pending_ids:
                    wait_info["lead_pending_ids"] = lead_pending_ids
                last["wait"] = wait_info
                raise ClientError(last, exit_code=EXIT_NEED_HUMAN)
        if time.monotonic() >= deadline:
            last["wait"] = {
                "terminal": False,
                "timed_out": True,
                "state": state,
                "timeout_sec": float(args.timeout),
                "kind": "observation_timeout",
                "task_still_running": state not in TERMINAL_STATES,
                "resume": _wait_resume(str(args.request_id), float(args.timeout)),
                "note": _OBSERVATION_NOTE,
            }
            raise ClientError(last, exit_code=EXIT_TIMEOUT)
        time.sleep(interval)


class _Cut:
    """Track whether a free-text field was shortened to the summary cap."""

    def __init__(self) -> None:
        self.hit = False

    def text(self, value: Any, limit: int = _SUMMARY_TEXT_CAP) -> str:
        if value is None:
            return ""
        text = str(value)
        if limit < 0:
            limit = 0
        if len(text) <= limit:
            return text
        self.hit = True
        return text[:limit]


def _layers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    layers = [payload]
    report = payload.get("report")
    if isinstance(report, dict):
        layers.append(report)
    return layers


def _pick(payload: dict[str, Any], key: str) -> Any:
    for layer in _layers(payload):
        if key in layer and layer[key] is not None:
            return layer[key]
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None
    return value


def _task_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _pick(payload, "tasks")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _pending_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _pick(payload, "pending_decisions")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _artifact_size(meta: dict[str, Any]) -> int | None:
    for key in ("size", "bytes", "nbytes"):
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _remember_artifact(
    files: list[dict[str, Any]],
    index: dict[tuple[str, str], dict[str, Any]],
    task_id: str,
    path: str,
    size: int | None,
) -> None:
    if not path:
        return
    key = (task_id, path)
    entry = index.get(key)
    if entry is None:
        entry = {"task_id": task_id, "path": path}
        index[key] = entry
        files.append(entry)
    if size is not None and "size" not in entry:
        entry["size"] = size


def _consume_artifacts(
    value: Any,
    task_id: str,
    names: list[str],
    seen_names: set[str],
    files: list[dict[str, Any]],
    index: dict[tuple[str, str], dict[str, Any]],
    cut: _Cut,
) -> None:
    """Collect artifact names and {task_id, path, size?}. Never copy previews or bytes content."""

    def add_name(label: str) -> str:
        shown = cut.text(label.strip(), _SUMMARY_TEXT_CAP)
        if shown and shown not in seen_names:
            seen_names.add(shown)
            names.append(shown)
        return shown

    if isinstance(value, str):
        path = add_name(value)
        _remember_artifact(files, index, task_id, path, None)
        return
    if isinstance(value, list):
        for item in value:
            _consume_artifacts(item, task_id, names, seen_names, files, index, cut)
        return
    if not isinstance(value, dict):
        return
    if any(key in value for key in ("path", "name", "file")):
        label = value.get("path") or value.get("name") or value.get("file")
        if isinstance(label, str):
            path = add_name(label)
            _remember_artifact(files, index, task_id, path, _artifact_size(value))
        return
    for name, meta in value.items():
        if not isinstance(name, str):
            continue
        path = add_name(name)
        size = _artifact_size(meta) if isinstance(meta, dict) else None
        _remember_artifact(files, index, task_id, path, size)


def _task_workspace(task: dict[str, Any], result: dict[str, Any] | None) -> str:
    sources: list[dict[str, Any]] = [task]
    if isinstance(result, dict):
        sources.append(result)
    for source in sources:
        for key in ("workspace", "workdir", "workspace_path", "workspace_dir"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _task_error(task: dict[str, Any], result: dict[str, Any] | None) -> str:
    candidates: list[Any] = []
    if isinstance(result, dict):
        candidates.append(result.get("error"))
    candidates.append(task.get("error"))
    if isinstance(result, dict):
        candidates.append(result.get("failure_reason"))
    candidates.append(task.get("failure_reason"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _cap_usage(usage: Any, cut: _Cut) -> Any:
    """Copy worker-reported usage. Do not add numeric fields together."""
    if isinstance(usage, str):
        return cut.text(usage, _SUMMARY_TEXT_CAP)
    if isinstance(usage, dict):
        copied: dict[str, Any] = {}
        for key, value in usage.items():
            if isinstance(value, str):
                copied[str(key)] = cut.text(value, _SUMMARY_TEXT_CAP)
            elif isinstance(value, (dict, list)):
                copied[str(key)] = _cap_usage(value, cut)
            else:
                copied[str(key)] = value
        return copied
    if isinstance(usage, list):
        return [
            _cap_usage(item, cut) if isinstance(item, (dict, list, str)) else item
            for item in usage
        ]
    return usage


def _usage_summary(tasks: list[dict[str, Any]], cut: _Cut) -> dict[str, Any]:
    found: list[tuple[str, Any]] = []
    for task in tasks:
        result = task.get("result")
        if not isinstance(result, dict) or "usage" not in result:
            continue
        usage = result.get("usage")
        if usage is None:
            continue
        found.append((str(task.get("task_id") or ""), _cap_usage(usage, cut)))
    if not found:
        return {"source": "unknown"}
    if len(found) == 1:
        return {"source": "worker_self_reported", "values": found[0][1]}
    values: dict[str, Any] = {}
    for index, (task_id, usage) in enumerate(found):
        key = task_id or f"task_{index}"
        if key in values:
            key = f"{key}#{index}"
        values[key] = usage
    return {"source": "worker_self_reported", "values": values}


def _progress_summary(payload: dict[str, Any], cut: _Cut) -> dict[str, Any]:
    """Pass through real progress fields. Never invent a percentage."""
    raw = _pick(payload, "progress")
    if not isinstance(raw, dict):
        return {"available": False, "phase": "unknown"}
    present: dict[str, Any] = {}
    for key in _PROGRESS_KEYS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if isinstance(value, str):
            value = cut.text(value, _SUMMARY_TEXT_CAP)
        present[key] = value
    if not present:
        return {"available": False, "phase": "unknown"}
    phase = present.get("phase")
    if not isinstance(phase, str) or not phase.strip():
        phase = "unknown"
    view: dict[str, Any] = {"available": True, "phase": phase}
    for key, value in present.items():
        if key == "phase":
            continue
        view[key] = value
    return view


def _task_review(task: dict[str, Any], result: dict[str, Any] | None, cut: _Cut) -> dict[str, str]:
    """Review object from the task result. Missing means it was not requested."""
    raw: Any = None
    if isinstance(result, dict) and isinstance(result.get("review"), dict):
        raw = result.get("review")
    elif isinstance(task.get("review"), dict):
        raw = task.get("review")
    if not isinstance(raw, dict):
        raw = {"status": "not_requested", "source": "none", "evidence": ""}
    return {
        "status": cut.text(raw.get("status") or "not_requested", _SUMMARY_TEXT_CAP),
        "source": cut.text(raw.get("source") or "none", _SUMMARY_TEXT_CAP),
        "evidence": cut.text(raw.get("evidence") or "", _SUMMARY_TEXT_CAP),
    }


def _warning_items(value: Any, cut: _Cut) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [cut.text(text, _SUMMARY_TEXT_CAP)] if text else []
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                items.append(cut.text(text, _SUMMARY_TEXT_CAP))
            continue
        if isinstance(item, dict):
            picked = ""
            for key in ("message", "warning", "text", "detail"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    picked = candidate.strip()
                    break
            if not picked:
                picked = json.dumps(item, ensure_ascii=True, default=str)
            items.append(cut.text(picked, _SUMMARY_TEXT_CAP))
    return items


def _need_human_flag(payload: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
    for layer in _layers(payload):
        if isinstance(layer.get("need_human"), bool):
            return layer["need_human"]
    failure = _pick(payload, "failure")
    if isinstance(failure, dict) and failure.get("need_human") is True:
        return True
    for task in tasks:
        if task.get("need_human") is True:
            return True
        result = task.get("result")
        if isinstance(result, dict) and result.get("need_human") is True:
            return True
    return False


def _primary_failure_source(payload: dict[str, Any]) -> str:
    """Server source label, if a primary_failure brief is present."""
    for layer in _layers(payload):
        primary = layer.get("primary_failure")
        if not isinstance(primary, dict):
            continue
        source = primary.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return ""


def _primary_failure_summary(raw: Any, cut: _Cut) -> dict[str, Any] | None:
    """Copy the server brief. Drop stdout and any other extra keys."""
    if not isinstance(raw, dict):
        return None
    missing_raw = raw.get("missing_artifacts")
    missing: list[str] = []
    if isinstance(missing_raw, list):
        for item in missing_raw:
            if isinstance(item, str) and item.strip():
                missing.append(cut.text(item.strip(), _SUMMARY_TEXT_CAP))
    return {
        "task_id": cut.text("" if raw.get("task_id") is None else raw.get("task_id"), _SUMMARY_TEXT_CAP),
        "run_id": cut.text("" if raw.get("run_id") is None else raw.get("run_id"), _SUMMARY_TEXT_CAP),
        "title": cut.text(raw.get("title") or "", _SUMMARY_TEXT_CAP),
        "error": cut.text(raw.get("error") or "", _SUMMARY_TEXT_CAP),
        "source": cut.text(raw.get("source") or "", _SUMMARY_TEXT_CAP),
        "missing_artifacts": missing,
        "retryable": _as_bool(raw.get("retryable"), default=False),
        "next_step": cut.text(raw.get("next_step") or "", _SUMMARY_TEXT_CAP),
    }


def _failure_count(payload: dict[str, Any], primary: dict[str, Any] | None) -> int:
    raw = _pick(payload, "failures")
    if isinstance(raw, list):
        return sum(1 for item in raw if isinstance(item, dict))
    if primary is not None:
        return 1
    return 0


def _failure_fields(payload: dict[str, Any], cut: _Cut) -> tuple[str, str]:
    reason = ""
    code = ""
    picked_reason = _pick(payload, "failure_reason")
    if isinstance(picked_reason, str) and picked_reason.strip():
        reason = picked_reason
    picked_code = _pick(payload, "failure_code")
    if not picked_code:
        picked_code = _pick(payload, "error_class")
    if isinstance(picked_code, str) and picked_code.strip():
        code = picked_code.strip()
    failure = _pick(payload, "failure")
    if isinstance(failure, str) and failure.strip() and not reason:
        reason = failure
    elif isinstance(failure, dict):
        if not reason:
            for key in ("failure_reason", "error", "message", "reason"):
                value = failure.get(key)
                if isinstance(value, str) and value.strip():
                    reason = value
                    break
        if not code:
            for key in ("failure_code", "code", "error_class", "kind"):
                value = failure.get(key)
                if isinstance(value, str) and value.strip():
                    code = value.strip()
                    break
    return cut.text(reason, _SUMMARY_TEXT_CAP), cut.text(code, _SUMMARY_TEXT_CAP)


def _awaiting_counts(
    payload: dict[str, Any], rows: list[dict[str, Any]]
) -> tuple[int, int]:
    lead = _as_int(_pick(payload, "awaiting_lead_count"))
    human = _as_int(_pick(payload, "awaiting_human_count"))
    if lead is not None and human is not None:
        return lead, human
    derived_lead = 0
    derived_human = 0
    for row in rows:
        if row.get("awaiting") == "lead":
            derived_lead += 1
        else:
            derived_human += 1
    return (
        lead if lead is not None else derived_lead,
        human if human is not None else derived_human,
    )


def summarize_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Stable concise view of a status, report, or wait payload.

    Omits the goal contract, worker response text, and stdout/stderr.
    Free-text fields are capped; ``truncated`` is true when any cap fired.
    ``progress.percent`` is included only when the server sent it.
    ``usage`` is copied from task results and is never summed.
    """
    cut = _Cut()
    tasks = _task_rows(payload)
    rows = _pending_rows(payload)
    state = ""
    picked_state = _pick(payload, "state")
    if picked_state is not None:
        state = str(picked_state)
    request_id = _pick(payload, "request_id")
    if request_id is None:
        request_id = _pick(payload, "goal_id")
    request_id_text = "" if request_id is None else str(request_id)
    ok_value = _pick(payload, "ok")
    reason, failure_code = _failure_fields(payload, cut)
    primary_failure = _primary_failure_summary(_pick(payload, "primary_failure"), cut)
    # Top-level failure_reason wins. Fall back to the primary task error.
    if not reason and primary_failure is not None:
        reason = primary_failure.get("error") or ""
    failure_count = _failure_count(payload, primary_failure)
    briefs: list[dict[str, Any]] = []
    for row in rows:
        brief = _decision_brief(row)
        for key in ("title", "reason", "summary", "kind", "decision_id", "awaiting", "status", "task_id"):
            if key in brief and isinstance(brief[key], str):
                brief[key] = cut.text(brief[key], _SUMMARY_TEXT_CAP)
        briefs.append(brief)
    lead_count, human_count = _awaiting_counts(payload, rows)
    task_views: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for task in tasks:
        task_id = "" if task.get("task_id") is None else str(task.get("task_id"))
        result = task.get("result") if isinstance(task.get("result"), dict) else None
        names: list[str] = []
        seen_names: set[str] = set()
        for key in ("expected_artifacts", "artifacts"):
            _consume_artifacts(task.get(key), task_id, names, seen_names, artifacts, index, cut)
        done = task.get("done_when")
        if isinstance(done, dict):
            _consume_artifacts(
                done.get("artifacts"), task_id, names, seen_names, artifacts, index, cut
            )
        if isinstance(result, dict):
            _consume_artifacts(
                result.get("artifacts"), task_id, names, seen_names, artifacts, index, cut
            )
        task_views.append(
            {
                "task_id": task_id,
                "title": cut.text(task.get("title") or "", _SUMMARY_TEXT_CAP),
                "status": "" if task.get("status") is None else str(task.get("status")),
                "artifacts": names,
                "workspace": cut.text(_task_workspace(task, result), _SUMMARY_TEXT_CAP),
                "error": cut.text(_task_error(task, result), _SUMMARY_TEXT_CAP),
                "review": _task_review(task, result, cut),
            }
        )
    summary: dict[str, Any] = {
        "ok": True if ok_value is None else _as_bool(ok_value),
        "request_id": request_id_text,
        "state": state,
        "terminal": state in TERMINAL_STATES,
        "need_human": _need_human_flag(payload, tasks),
        "failure_reason": reason,
        "primary_failure": primary_failure,
        "failure_count": failure_count,
        "pending_decisions": briefs,
        "awaiting_lead_count": lead_count,
        "awaiting_human_count": human_count,
        "tasks": task_views,
        "artifacts": artifacts,
        "progress": _progress_summary(payload, cut),
        "usage": _usage_summary(tasks, cut),
    }
    if failure_code:
        # Insert beside failure_reason without inventing an empty code.
        ordered = {
            "ok": summary["ok"],
            "request_id": summary["request_id"],
            "state": summary["state"],
            "terminal": summary["terminal"],
            "need_human": summary["need_human"],
            "failure_reason": summary["failure_reason"],
            "failure_code": failure_code,
            "primary_failure": summary["primary_failure"],
            "failure_count": summary["failure_count"],
        }
        for key, value in summary.items():
            if key not in ordered:
                ordered[key] = value
        summary = ordered
    code = payload.get("code")
    if isinstance(code, str) and code.strip():
        summary["code"] = cut.text(code.strip(), _SUMMARY_TEXT_CAP)
    warning_found = False
    warnings: list[str] = []
    for layer in _layers(payload):
        for key in ("warnings", "capability_warnings"):
            if key in layer and layer[key] is not None:
                warning_found = True
                warnings.extend(_warning_items(layer[key], cut))
    if warning_found:
        summary["warnings"] = warnings
    http_status = payload.get("http_status")
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        summary["http_status"] = http_status
    summary["truncated"] = cut.hit
    summary["full_hint"] = _FULL_HINT
    wait = payload.get("wait")
    scheduler = _pick(payload, "scheduler")
    if isinstance(scheduler, dict):
        running = _as_int(scheduler.get("running"))
        queued_ready = _as_int(scheduler.get("queued_ready"))
        capacity = _as_int(scheduler.get("capacity"))
        reason = scheduler.get("waiting_reason")
        summary["scheduler"] = {
            "running": 0 if running is None else running,
            "queued_ready": 0 if queued_ready is None else queued_ready,
            "capacity": 0 if capacity is None else capacity,
            "waiting_reason": cut.text("" if reason is None else str(reason), _SUMMARY_TEXT_CAP),
        }
    if isinstance(wait, dict):
        summary["wait"] = wait
    return summary


def cmd_ping(args: argparse.Namespace) -> dict[str, Any]:
    """GET /health, then authenticated GET /v1/capabilities when that route exists."""
    health = request_json("GET", "/health", timeout=float(args.http_timeout))
    out: dict[str, Any] = {
        "ok": health.get("ok") is not False,
        "api_version": health.get("api_version"),
        "base": _base_url(),
    }
    try:
        caps = request_json("GET", "/v1/capabilities", timeout=float(args.http_timeout))
    except ClientError as exc:
        status = exc.payload.get("http_status")
        try:
            missing = int(status) == 404
        except (TypeError, ValueError):
            missing = False
        if not missing:
            raise
        out["capabilities"] = None
    else:
        out["capabilities"] = caps
    return out


def cmd_pending(args: argparse.Namespace) -> dict[str, Any]:
    """List non-terminal requests so a new Hermes turn can resume the same id."""
    payload = request_json("GET", "/v1/requests", timeout=float(args.http_timeout))
    rows = payload.get("requests")
    if not isinstance(rows, list):
        rows = payload.get("goals") if isinstance(payload.get("goals"), list) else []
    listed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "")
        if state in TERMINAL_STATES:
            continue
        rid = row.get("request_id")
        if rid is None:
            rid = row.get("goal_id")
        if isinstance(row.get("updated_at_iso"), str) and row.get("updated_at_iso"):
            updated: Any = row.get("updated_at_iso")
        else:
            updated = row.get("updated_at")
        if "awaiting_decision" in row:
            awaiting = bool(row.get("awaiting_decision"))
        else:
            count = row.get("pending_decision_count")
            if count is None:
                count = row.get("pending_count") or 0
            parsed = _as_int(count) if not isinstance(count, bool) else None
            if parsed is None:
                try:
                    parsed = int(count)
                except (TypeError, ValueError):
                    parsed = 1 if count else 0
            awaiting = parsed > 0
        listed.append(
            {
                "request_id": "" if rid is None else str(rid),
                "state": state,
                "awaiting_decision": awaiting,
                "updated_at": updated,
            }
        )
    result: dict[str, Any] = {"ok": True if payload.get("ok") is not False else False, "requests": listed}
    http_status = payload.get("http_status")
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        result["http_status"] = http_status
    return result


def _output_full_enabled(flag: bool = False) -> bool:
    """True for --full or COLLAB_OUTPUT_FULL=1|true|yes|on."""
    if flag:
        return True
    raw = (os.environ.get("COLLAB_OUTPUT_FULL") or "").strip().lower()
    return raw in _JSON_UNICODE_ON


def _present_output(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    """Summary for status/report/wait. Bare transport errors and --full stay raw."""
    if getattr(args, "cmd", None) not in {"status", "report", "wait"}:
        return payload
    if _output_full_enabled(bool(getattr(args, "full", False))):
        return payload
    if not isinstance(payload, dict):
        return payload
    if not any(key in payload for key in ("state", "wait", "tasks", "report")):
        return payload
    return summarize_status(payload)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-collab-request.py",
        description=(
            "Minimal collab Application API client (Hermes-friendly). "
            "COLLAB_API_BASE and COLLAB_API_TOKEN: first non-empty of process "
            "environment, COLLAB_ENV_FILE, HERMES_HOME/.env, then "
            "%LOCALAPPDATA%\\hermes\\.env on Windows or ~/.hermes/.env "
            "elsewhere. Only those two keys are read from dotenv. "
            "Stdout is one ASCII JSON line by default (non-ASCII as \\uXXXX); "
            "--unicode emits raw UTF-8. status/report/wait print a summary; "
            "--full or COLLAB_OUTPUT_FULL=1 prints the raw payload."
        ),
    )
    p.add_argument(
        "--http-timeout",
        type=float,
        default=30.0,
        help="per-request HTTP timeout seconds (default 30)",
    )
    p.add_argument(
        "--unicode",
        action="store_true",
        help=(
            "emit UTF-8 JSON with raw non-ASCII characters "
            "(default: one-line ASCII JSON, non-ASCII as \\uXXXX). "
            "Also enabled by COLLAB_JSON_UNICODE=1|true|yes|on. "
            "Reconfigures stdout to utf-8 when supported."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="POST /v1/requests")
    p_open.add_argument("--goal", required=True, help="desired outcome text")
    p_open.add_argument("--title", default="", help="short title (default: goal prefix)")
    p_open.add_argument("--client-id", default="hermes", help="client_id (default hermes)")
    p_open.add_argument("--idempotency-key", default="", help="optional idempotency key")
    p_open.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="acceptance artifact relative path (repeatable; default delivery.md)",
    )
    p_open.add_argument("--acceptance-text", default="", help="acceptance.text override")
    p_open.add_argument("--must", action="append", default=[], help="boundaries.must (repeatable)")
    p_open.add_argument(
        "--must-not",
        action="append",
        default=[],
        help="boundaries.must_not (repeatable)",
    )
    p_open.add_argument("--wall-sec", type=int, default=300, help="budget.wall_sec")
    p_open.add_argument("--max-reworks", type=int, default=1, help="budget.max_reworks")
    p_open.add_argument(
        "--backend",
        choices=("teleagent-windows", "antigravity", "agy", "inprocess"),
        default="",
        help=(
            "optional caller annotation only; worker backend is chosen at "
            "collab-service start (teleagent-windows | antigravity | …)"
        ),
    )
    p_open.add_argument(
        "--external-input",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "pin a file the worker may read outside the task workspace "
            "(repeatable, at most 8 combined with --sqlite-snapshot and "
            "--file-snapshot; sends absolute path and sha256). "
            "More than 8 exits 1 with code too_many_external_inputs "
            "before any file is opened. "
            "A missing file exits 1 with code bad_external_input. "
            "A SQLite file with a non-empty sibling -wal exits 1 with "
            "code sqlite_live_wal (use --sqlite-snapshot). "
            "Pinning a -wal or -shm file exits 1 with code sqlite_sidecar_refused"
        ),
    )
    p_open.add_argument(
        "--input-manifest",
        default="",
        metavar="FILE",
        help=(
            "JSON manifest {root, include, max_files, max_total_bytes}. "
            "Single-level globs unless recursive is true. Hard cap 256 files "
            "and 256 MiB. Count and size are checked before hashing. "
            "Sends input_manifest entries (relative, sha256, size)"
        ),
    )
    p_open.add_argument(
        "--sqlite-snapshot",
        action="append",
        default=[],
        metavar="DB",
        help=(
            "consistent read-only backup of a SQLite database "
            "(repeatable; counts toward the 8 external input cap). "
            "Pins the snapshot file only, never -wal or -shm"
        ),
    )
    p_open.add_argument(
        "--file-snapshot",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "copy the byte prefix measured at open "
            "(repeatable; counts toward the 8 external input cap). "
            ".jsonl copies drop a trailing partial line"
        ),
    )
    p_open.add_argument(
        "--require-capability",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "capability this request requires (repeatable). "
            "Unmet names: HTTP 409 capability_unavailable, stdout includes missing, exit 1. "
            "The server does not create a goal."
        ),
    )
    p_open.add_argument(
        "--ack-prompt-only-inputs",
        action="store_true",
        help=(
            "operator acknowledges pinned external inputs are prompt-only "
            "on a skip-permissions backend (acknowledge_prompt_only_inputs true). "
            "Does not satisfy an explicit --require-capability."
        ),
    )
    p_open.set_defaults(func=cmd_open)

    p_ping = sub.add_parser(
        "ping",
        help="GET /health; include GET /v1/capabilities when that route exists",
    )
    p_ping.set_defaults(func=cmd_ping)

    def _add_full(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--full",
            action="store_true",
            help="print the raw JSON payload (also COLLAB_OUTPUT_FULL=1|true|yes|on)",
        )

    p_st = sub.add_parser("status", help="GET /v1/requests/{id}")
    p_st.add_argument("request_id", help="request_id / goal_id")
    _add_full(p_st)
    p_st.set_defaults(func=cmd_status)

    p_rep = sub.add_parser("report", help="GET /v1/requests/{id}/report")
    p_rep.add_argument("request_id", help="request_id / goal_id")
    _add_full(p_rep)
    p_rep.set_defaults(func=cmd_report)

    p_wait = sub.add_parser(
        "wait",
        help="poll status until terminal, human decision, or timeout",
    )
    p_wait.add_argument("request_id", help="request_id / goal_id")
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help=(
            "client observation window in seconds (default 600). "
            "Expiry exits 3 with wait.kind=observation_timeout and is not a task failure. "
            "A 300s slice stays under a ~420s host tool limit."
        ),
    )
    p_wait.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="poll interval seconds (default 2)",
    )
    _add_full(p_wait)
    p_wait.set_defaults(func=cmd_wait)

    p_pending = sub.add_parser(
        "pending",
        help="GET /v1/requests and list non-terminal requests",
    )
    p_pending.set_defaults(func=cmd_pending)

    p_dec = sub.add_parser(
        "decide",
        help="POST /v1/requests/{id}/decisions/{decision_id} (human verdict; HTTP 409 exits 5)",
    )
    p_dec.add_argument("request_id", help="request_id / goal_id")
    p_dec.add_argument("decision_id", help="decision_id")
    p_dec.add_argument("--verdict", required=True, help="verdict to submit, exactly as given")
    p_dec.add_argument("--reason", default="", help="optional reason")
    p_dec.add_argument(
        "--answers",
        default="",
        help="optional JSON array of answers (question decisions)",
    )
    p_dec.set_defaults(func=cmd_decide)

    p_clean = sub.add_parser(
        "snapshots-clean",
        help="delete client snapshot directories older than N hours (snapshot root only)",
    )
    p_clean.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="delete snapshot dirs older than this many hours (default 24)",
    )
    p_clean.set_defaults(func=cmd_snapshots_clean)

    return p


def main(argv: list[str] | None = None) -> int:
    # Do not change encoding here. backslashreplace only stops
    # UnicodeEncodeError when the console code page cannot hold a character.
    _reconfigure(sys.stdout, errors="backslashreplace")
    _reconfigure(sys.stderr, errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    as_unicode = _json_unicode_enabled(bool(getattr(args, "unicode", False)))
    try:
        payload = args.func(args)
    except ClientError as e:
        _emit(_present_output(args, e.payload), unicode=as_unicode)
        return int(e.exit_code)
    except KeyboardInterrupt:
        _emit(
            {"ok": False, "code": "interrupted", "error": "KeyboardInterrupt"},
            unicode=as_unicode,
        )
        return 130
    _emit(_present_output(args, payload), unicode=as_unicode)
    if isinstance(payload, dict) and payload.get("ok") is False:
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
