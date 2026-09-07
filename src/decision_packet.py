"""Decision-packet schema + ping policy for lead gating.

Do NOT feed worker CoT. Ping lead only on forks (plan/surface/secret/workaround/…).
Ordinary source R/W and tests return False from should_ping_lead.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional

# Trigger enumeration (consultation).
PING_REASONS = frozenset(
    {
        "plan_commit",
        "plan_rewrite",
        "surface_switch",
        "secret_adjacent",
        "blocked_workaround",
        "constraint_reinterp",
        "permission",
        "outbound_auth",
        "job_end",
    }
)

# Lead ask / decision enums (API maps demand_safe_path & deny_job → reject).
ASK_ENUM = frozenset({"reject", "deny_job", "demand_safe_path", "once"})
LEAD_DECISION_ENUM = frozenset({"once", "reject", "deny_job", "demand_safe_path"})

# Tools / path classes that are ordinary and must NOT ping.
_ORDINARY_TOOLS = frozenset(
    {
        "read",
        "read_file",
        "write",
        "edit",
        "apply_patch",
        "list",
        "ls",
        "glob",
        "grep",
        "search",
        "bash",
        "shell",
        "test",
        "pytest",
    }
)

_SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".md",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
    ".css",
    ".html",
)


def path_pattern(path: str | None) -> str:
    """Collapse a concrete path to a privacy-safe pattern + basename hint."""
    if not path:
        return ""
    s = str(path).replace("\\", "/")
    base = s.rsplit("/", 1)[-1]
    # known secret-ish → class pattern
    low = base.lower()
    if low.startswith(".env") or low == ".env":
        return "**/.env*"
    if "token" in low:
        return "**/*token*"
    if low in ("auth.json", ".netrc", "hosts.yml", "hosts.yaml"):
        return f"**/{base}"
    if "/.ssh" in s.lower():
        return "**/.ssh/**"
    # generic: parent wildcard + basename
    if "/" in s:
        return f"**/{base}"
    return base


def target_class_for(path: str | None, tool: str | None = None) -> str:
    if not path:
        if tool and "browser" in (tool or "").lower():
            return "url"
        return "unknown"
    s = str(path).replace("\\", "/").lower()
    base = s.rsplit("/", 1)[-1]
    if ".env" in base or base.startswith(".env"):
        return "env_file"
    if "token" in base or "credential" in base or base in ("auth.json", ".netrc", "hosts.yml", "hosts.yaml"):
        return "user_secret_store"
    if "/.ssh" in s or "gh/hosts" in s or ".netrc" in s:
        return "user_secret_store"
    if "cookie" in s or "profile" in s or "chromium" in s or "chrome" in s:
        return "browser_profile"
    if s.startswith("http://") or s.startswith("https://"):
        return "url"
    if any(base.endswith(suf) for suf in _SOURCE_SUFFIXES):
        return "source"
    return "path"


def charter_hash(charter: dict | None) -> str:
    if not charter:
        return ""
    blob = json.dumps(charter, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_decision_packet(
    *,
    ping_reason: str,
    proposed_action: dict,
    worker_intent: str,
    blocker: dict | str | None,
    mismatch: dict | None = None,
    spine: list | None = None,
    risk_tags: list | None = None,
    ask: str = "reject",
    charter: dict | None = None,
    charter_ref: str | None = None,
    job_id: str | None = None,
    include_charter_full: bool = False,
) -> dict:
    """Assemble a lead decision packet. worker_intent + blocker + charter_ref are required fields."""
    if ping_reason not in PING_REASONS and ping_reason not in ("plan_commit", "plan_rewrite"):
        # allow plan_commit/rewrite aliases already in set; else coerce to permission
        if ping_reason not in PING_REASONS:
            ping_reason = "permission"

    intent = (worker_intent or "").strip()
    if len(intent) > 400:
        intent = intent[:400]

    if isinstance(blocker, str):
        blocker_obj: dict = {"detail": blocker}
    elif isinstance(blocker, dict):
        blocker_obj = blocker
    else:
        blocker_obj = {"detail": "unspecified"}

    cref = charter_ref or (charter_hash(charter) if charter else "")
    if not cref:
        cref = "missing"

    action = dict(proposed_action or {})
    if "target_class" not in action and action.get("target"):
        action["target_class"] = target_class_for(action.get("target"), action.get("tool"))
    if "path_pattern" not in action and action.get("target"):
        action["path_pattern"] = path_pattern(action.get("target"))
    if "when" not in action:
        action["when"] = "before_exec"

    spine_list = spine or []
    if len(spine_list) > 8:
        spine_list = spine_list[-8:]
    # ensure 0–8; consultation wants 5–8 when available — do not pad fake steps

    packet: dict[str, Any] = {
        "ping_reason": ping_reason,
        "charter_ref": cref,
        "proposed_action": action,
        "worker_intent": intent or "(missing intent — treat as suspicious)",
        "blocker": blocker_obj,
        "mismatch": mismatch or {},
        "spine": spine_list,
        "risk_tags": risk_tags or [],
        "ask": ask if ask in ASK_ENUM else "reject",
    }
    if job_id:
        packet["job_id"] = job_id
    if include_charter_full and charter:
        packet["charter"] = charter
    return packet


def _collect_all_paths_patterns_ops(permission_dict: dict) -> tuple[list[str], list[str], list[str]]:
    """Collect *all* paths / patterns / ops from a permission request (not just the first)."""
    paths: list[str] = []
    patterns: list[str] = []
    ops: list[str] = []

    def add_path(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str) and v.strip():
            paths.append(v.strip())
        elif isinstance(v, (list, tuple)):
            for x in v:
                add_path(x)
        elif isinstance(v, dict):
            for k in ("path", "paths", "file", "filepath", "target", "uri"):
                if k in v:
                    add_path(v[k])

    def add_pat(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str) and v.strip():
            patterns.append(v.strip())
        elif isinstance(v, (list, tuple)):
            for x in v:
                add_pat(x)

    def add_op(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str) and v.strip():
            ops.append(v.strip())
        elif isinstance(v, (list, tuple)):
            for x in v:
                add_op(x)

    if not isinstance(permission_dict, dict):
        return [], [], []

    for k in ("path", "paths", "file", "filepath", "target", "uri"):
        if k in permission_dict:
            add_path(permission_dict[k])
    for k in ("patterns", "pattern", "glob", "globs"):
        if k in permission_dict:
            add_pat(permission_dict[k])
    for k in ("ops", "operations", "op", "action", "actions"):
        if k in permission_dict:
            add_op(permission_dict[k])

    # permission / tool fields as ops hints
    for k in ("permission", "permissions", "tool", "type"):
        v = permission_dict.get(k)
        if isinstance(v, str) and v.strip():
            if k in ("permission", "permissions", "tool") and v.strip() not in ops:
                ops.append(v.strip())
        elif isinstance(v, list):
            add_op(v)

    for nest_key in ("metadata", "input", "args"):
        nest = permission_dict.get(nest_key)
        if isinstance(nest, dict):
            p2, g2, o2 = _collect_all_paths_patterns_ops(nest)
            paths.extend(p2)
            patterns.extend(g2)
            ops.extend(o2)

    # de-dupe preserving order
    def uniq(xs: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return uniq(paths), uniq(patterns), uniq(ops)


def packet_from_permission(
    permission_dict: dict,
    *,
    worker_intent: str,
    blocker: dict | str,
    charter: dict | None = None,
    charter_ref: str | None = None,
    include_charter_full: bool = False,
    spine: list | None = None,
    risk_tags: list | None = None,
    mismatch: dict | None = None,
    ping_reason: str = "permission",
) -> dict:
    """Build packet from a TeleAgent permission payload — never tool+path only.

    Includes *all* paths / patterns / ops from the request (not just the first item).
    """
    paths, patterns, ops = _collect_all_paths_patterns_ops(
        permission_dict if isinstance(permission_dict, dict) else {}
    )
    # primary target: first path, else first pattern
    path = paths[0] if paths else (patterns[0] if patterns else "")
    tool = (
        permission_dict.get("tool")
        or permission_dict.get("permission")
        or permission_dict.get("type")
        or (ops[0] if ops else None)
        or "unknown"
    )
    # worst / most-sensitive target_class among all candidates
    classes = [target_class_for(p, str(tool)) for p in (paths + patterns) or [None]]
    priority = ("user_secret_store", "browser_profile", "env_file", "url", "path", "source", "unknown")
    tclass = "unknown"
    for cand in priority:
        if cand in classes:
            tclass = cand
            break
    # path_pattern: join unique patterns for all targets
    ppats = [path_pattern(p) for p in (paths + patterns) if p]
    ppats = list(dict.fromkeys([x for x in ppats if x]))
    proposed = {
        "tool": str(tool),
        "target": str(path) if path else "",
        "targets": list(paths),
        "patterns": list(patterns),
        "ops": list(ops),
        "target_class": tclass,
        "path_pattern": ppats[0] if len(ppats) == 1 else ("|".join(ppats) if ppats else path_pattern(str(path) if path else None)),
        "path_patterns": ppats,
        "when": "before_exec",
    }
    tags = list(risk_tags or [])
    if proposed["target_class"] in ("user_secret_store", "env_file", "browser_profile"):
        if "secret_adjacent" not in tags:
            tags.append("secret_adjacent")
    return build_decision_packet(
        ping_reason=ping_reason,
        proposed_action=proposed,
        worker_intent=worker_intent,
        blocker=blocker,
        mismatch=mismatch,
        spine=spine,
        risk_tags=tags,
        ask="reject",
        charter=charter,
        charter_ref=charter_ref,
        include_charter_full=include_charter_full,
    )


def lead_permission_schema() -> dict:
    """JSON schema for lead permission replies (includes demand_safe_path)."""
    return {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["once", "reject", "deny_job", "demand_safe_path"],
            },
            "reason": {"type": "string"},
            "safe_path_hint": {"type": "string"},
        },
        "required": ["decision", "reason"],
        "additionalProperties": False,
    }


def map_lead_decision_to_api(decision: str | None) -> tuple[str, str | None]:
    """Map lead decision → TeleAgent reply. demand_safe_path/deny_job → reject.

    Returns (api_reply, note_or_None).
    """
    d = (decision or "").strip().lower()
    if d == "once":
        return "once", None
    if d == "always":
        # harden: never promote always
        return "once", "downgraded always->once"
    if d == "demand_safe_path":
        return "reject", "mapped demand_safe_path->reject"
    if d == "deny_job":
        return "reject", "mapped deny_job->reject"
    if d == "reject":
        return "reject", None
    return "reject", f"unknown decision {decision!r} -> reject"


class PingDeduper:
    """Dedupe by *permission request id* (not target_class/path_pattern).

    Same-class subsequent legitimate requests with a new id may be approved again.
    Pattern-tuple API kept as no-op compatibility shims so callers do not
    accidentally suppress follow-up requests.
    """

    def __init__(self) -> None:
        self._inflight_ids: set[str] = set()
        self._done_ids: set[str] = set()

    def already_handled(self, request_id: str) -> bool:
        rid = (request_id or "").strip()
        return bool(rid) and (rid in self._done_ids or rid in self._inflight_ids)

    def should_emit_id(self, request_id: str) -> bool:
        rid = (request_id or "").strip()
        if not rid:
            return False
        return rid not in self._done_ids and rid not in self._inflight_ids

    def mark_inflight_id(self, request_id: str) -> None:
        rid = (request_id or "").strip()
        if rid:
            self._inflight_ids.add(rid)

    def mark_replied_id(self, request_id: str) -> None:
        rid = (request_id or "").strip()
        if rid:
            self._inflight_ids.discard(rid)
            self._done_ids.add(rid)

    # --- backward-compatible shims (always allow; do NOT dedupe by pattern) ---
    @staticmethod
    def key(ping_reason: str, target_class: str, path_pattern_s: str) -> tuple[str, str, str]:
        return (ping_reason or "", target_class or "", path_pattern_s or "")

    def should_emit(self, ping_reason: str, target_class: str, path_pattern_s: str) -> bool:
        # Intentionally always True: pattern-based suppress blocked same-class re-asks.
        return True

    def mark_inflight(self, ping_reason: str, target_class: str, path_pattern_s: str) -> None:
        return None

    def mark_replied(self, ping_reason: str, target_class: str, path_pattern_s: str) -> None:
        return None

    def clear(self) -> None:
        self._inflight_ids.clear()
        self._done_ids.clear()


def should_ping_lead(
    *,
    ping_reason: str | None = None,
    tool: str | None = None,
    path: str | None = None,
    target_class: str | None = None,
    is_test: bool = False,
    ordinary_rw: bool | None = None,
) -> bool:
    """Return False for ordinary source R/W and tests; True on fork triggers."""
    if is_test:
        return False
    reason = (ping_reason or "").strip()
    if reason in PING_REASONS:
        # explicit fork reason → ping (permission still subject to ordinary check below only if unmarked)
        if reason != "permission":
            return True

    tc = target_class or target_class_for(path, tool)
    if tc in ("user_secret_store", "env_file", "browser_profile"):
        return True

    tool_l = (tool or "").lower()
    if ordinary_rw is True:
        return False
    if ordinary_rw is False and reason == "permission":
        return True

    # Heuristic: source-like path + ordinary tool → no ping
    p = (path or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1] if p else ""
    if is_test or base.startswith("test_") or "/tests/" in p or base.endswith("_test.py"):
        return False
    if tool_l in _ORDINARY_TOOLS or not tool_l:
        if tc == "source" or any(base.endswith(suf) for suf in _SOURCE_SUFFIXES):
            return False
        if not p and reason in ("", "permission"):
            # no path, no fork tag — treat as ordinary (caller should set ping_reason for real forks)
            return False

    if reason == "permission":
        # permission on non-source / unknown — still ping (grey)
        if tc == "source":
            return False
        return True

    return bool(reason in PING_REASONS)


def format_lead_prompt(packet: dict, allow_hint: str = "") -> str:
    """Compact lead prompt: decision packet JSON only, no CoT."""
    body = json.dumps(packet, ensure_ascii=False, indent=2)
    return (
        "You are the team lead gating a TeleAgent worker. "
        "Judge whether the proposed action drifts from the charter. "
        "Output JSON only: decision + reason "
        "(decision enum: once|reject|deny_job|demand_safe_path). "
        "Never choose always. For secret_adjacent / auth workarounds prefer "
        "reject or demand_safe_path — never once.\n"
        f"{allow_hint}\n"
        f"Decision packet:\n{body}\n"
    )


__all__ = [
    "PING_REASONS",
    "ASK_ENUM",
    "PingDeduper",
    "build_decision_packet",
    "packet_from_permission",
    "should_ping_lead",
    "lead_permission_schema",
    "map_lead_decision_to_api",
    "format_lead_prompt",
    "path_pattern",
    "target_class_for",
    "charter_hash",
]
