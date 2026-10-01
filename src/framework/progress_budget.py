"""Runner progress snapshots and usage-budget policy.

No token values are logged here. Usage numbers are copied as reported and
are never treated as a bill. File checkpoints store name, size, and mtime
only — never file contents, prompts, or commands.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

STALE_AFTER_DEFAULT = 120.0

PROGRESS_STATES = frozenset(
    {
        "planning",
        "preparing",
        "executing",
        "testing",
        "reviewing",
        "waiting_decision",
        "waiting_capacity",
        "stale",
        "idle",
        "delivering",
        "done",
        "unknown",
    }
)

# Token fields we will read when a worker reports them. Absent fields stay
# absent; nothing here is priced or summed into a charge.
TOKEN_TOTAL_KEYS = ("total_tokens", "total")
# Without an explicit total only input + output are added. Cache fields are
# reported on a different basis (agy reported cache_read_tokens ~25x the
# total in issue #10), so adding them would invent a number.
TOKEN_PART_KEYS = (
    "input_tokens",
    "output_tokens",
)
TOOL_CALL_KEYS = ("tool_calls", "tool_call_count")

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|password|authorization|bearer\s+[a-z0-9._\-]{6,}"
    r"|token|[a-f0-9]{32,}|sk-[a-z0-9]{8,})"
)
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$")

MAX_EVENTS = 5
MAX_EVENT_CHARS = 160
MAX_CHECKPOINT = 32
MAX_NAME_CHARS = 180


def utc_iso(epoch: float) -> str:
    """UTC timestamp with a Z suffix. Seconds resolution is enough for ages."""
    stamp = datetime.fromtimestamp(float(epoch), timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _ISO_RE.match(text)
    if match is None:
        return None
    try:
        year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
        frac = match.group(7) or "0"
        micro = int((frac + "000000")[:6])
        stamp = datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp.timestamp()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def sanitize_event(text: Any, *, limit: int = MAX_EVENT_CHARS) -> str:
    """One short status line. Drop anything that looks like a secret."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed or _SECRET_RE.search(collapsed):
        return ""
    if len(collapsed) > limit:
        collapsed = collapsed[:limit]
    return collapsed


def _safe_name(name: str) -> str:
    text = name.replace("\\", "/").strip()
    if not text or _SECRET_RE.search(text):
        return "redacted"
    if len(text) > MAX_NAME_CHARS:
        return text[:MAX_NAME_CHARS]
    return text


def artifact_checkpoint(root: str | Path, *, limit: int = MAX_CHECKPOINT) -> tuple[list[dict[str, Any]], float | None]:
    """Names, sizes, and mtimes under ``root``. No file bytes are read.

    Symlinks are not followed. The walk stops at ``limit`` files.
    """
    base = Path(root)
    try:
        if base.is_symlink() or not base.is_dir():
            return [], None
    except OSError:
        return [], None
    entries: list[dict[str, Any]] = []
    latest: float | None = None
    try:
        walker = os.walk(base, followlinks=False)
    except OSError:
        return [], None
    for dirpath, dirnames, filenames in walker:
        kept: list[str] = []
        for dirname in dirnames:
            child = Path(dirpath) / dirname
            try:
                if child.is_symlink():
                    continue
            except OSError:
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            if len(entries) >= limit:
                return entries, latest
            path = Path(dirpath) / filename
            try:
                if path.is_symlink():
                    continue
                st = path.stat()
            except OSError:
                continue
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                continue
            mtime = float(st.st_mtime)
            entries.append(
                {
                    "name": _safe_name(rel),
                    "size": int(st.st_size),
                    "mtime": utc_iso(mtime),
                }
            )
            if latest is None or mtime > latest:
                latest = mtime
    return entries, latest


# Standard meter names contain the substring "token" and are not secrets.
_USAGE_NAME_OK = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "total",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_tokens",
        "tool_calls",
        "tool_call_count",
    }
)


def usage_fields(raw: Any) -> dict[str, Any]:
    """Copy scalar usage fields as reported. Drop nested text and secrets."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key)[:64]
        if not name or (name not in _USAGE_NAME_OK and _SECRET_RE.search(name)):
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[name] = int(value) if isinstance(value, int) else value
            continue
        if isinstance(value, str):
            text = value.strip()
            if text and len(text) <= 80 and not _SECRET_RE.search(text):
                out[name] = text
    return out


def usage_report(raw: Any, *, source: str) -> dict[str, Any] | None:
    fields = usage_fields(raw)
    if not fields:
        return None
    origin = source if source in {"worker_self_reported", "none"} else "worker_self_reported"
    return {"source": origin, "fields": fields, "note": "not a bill"}


def token_total(fields: Mapping[str, Any] | None) -> int | None:
    """Total tokens as reported. Prefer an explicit total; else sum parts.

    Parts are only added when the report did not include a total. This is
    the worker's own number, not a price.
    """
    if not isinstance(fields, Mapping):
        return None
    for key in TOKEN_TOTAL_KEYS:
        if key in fields:
            number = _number(fields.get(key))
            if number is not None and number >= 0:
                return int(number)
    parts: list[int] = []
    for key in TOKEN_PART_KEYS:
        if key not in fields:
            continue
        number = _number(fields.get(key))
        if number is not None and number >= 0:
            parts.append(int(number))
    if not parts:
        return None
    return sum(parts)


def tool_call_total(payload: Mapping[str, Any] | None) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    for key in TOOL_CALL_KEYS:
        number = _int(payload.get(key))
        if number is not None and number >= 0:
            return number
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        for key in TOOL_CALL_KEYS:
            number = _int(usage.get(key))
            if number is not None and number >= 0:
                return number
    return None


SESSION_ATTRIBUTIONS = frozenset({"open_handle", "new_file", "none"})


def heartbeat_signals_view(raw: Any) -> dict[str, Any]:
    """Which heartbeat inputs were used, and why one was not. Bounded, no paths."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("session"), Mapping):
        return {}
    note = raw["session"]
    attribution = str(note.get("attribution") or "none")
    if attribution not in SESSION_ATTRIBUTIONS:
        attribution = "none"
    row: dict[str, Any] = {"used": bool(note.get("used")), "attribution": attribution}
    reason = sanitize_event(str(note.get("reason") or ""), limit=200) if note.get("reason") else ""
    if reason:
        row["reason"] = reason[:200]
    return {"session": row}


def bound_task_progress(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Durable per-task snapshot. Bounded and free of file contents."""
    src = raw if isinstance(raw, Mapping) else {}
    phase = str(src.get("phase") or "unknown").strip() or "unknown"
    if len(phase) > 64:
        phase = phase[:64]
    source = str(src.get("source") or "none").strip()
    if source not in {"runner", "worker", "none"}:
        source = "none"
    events: list[str] = []
    for item in src.get("events") or []:
        text = sanitize_event(item)
        if text and text not in events:
            events.append(text)
        if len(events) >= MAX_EVENTS:
            break
    checkpoint: list[dict[str, Any]] = []
    for item in src.get("artifacts_checkpoint") or []:
        if not isinstance(item, Mapping):
            continue
        name = _safe_name(str(item.get("name") or ""))
        size = _int(item.get("size"))
        mtime = item.get("mtime") if isinstance(item.get("mtime"), str) else ""
        if parse_iso(mtime) is None:
            mtime = ""
        checkpoint.append({"name": name or "redacted", "size": 0 if size is None or size < 0 else size, "mtime": mtime})
        if len(checkpoint) >= MAX_CHECKPOINT:
            break
    hb = src.get("last_heartbeat_at")
    prog = src.get("last_progress_at")
    out: dict[str, Any] = {
        "phase": phase,
        "last_heartbeat_at": hb if isinstance(hb, str) and parse_iso(hb) is not None else None,
        "last_progress_at": prog if isinstance(prog, str) and parse_iso(prog) is not None else None,
        "events": events,
        "source": source,
        "artifacts_checkpoint": checkpoint,
    }
    signals = heartbeat_signals_view(src.get("heartbeat_signals"))
    if signals:
        out["heartbeat_signals"] = signals
    fields = usage_fields(src.get("usage_fields") if "usage_fields" in src else src.get("usage"))
    if fields:
        out["usage_fields"] = fields
    calls = _int(src.get("tool_calls"))
    if calls is not None and calls >= 0:
        out["tool_calls"] = calls
    return out


def progress_capability(
    *,
    available: bool,
    heartbeat: str | bool,
    artifact_checkpoint: bool,
    subagent_observability: bool | str,
) -> dict[str, Any]:
    """Honest progress capability. ``percent`` is never offered.

    Subagent observability is false or ``\"unknown\"``. A count of 0 is not
    a real observation and is not emitted.
    """
    beat: str | bool = False
    if heartbeat in {"runner_process", "runner_activity", "engine"}:
        beat = heartbeat
    sub: bool | str = "unknown"
    if subagent_observability is False:
        sub = False
    elif subagent_observability == "unknown":
        sub = "unknown"
    return {
        "available": bool(available),
        "heartbeat": beat,
        "artifact_checkpoint": bool(artifact_checkpoint),
        "percent": False,
        "subagent_observability": sub,
    }


def metering_capability(
    *,
    live_usage: bool,
    usage_at_end: bool,
    tool_calls: bool,
    fields: list[str] | tuple[str, ...],
    source: str,
) -> dict[str, Any]:
    origin = source if source in {"worker_self_reported", "none"} else "none"
    clean = [str(item) for item in fields if isinstance(item, str) and item.strip()]
    return {
        "live_usage": bool(live_usage),
        "usage_at_end": bool(usage_at_end),
        "tool_calls": bool(tool_calls),
        "fields": clean,
        "source": origin,
    }


def budget_enforcement_capability(
    *,
    wall_sec: str = "enforced",
    max_tokens: str = "unsupported",
    max_tool_calls: str = "unsupported",
    no_progress_sec: str = "unsupported",
) -> dict[str, str]:
    def _one(value: str, allowed: set[str]) -> str:
        text = str(value or "").strip()
        return text if text in allowed else "unsupported"

    return {
        "wall_sec": _one(wall_sec, {"enforced"}),
        "max_tokens": _one(max_tokens, {"enforced_live", "post_hoc", "unsupported"}),
        "max_tool_calls": _one(max_tool_calls, {"enforced_live", "post_hoc", "unsupported"}),
        "no_progress_sec": _one(no_progress_sec, {"enforced", "unsupported"}),
    }


class BudgetValidationError(ValueError):
    """Submit payload has a budget field of the wrong type."""


def _reject(message: str) -> None:
    raise BudgetValidationError(message)


def normalize_budget(raw: Any) -> dict[str, Any]:
    """Validate goal budget fields. Unknown keys are kept.

    Missing ``budget`` is the caller's default, not an error. A present
    non-object is an error. Bad types raise ``BudgetValidationError``.
    """
    if raw is None:
        return {"wall_sec": 300, "max_reworks": 1}
    if not isinstance(raw, Mapping):
        _reject("budget must be an object")
    out = dict(raw)
    if "wall_sec" in out and out.get("wall_sec") is not None:
        number = _number(out.get("wall_sec"))
        if number is None or number < 0:
            _reject("budget.wall_sec must be a non-negative number")
        out["wall_sec"] = out.get("wall_sec")
    if "max_reworks" in out and out.get("max_reworks") is not None:
        number = _int(out.get("max_reworks"))
        if number is None or number < 0:
            _reject("budget.max_reworks must be a non-negative integer")
        out["max_reworks"] = number
    if "max_tokens" in out and out.get("max_tokens") is not None:
        number = _int(out.get("max_tokens"))
        if number is None or number < 0:
            _reject("budget.max_tokens must be a non-negative integer")
        out["max_tokens"] = number
    if "max_tool_calls" in out and out.get("max_tool_calls") is not None:
        number = _int(out.get("max_tool_calls"))
        if number is None or number < 0:
            _reject("budget.max_tool_calls must be a non-negative integer")
        out["max_tool_calls"] = number
    if "no_progress_sec" in out and out.get("no_progress_sec") is not None:
        number = _number(out.get("no_progress_sec"))
        if number is None or number <= 0:
            _reject("budget.no_progress_sec must be a positive number")
        out["no_progress_sec"] = out.get("no_progress_sec")
    if "on_no_progress" in out and out.get("on_no_progress") is not None:
        mode = str(out.get("on_no_progress") or "").strip()
        if mode not in {"checkpoint", "fail"}:
            _reject("budget.on_no_progress must be checkpoint or fail")
        out["on_no_progress"] = mode
    if "budget_mode" in out and out.get("budget_mode") is not None:
        mode = str(out.get("budget_mode") or "").strip()
        if mode not in {"enforce", "report_only"}:
            _reject("budget.budget_mode must be enforce or report_only")
        out["budget_mode"] = mode
    return out


_BUDGET_FIELDS = ("max_tokens", "max_tool_calls", "no_progress_sec")


def budget_submit_issues(budget: Mapping[str, Any], caps: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Unsupported fields are refused unless report-only. Post-hoc fields warn.

    Returns ``(missing capability names, warnings)``. ``missing`` entries
    look like ``budget:max_tokens``.
    """
    enf = caps.get("budget_enforcement") if isinstance(caps.get("budget_enforcement"), Mapping) else {}
    mode = str(budget.get("budget_mode") or "enforce")
    missing: list[str] = []
    warnings: list[str] = []
    for field in _BUDGET_FIELDS:
        if field not in budget or budget.get(field) is None:
            continue
        level = str(enf.get(field) or "unsupported")
        if level == "unsupported":
            if mode == "report_only":
                warnings.append(f"budget {field} is report-only on this backend")
            else:
                missing.append(f"budget:{field}")
        elif level == "post_hoc":
            warnings.append(f"budget {field}: checked after the run; cannot stop mid-run")
    return missing, warnings


def _task_rows(snap: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [row for row in (snap.get("tasks") or []) if isinstance(row, Mapping)]


def _progress_of(task: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = task.get("progress")
    return raw if isinstance(raw, Mapping) else {}


def _subagent(cap: Mapping[str, Any]) -> bool | str:
    value = cap.get("subagent_observability", "unknown")
    if value is False:
        return False
    return "unknown"


def _empty_progress(*, available: bool, stale_after_sec: float, subagent: bool | str, state: str, phase: str) -> dict[str, Any]:
    return {
        "available": bool(available),
        "phase": phase,
        "state": state if state in PROGRESS_STATES else "unknown",
        "last_heartbeat_at": None,
        "last_progress_at": None,
        "stale_after_sec": float(stale_after_sec),
        "recent_events": [],
        "artifacts_checkpoint": [],
        "source": "none",
        "subagent_observability": subagent,
    }


def derive_goal_progress(
    snap: Mapping[str, Any],
    *,
    scheduler: Mapping[str, Any] | None,
    capability: Mapping[str, Any] | None,
    stale_after_sec: float,
    now: float,
) -> dict[str, Any]:
    """One goal-level progress block from persisted task snapshots.

    Does not call a backend. ``unknown`` stays unknown: a missing heartbeat
    is not stale, and a one-shot backend with ``available`` false does not
    grow a phase or a percentage.
    """
    cap = capability if isinstance(capability, Mapping) else {}
    available = cap.get("available") is True
    sub = _subagent(cap)
    if not available:
        return _empty_progress(
            available=False,
            stale_after_sec=stale_after_sec,
            subagent=sub,
            state="unknown",
            phase="unknown",
        )
    tasks = _task_rows(snap)
    pending = [row for row in (snap.get("pending_decisions") or []) if isinstance(row, Mapping)]
    goal_state = str(snap.get("state") or "")
    sched = scheduler if isinstance(scheduler, Mapping) else {}
    waiting_reason = str(sched.get("waiting_reason") or "")
    queued_ready = int(sched.get("queued_ready") or 0) if not isinstance(sched.get("queued_ready"), bool) else 0

    focus: Mapping[str, Any] | None = None
    focus_progress: Mapping[str, Any] = {}
    stale_task = False
    had_stale = False
    delivering = False
    executing = False
    running_unknown = False

    for task in tasks:
        status = str(task.get("status") or "")
        progress = _progress_of(task)
        if status not in {"running", "cancel_requested"}:
            continue
        phase = str(progress.get("phase") or "unknown")
        source = str(progress.get("source") or "none")
        hb = parse_iso(progress.get("last_heartbeat_at"))
        reliable = source in {"runner", "worker"} and phase != "unknown"
        # A stale sibling stays the focus so a healthy task cannot hide it.
        if reliable and hb is not None and (float(now) - hb) > float(stale_after_sec):
            stale_task = True
            if not had_stale:
                focus, focus_progress = task, progress
                had_stale = True
        elif phase == "finalizing" and not had_stale:
            delivering = True
            focus, focus_progress = task, progress
        elif reliable and phase in {"starting", "executing", "done"} and not had_stale and not delivering:
            executing = True
            focus, focus_progress = task, progress
        elif not reliable and not had_stale and not delivering and focus is None:
            running_unknown = True
            focus, focus_progress = task, progress

    if goal_state in {"completed", "failed", "cancelled"}:
        state = "done"
        phase = "done"
    elif pending or any(str(task.get("status") or "") == "awaiting_decision" for task in tasks):
        state = "waiting_decision"
        phase = str(focus_progress.get("phase") or "executing")
        if phase == "unknown":
            phase = "executing"
    elif stale_task:
        state = "stale"
        phase = str(focus_progress.get("phase") or "executing")
    elif delivering:
        state = "delivering"
        phase = "finalizing"
    elif executing:
        state = "executing"
        phase = str(focus_progress.get("phase") or "executing")
    elif waiting_reason == "capacity" and queued_ready > 0:
        state = "waiting_capacity"
        phase = "unknown"
    elif running_unknown:
        state = "unknown"
        phase = "unknown"
    elif goal_state in {"queued", "running", "blocked", "cancel_requested"} or tasks:
        state = "idle"
        phase = "unknown"
    else:
        state = "idle"
        phase = "unknown"

    # A decision wait or a capacity wait is a coordinator fact. The phase of
    # an unknown runner must not be rewritten into a fake percentage, and a
    # purely unknown running task must not become stale.
    if state == "unknown":
        phase = "unknown"

    events = [sanitize_event(item) for item in (focus_progress.get("events") or [])]
    events = [item for item in events if item][:MAX_EVENTS]
    checkpoint = focus_progress.get("artifacts_checkpoint") if isinstance(focus_progress.get("artifacts_checkpoint"), list) else []
    source = str(focus_progress.get("source") or "none")
    if source not in {"runner", "worker", "none"}:
        source = "none"
    if state in {"waiting_capacity", "idle", "done"} and focus is None:
        source = "none"
    hb_text = focus_progress.get("last_heartbeat_at")
    prog_text = focus_progress.get("last_progress_at")
    return {
        "available": True,
        "phase": phase if isinstance(phase, str) and phase else "unknown",
        "state": state,
        "last_heartbeat_at": hb_text if isinstance(hb_text, str) else None,
        "last_progress_at": prog_text if isinstance(prog_text, str) else None,
        "stale_after_sec": float(stale_after_sec),
        "recent_events": events,
        "artifacts_checkpoint": list(checkpoint)[:MAX_CHECKPOINT],
        "source": source,
        "subagent_observability": sub,
        **(
            {"heartbeat_signals": heartbeat_signals_view(focus_progress.get("heartbeat_signals"))}
            if heartbeat_signals_view(focus_progress.get("heartbeat_signals"))
            else {}
        ),
    }


def enforcement_level(caps: Mapping[str, Any], field: str) -> str:
    enf = caps.get("budget_enforcement") if isinstance(caps.get("budget_enforcement"), Mapping) else {}
    return str(enf.get(field) or "unsupported")


def budget_of(snap: Mapping[str, Any]) -> dict[str, Any]:
    goal = snap.get("goal") if isinstance(snap.get("goal"), Mapping) else {}
    budget = goal.get("budget") if isinstance(goal.get("budget"), Mapping) else {}
    return dict(budget)


def budget_mode(budget: Mapping[str, Any]) -> str:
    mode = str(budget.get("budget_mode") or "enforce")
    return mode if mode in {"enforce", "report_only"} else "enforce"


def on_no_progress(budget: Mapping[str, Any]) -> str:
    mode = str(budget.get("on_no_progress") or "checkpoint")
    return mode if mode in {"checkpoint", "fail"} else "checkpoint"


def format_seconds(value: float) -> str:
    """Whole numbers stay integers so decision text can say ``30s``."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return format(number, "g")
