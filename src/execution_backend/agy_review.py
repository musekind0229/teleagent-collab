"""Pure helpers for agy lead review. No process or store I/O.

Artifact reads are the only filesystem access. Verdict strings, rework prompts,
and collect-result projection live here so the backend hook stays a state machine.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from execution_backend.agy_session_attribution import session_base

_EXACT_CONTENT_RE = re.compile(
    r"(?P<path>\S+)\s+must\s+contain\s+exactly\s+(?P<body>.+)$",
    re.IGNORECASE,
)
_REQUEST_RE = re.compile(r"^agyrev:(agy_[0-9a-f]{12}):r([1-9][0-9]*)$")
_UNAVAILABLE_CODE_RE = re.compile(r"lead_review_unavailable:\s*([A-Za-z0-9_.-]+)")

_PASS = frozenset({"pass", "approve", "allow", "once"})
_FAIL = frozenset({"fail", "reject", "deny", "deny_job"})

_PREVIEW_PER_FILE = 8000
_PREVIEW_TOTAL = 24000
_PREVIEW_FILES = 8
_PREVIEW_BYTES_CAP = 512 * 1024

NEXT_STEP = (
    "check the lead CLI (login, quota, --lead-timeout) with collab-service --ready, "
    "then open a NEW request citing this request_id"
)
INTERRUPTED_NEXT = "open a NEW request citing this request_id"
_REWORK_TAIL = (
    "Modify the existing files in the current working directory so the acceptance is "
    "met, then stop. Do not wait for further input."
)
TOOL_EVIDENCE_UNAVAILABLE_NOTE = "agy provides no tool evidence for this run; judge by artifacts and output. Do not reject merely because tool evidence is empty."
TOOL_EVIDENCE_SOURCE_NOTE = "tools are reconstructed from the agy session transcript (last 30 calls, outputs truncated)."
_CONV_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_EXIT_CODE_RE = re.compile(r"exited with code (\d+)")
_SECRET_RE = re.compile(
    r"Bearer\s+\S+|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+|sk-[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)
# Wholesale replacement when a key contains one of these words (any case, _ or - optional).
_SENSITIVE_KEY_RE = re.compile(
    r"api[_-]?key|password|passwd|private[_-]?key|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|authorization|token|cookie|"
    r"credentials?|secret|auth",
    re.IGNORECASE,
)
_REDACTED = "[redacted]"
_ARGS_CAP_MARK = "[capped]"
_ARGS_MAX_DEPTH = 12
_ARGS_MAX_ITEMS = 64
_ARGS_MAX_NODES = 256
_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024


def review_required(charter: dict | None, *, enabled: bool) -> bool:
    """Enabled, charter asks for a lead, and acceptance is not exact-content."""
    if not enabled:
        return False
    from execution_backend.antigravity_cli_v1 import _charter_requests_lead_review

    if not _charter_requests_lead_review(charter if isinstance(charter, dict) else None):
        return False
    if isinstance(charter, dict):
        acc = charter.get("acceptance")
        if isinstance(acc, str) and _EXACT_CONTENT_RE.match(acc.strip()):
            return False
    return True


def max_redos_from_charter(charter: dict | None) -> int:
    """``clamp(int(max_redos), 0, 3)``. Missing, None, or invalid → 1."""
    if not isinstance(charter, dict) or "max_redos" not in charter:
        return 1
    raw = charter.get("max_redos")
    if raw is None or isinstance(raw, bool):
        return 1
    try:
        number = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return 1
    if number < 0:
        return 0
    if number > 3:
        return 3
    return number


def parse_request_id(request_id: str) -> tuple[str, int] | None:
    if not isinstance(request_id, str):
        return None
    match = _REQUEST_RE.match(request_id)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def snapshot_artifacts(root: Path, artifacts: list[str]) -> dict[str, Any]:
    """Hash and bounded text preview of the named artifacts.

    Preview is utf-8 (errors replaced), at most 8000 chars per file, 24000 total,
    and 8 files. A file over 512 KiB contributes sha256 and size only.
    ``artifact_hash`` is sha256 over sorted ``name:sha256`` lines of files that
    were read. A file that exists but cannot be read is listed in ``unreadable``
    and is not hashed as empty.
    """
    base = Path(root)
    found: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    unreadable: list[str] = []
    preview_chars = 0
    preview_files = 0
    for raw_name in artifacts or []:
        name = str(raw_name)
        path = Path(name)
        if not path.is_absolute():
            path = base / name
        try:
            if not path.is_file():
                missing.append(name)
                continue
            size = path.stat().st_size
        except OSError:
            missing.append(name)
            continue
        try:
            digest, nbytes, preview, truncated = _hash_and_preview(path, size)
        except OSError:
            unreadable.append(name)
            continue
        if preview and preview_files < _PREVIEW_FILES and preview_chars < _PREVIEW_TOTAL:
            room = _PREVIEW_TOTAL - preview_chars
            if len(preview) > room:
                preview = preview[:room]
                truncated = True
            preview_chars += len(preview)
            preview_files += 1
        elif preview:
            preview = ""
            truncated = True
        found[name] = {
            "sha256": digest,
            "bytes": nbytes,
            "preview": preview,
            "truncated": truncated,
            "contamination": None,
        }
    lines = [f"{name}:{found[name]['sha256']}" for name in sorted(found)]
    artifact_hash = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {
        "artifacts": found,
        "artifact_hash": artifact_hash,
        "missing": missing,
        "unreadable": unreadable,
    }


def _hash_and_preview(path: Path, size: int) -> tuple[str, int, str, bool]:
    """Hash ``path``. ``OSError`` propagates so the caller can fail closed."""
    if size > _PREVIEW_BYTES_CAP:
        return _hash_over_cap(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    truncated = False
    if len(text) > _PREVIEW_PER_FILE:
        text = text[:_PREVIEW_PER_FILE]
        truncated = True
    return hashlib.sha256(raw).hexdigest(), len(raw), text, truncated


def _hash_over_cap(path: Path) -> tuple[str, int, str, bool]:
    """Stream sha256 for a file over the preview cap. ``OSError`` propagates."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total, "", True


def _unreadable_names(snap: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(snap, Mapping):
        return []
    raw = snap.get("unreadable")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        text = item if isinstance(item, str) else ("" if item is None else str(item))
        if text:
            names.append(text)
    return names


def _unreadable_reason(names: list[str]) -> str:
    joined = ", ".join(names)
    if joined:
        return f"lead_review_unavailable: unreadable artifacts: {joined}"
    return "lead_review_unavailable: unreadable artifacts"


def _mark_unreadable_payload(payload: dict[str, Any], snap: Mapping[str, Any] | None) -> list[str]:
    """Copy the snapshot's unreadable list onto the lead-review payload."""
    names = _unreadable_names(snap)
    payload["unreadable"] = names
    if names:
        payload["artifact_error"] = "unreadable artifacts: " + ", ".join(names)
    return names


def read_tool_evidence(
    environ: Mapping[str, Any] | None,
    conversation_id: str,
    *,
    max_calls: int = 30,
    output_cap: int = 2000,
    input_cap: int = 1000,
) -> list[dict[str, Any]] | None:
    """Last tool rows from transcript_full.jsonl, or None if that log is unavailable. Never raises."""
    try:
        if (
            not isinstance(conversation_id, str)
            or conversation_id in {".", ".."}
            or _CONV_ID_RE.fullmatch(conversation_id) is None
        ):
            return None
        base = session_base(environ)
        if base is None:
            return None
        path = base / "brain" / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"
        if not path.is_file() or path.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return None
        raw = path.read_bytes()
        if len(raw) > _MAX_TRANSCRIPT_BYTES:
            return None
        return _calls_from_transcript(raw.decode("utf-8", errors="replace"), max_calls, output_cap, input_cap)
    except Exception:
        return None


def _calls_from_transcript(text: str, max_calls: int, output_cap: int, input_cap: int) -> list[dict[str, Any]]:
    """PLANNER_RESPONSE tool_calls paired FIFO with the following GENERIC steps."""
    rows: list[dict[str, Any]] = []
    waiting = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "PLANNER_RESPONSE":
            calls = step.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if isinstance(call, dict):
                    name = call.get("name")
                    rows.append({
                        "tool": name if isinstance(name, str) else ("" if name is None else str(name)),
                        "status": "no_output",
                        "input": _cap_args(call.get("args"), input_cap),
                        "output": "",
                    })
        elif kind == "GENERIC" and waiting < len(rows):
            _fill_call(rows[waiting], step, output_cap)
            waiting += 1
    keep = max_calls if isinstance(max_calls, int) and not isinstance(max_calls, bool) else 30
    return rows[-keep:] if keep > 0 else []


def _fill_call(row: dict[str, Any], step: dict[str, Any], output_cap: int) -> None:
    content = step.get("content")
    text = content if isinstance(content, str) else ("" if content is None else str(content))
    match = _EXIT_CODE_RE.search(text)
    code = int(match.group(1)) if match else None
    row["status"] = "error" if step.get("status") != "DONE" or (code is not None and code != 0) else "completed"
    redacted = _SECRET_RE.sub("[redacted]", text)
    limit = output_cap if isinstance(output_cap, int) and not isinstance(output_cap, bool) and output_cap > 0 else 0
    tail = redacted[-limit:] if limit else ""
    row["output"] = redacted if len(redacted) <= limit else "\u2026" + tail
    if code is not None:
        row["exit_code"] = code


def _cap_args(args: Any, cap: int) -> dict[str, Any]:
    """Redact and bound tool args. Sensitive keys are replaced at any depth."""
    if not isinstance(args, dict):
        return {}
    limit = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else 0
    return _cap_arg_mapping(args, limit, 0, [_ARGS_MAX_NODES])


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _sensitive_arg_key(name: str) -> bool:
    return _SENSITIVE_KEY_RE.search(name) is not None


def _cap_arg_mapping(mapping: dict, limit: int, depth: int, budget: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if len(out) >= _ARGS_MAX_ITEMS or budget[0] <= 0:
            break
        budget[0] -= 1
        name = key if isinstance(key, str) else str(key)
        if _sensitive_arg_key(name):
            out[name] = _clip(_REDACTED, limit)
            continue
        out[name] = _cap_arg_value(value, limit, depth + 1, budget)
    return out


def _cap_arg_sequence(values: list | tuple, limit: int, depth: int, budget: list[int]) -> list[Any]:
    out: list[Any] = []
    for value in values:
        if len(out) >= _ARGS_MAX_ITEMS or budget[0] <= 0:
            break
        budget[0] -= 1
        out.append(_cap_arg_value(value, limit, depth + 1, budget))
    return out


def _cap_arg_value(value: Any, limit: int, depth: int, budget: list[int]) -> Any:
    if isinstance(value, str):
        return _clip(_SECRET_RE.sub(_REDACTED, value), limit)
    # bool is an int subclass; keep it as a JSON boolean.
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _ARGS_MAX_DEPTH or budget[0] <= 0:
        return _clip(_ARGS_CAP_MARK, limit)
    if isinstance(value, dict):
        return _cap_arg_mapping(value, limit, depth, budget)
    if isinstance(value, (list, tuple)):
        return _cap_arg_sequence(value, limit, depth, budget)
    return _clip(_SECRET_RE.sub(_REDACTED, str(value)), limit)


def build_payload(
    rec_review: dict[str, Any],
    snap: dict[str, Any],
    *,
    acceptance_text: str,
    response_excerpt: str,
    tools: list | None = None,
) -> dict[str, Any]:
    history = rec_review.get("history") if isinstance(rec_review, dict) else None
    previous: list[str] = []
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict) and item.get("verdict") == "fail":
                previous.append(str(item.get("reason") or ""))
    try:
        rnd = int(rec_review.get("round") or 1)
    except (TypeError, ValueError):
        rnd = 1
    try:
        max_redos = int(rec_review.get("max_redos") or 0)
    except (TypeError, ValueError):
        max_redos = 0
    excerpt = "" if response_excerpt is None else str(response_excerpt)
    artifacts = snap.get("artifacts") if isinstance(snap, dict) else {}
    unreadable = _unreadable_names(snap if isinstance(snap, Mapping) else None)
    if isinstance(tools, list):
        tool_rows: list = tools
        tool_evidence = {"source": "agy_transcript", "calls": len(tools)}
        review_note = TOOL_EVIDENCE_SOURCE_NOTE
    else:
        tool_rows = []
        tool_evidence = {"source": "unavailable", "calls": 0}
        review_note = TOOL_EVIDENCE_UNAVAILABLE_NOTE
    return {
        "artifacts": artifacts if isinstance(artifacts, dict) else {},
        "artifact_hash": str((snap or {}).get("artifact_hash") or ""),
        "unreadable": unreadable,
        "artifact_error": (
            "unreadable artifacts: " + ", ".join(unreadable) if unreadable else None
        ),
        "tools": tool_rows,
        "tool_evidence": tool_evidence,
        "review_note": review_note,
        "policy_violations": [],
        "finish": "stop",
        "backend": "antigravity.cli_v1",
        "round": rnd,
        "max_round": 1 + max_redos,
        "previous_rejections": previous,
        "acceptance_text": "" if acceptance_text is None else str(acceptance_text),
        "worker_response_excerpt": excerpt[:2000],
    }


def context_hash(artifact_hash: str, round: int) -> str:
    return hashlib.sha256(f"{artifact_hash}:{round}".encode("utf-8")).hexdigest()


def normalize_verdict(verdict: str) -> str:
    key = str(verdict or "").strip().lower()
    if key in _PASS:
        return "pass"
    if key in _FAIL:
        return "fail"
    if key == "unavailable":
        return "unavailable"
    raise ValueError(f"unsupported review verdict {verdict!r}")


def build_rework_prompt(
    base_prompt: str,
    *,
    k: int,
    n: int,
    reason: str,
    previous: list[str] | None,
) -> str:
    """Original prompt plus a ``REWORK k/n`` section and the verbatim reason."""
    base = "" if base_prompt is None else str(base_prompt)
    lines = [
        f"REWORK {k}/{n}",
        "Lead review rejected the previous attempt. Reason (verbatim):",
        "" if reason is None else str(reason),
    ]
    for item in previous or []:
        lines.append("" if item is None else str(item))
    lines.append(_REWORK_TAIL)
    section = "\n".join(lines)
    if not base:
        return section
    if base.endswith("\n"):
        return base + "\n" + section
    return base + "\n\n" + section


def sum_usage(rounds: list[dict | None]) -> dict | None:
    """Sum numeric fields key-wise. None when every round is None."""
    totals: dict[str, int | float] = {}
    saw = False
    for item in rounds or []:
        if not isinstance(item, dict):
            continue
        saw = True
        for key, value in item.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            current = totals.get(str(key), 0)
            totals[str(key)] = current + value
    if not saw:
        return None
    out: dict[str, int | float] = {}
    for key, value in totals.items():
        if isinstance(value, float) and value.is_integer():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def remaining_sec(deadline: float, now: float) -> float:
    return float(deadline) - float(now)


def unavailable_code(reason: str) -> str:
    match = _UNAVAILABLE_CODE_RE.search("" if reason is None else str(reason))
    if not match:
        return "unknown"
    return match.group(1)


def project_outcome(out: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """Project a terminal review record onto a collect_result payload. Mutates ``out``."""
    if not isinstance(out, dict) or not isinstance(review, dict):
        return out
    state = str(review.get("state") or "")
    redos = _as_int(review.get("redos"), 0)
    max_redos = _as_int(review.get("max_redos"), 0)
    rnd = _as_int(review.get("round"), 1)
    history = list(review.get("history") or []) if isinstance(review.get("history"), list) else []

    if state == "worker_failed":
        if rnd > 1:
            out["rework"] = {"used": redos, "max": max_redos, "rounds": rnd, "history": history}
        return out

    if state == "accepted":
        _apply_common(out, review, redos, max_redos, rnd, history)
        out["review"] = {
            "status": "passed",
            "source": "lead_review",
            "evidence": _last_reason(history, "pass")[:240],
        }
        return out

    if state == "rejected_final":
        _apply_common(out, review, redos, max_redos, rnd, history)
        skip = str(review.get("skip_reason") or f"rework budget exhausted ({redos}/{max_redos})")
        error = (
            "acceptance_failed: lead_review_rejected; "
            + skip
            + "; last reason: "
            + _last_reason(history, "fail")[:200]
        )
        out["ok"] = False
        out["state"] = "fail"
        out["acceptance_failed"] = True
        out["error_source"] = "acceptance"
        out["error"] = error
        out["review"] = {"status": "failed", "source": "lead_review", "evidence": error[:240]}
        return out

    if state == "review_unavailable":
        _apply_common(out, review, redos, max_redos, rnd, history)
        reason = str(review.get("unavailable_reason") or "")
        code = unavailable_code(reason)
        out["ok"] = False
        out["state"] = "fail"
        out["error"] = reason + "; next step: " + NEXT_STEP
        if code == "rework_spawn_failed":
            out["error_source"] = "spawn"
        else:
            out.pop("error_source", None)
        out["review"] = {
            "status": "failed",
            "source": "lead_review",
            "evidence": f"lead_review_unavailable: {code}",
        }
        out["lead_review"] = {
            "outcome": "unavailable",
            "code": code,
            "next_step": NEXT_STEP,
            "human_action_required": True,
        }
        return out

    if state == "interrupted_worker":
        _apply_common(out, review, redos, max_redos, rnd, history)
        error = (
            f"lead_review_interrupted: round {rnd} worker lost in "
            f"service restart; used reworks {redos}/{max_redos} are kept"
        )
        out["ok"] = False
        out["state"] = "fail"
        out["error"] = error
        out["review"] = {
            "status": "failed",
            "source": "lead_review",
            "evidence": "lead_review_interrupted",
        }
        out["lead_review"] = {
            "outcome": "interrupted",
            "code": "lead_review_interrupted",
            "next_step": INTERRUPTED_NEXT,
            "human_action_required": True,
        }
        return out

    return out


def _apply_common(
    out: dict[str, Any],
    review: dict[str, Any],
    redos: int,
    max_redos: int,
    rnd: int,
    history: list,
) -> None:
    out["rework"] = {"used": redos, "max": max_redos, "rounds": rnd, "history": history}
    usage = sum_usage(review.get("usage_rounds") if isinstance(review.get("usage_rounds"), list) else [])
    if usage is not None:
        out["usage"] = usage


def _last_reason(history: list, verdict: str) -> str:
    for item in reversed(history):
        if isinstance(item, dict) and item.get("verdict") == verdict:
            return str(item.get("reason") or "")
    return ""


def _as_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
