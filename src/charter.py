#!/usr/bin/env python3
"""Load and validate job charter / task-package files (YAML or JSON).

Charter is the mechanical contract between the eternal (perpetual) layer
and the TeleAgent worker. Eternal writes the package; glue runs it;
reports land on disk. Do not pilot workers with long free-form prompts alone.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Required top-level keys (acceptance may be provided as done_when instead).
REQUIRED = ("goal", "must", "must_not")


class CharterError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    in_single = in_double = False
    out = []
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "" or s == "~" or s.lower() == "null":
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p.strip()) for p in inner.split(",")]
    return s


def _load_simple_yaml(text: str) -> dict:
    """Minimal YAML subset for charter files (maps, lists, | blocks). No PyYAML required."""
    lines = text.splitlines()
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if content.startswith("- "):
            item_raw = content[2:].strip()
            if not isinstance(parent, list):
                raise CharterError(f"list item without list parent: {content!r}")
            if ":" in item_raw and not item_raw.startswith("{") and not (
                item_raw.startswith('"') or item_raw.startswith("'")
            ):
                # map item in list: - key: val
                k, _, v = item_raw.partition(":")
                k, v = k.strip(), v.strip()
                obj: dict[str, Any] = {}
                if v in ("|", ">"):
                    block, i = _read_block(lines, i, indent + 2)
                    obj[k] = block
                    parent.append(obj)
                    stack.append((indent, obj))
                elif v == "":
                    obj[k] = {}
                    parent.append(obj)
                    stack.append((indent, obj))
                else:
                    obj[k] = _parse_scalar(v)
                    parent.append(obj)
            else:
                parent.append(_parse_scalar(item_raw))
            continue

        if ":" not in content:
            raise CharterError(f"expected key: value, got {content!r}")
        key, _, rest = content.partition(":")
        key, rest = key.strip(), rest.strip()

        if rest in ("|", ">"):
            block, i = _read_block(lines, i, indent + 2)
            if isinstance(parent, dict):
                parent[key] = block
            else:
                raise CharterError(f"cannot set key on non-dict parent: {key}")
            continue
        if rest == "":
            # peek next non-empty
            j = i
            while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j += 1
            nxt = lines[j] if j < len(lines) else ""
            nxt_indent = len(nxt) - len(nxt.lstrip(" ")) if nxt.strip() else -1
            if nxt.strip().startswith("- ") and nxt_indent > indent:
                child: list = []
                if isinstance(parent, dict):
                    parent[key] = child
                else:
                    raise CharterError(f"cannot set key on non-dict: {key}")
                stack.append((indent, child))
            else:
                child_d: dict = {}
                if isinstance(parent, dict):
                    parent[key] = child_d
                else:
                    raise CharterError(f"cannot set key on non-dict: {key}")
                stack.append((indent, child_d))
            continue

        val = _parse_scalar(rest)
        if isinstance(parent, dict):
            parent[key] = val
        else:
            raise CharterError(f"cannot set key on non-dict: {key}")
    return root


def _read_block(lines: list[str], start: int, min_indent: int) -> tuple[str, int]:
    chunks: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            chunks.append("")
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < min_indent:
            break
        chunks.append(line[min_indent:] if len(line) >= min_indent else line.lstrip())
        i += 1
    # trim trailing blank
    while chunks and chunks[-1] == "":
        chunks.pop()
    return "\n".join(chunks), i


def load_charter(path: str | Path) -> dict:
    """Load charter from YAML or JSON path; validate required fields."""
    p = Path(path)
    if not p.is_file():
        raise CharterError(f"charter not found: {p}")
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except ImportError:
            data = _load_simple_yaml(text)
    else:
        # try JSON then YAML
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(text)
            except ImportError:
                data = _load_simple_yaml(text)

    if not isinstance(data, dict):
        raise CharterError("charter root must be a mapping")
    validate_charter(data)
    # stamp source path for reports
    data.setdefault("_source", str(p.resolve()))
    if "name" not in data:
        data["name"] = p.stem.replace(".charter", "") or "job"
    return data


def validate_charter(data: dict) -> None:
    for k in REQUIRED:
        if k not in data:
            raise CharterError(f"charter missing required field: {k}")
    if not isinstance(data["goal"], str) or not data["goal"].strip():
        raise CharterError("goal must be a non-empty string")
    for list_key in ("must", "must_not"):
        if not isinstance(data[list_key], list):
            raise CharterError(f"{list_key} must be a list")
    # at least one allowlist field present (may be empty list)
    allow_keys = ("allow_secret_globs", "allow_paths", "allow_keys")
    if not any(k in data for k in allow_keys):
        raise CharterError(
            "charter must include at least one of: allow_secret_globs, allow_paths, allow_keys "
            "(use [] when none)"
        )
    for k in allow_keys:
        if k in data and data[k] is not None and not isinstance(data[k], list):
            raise CharterError(f"{k} must be a list")
    done = data.get("done_when")
    acceptance = data.get("acceptance")
    if done is None and acceptance is None:
        raise CharterError("charter needs done_when and/or acceptance")
    if done is not None and not isinstance(done, (dict, list, str)):
        raise CharterError("done_when must be mapping, list, or string")
    if acceptance is not None and not isinstance(acceptance, str):
        raise CharterError("acceptance must be a string")
    # 条5 authorization fields (optional; validated when present / by task_kind)
    try:
        from task_auth import validate_auth_fields, extract_auth_fields, normalize_task_kind
    except ImportError:
        validate_auth_fields = None  # type: ignore
    if validate_auth_fields is not None:
        if "task_kind" in data or "install_roots" in data or "network_allow" in data:
            try:
                if "task_kind" in data:
                    normalize_task_kind(data.get("task_kind"))
                validate_auth_fields(data)
            except ValueError as e:
                raise CharterError(str(e)) from e
        # soft-normalize defaults into a copy? keep original; extract at runtime
        _ = extract_auth_fields(data)
    for list_key in ("network_allow", "install_roots", "lead_review_steps", "user_gate_permissions"):
        if list_key in data and data[list_key] is not None and not isinstance(data[list_key], list):
            raise CharterError(f"{list_key} must be a list")
    if "rollback" in data and data["rollback"] is not None:
        if not isinstance(data["rollback"], (str, dict)):
            raise CharterError("rollback must be a string or mapping")


def charter_for_glue(charter: dict) -> dict:
    """Subset passed as run_job(charter=...) for hard rules + decision packets."""
    out = {
        "goal": charter["goal"],
        "must": list(charter.get("must") or []),
        "must_not": list(charter.get("must_not") or []),
    }
    for k in (
        "allow_secret_globs",
        "allow_paths",
        "allow_keys",
        "allowed_surfaces",
        "task_kind",
        "network_allow",
        "install_roots",
        "lead_review_steps",
        "user_gate_permissions",
        "acceptance",
        "rollback",
        "done_when",
    ):
        if k in charter and charter[k] is not None:
            out[k] = charter[k]
    return out


def expected_artifacts(charter: dict, workspace: Path | None = None) -> list[str]:
    """Resolve done_when.artifacts (or done_when list) to absolute paths."""
    done = charter.get("done_when")
    arts: list[str] = []
    if isinstance(done, dict):
        raw = done.get("artifacts") or done.get("files") or []
        if isinstance(raw, str):
            raw = [raw]
        arts = list(raw)
    elif isinstance(done, list):
        arts = list(done)
    elif isinstance(done, str):
        # free-text done_when; no path list — caller may still use acceptance-only review
        arts = []

    # optional top-level artifacts
    if charter.get("artifacts"):
        extra = charter["artifacts"]
        if isinstance(extra, str):
            arts.append(extra)
        elif isinstance(extra, list):
            arts.extend(extra)

    ws = workspace
    resolved: list[str] = []
    for a in arts:
        p = Path(a)
        if not p.is_absolute() and ws is not None:
            p = ws / a
        resolved.append(str(p))
    return resolved


def build_instruction(charter: dict) -> str:
    """Compose worker instruction from charter fields (not a free-form oral prompt)."""
    if isinstance(charter.get("instruction"), str) and charter["instruction"].strip():
        # Explicit instruction still must sit beside charter fields; prepend charter spine.
        explicit = charter["instruction"].strip()
    else:
        explicit = ""

    lines = [
        f"Goal: {charter['goal'].strip()}",
        "",
        "Must:",
    ]
    for m in charter.get("must") or []:
        lines.append(f"- {m}")
    lines.append("")
    lines.append("Must not:")
    for m in charter.get("must_not") or []:
        lines.append(f"- {m}")

    done = charter.get("done_when")
    if isinstance(done, dict) and done.get("artifacts"):
        lines.append("")
        lines.append("Done when these artifacts exist:")
        arts = done["artifacts"]
        if isinstance(arts, str):
            arts = [arts]
        for a in arts:
            lines.append(f"- {a}")
    elif isinstance(done, str) and done.strip():
        lines.append("")
        lines.append(f"Done when: {done.strip()}")

    if charter.get("acceptance"):
        lines.append("")
        lines.append("Acceptance:")
        lines.append(charter["acceptance"].strip())

    # 条5: surface authorization spine (prompt constraint + mechanical hints)
    try:
        from task_auth import extract_auth_fields
        auth = extract_auth_fields(charter)
        lines.append("")
        lines.append(f"Task kind: {auth['task_kind']}")
        if auth["network_allow"]:
            lines.append("Network allow:")
            for n in auth["network_allow"]:
                lines.append(f"- {n}")
        if auth["install_roots"]:
            lines.append("Install roots (only these dirs for system installs):")
            for r in auth["install_roots"]:
                lines.append(f"- {r}")
        if auth["user_gate_permissions"]:
            lines.append("User-gate permissions (must not silently expand):")
            for g in auth["user_gate_permissions"]:
                lines.append(f"- {g}")
        if auth["lead_review_steps"]:
            lines.append("Steps requiring lead review:")
            for s in auth["lead_review_steps"]:
                lines.append(f"- {s}")
        if auth["rollback"]:
            lines.append("")
            lines.append("Rollback:")
            rb = auth["rollback"]
            lines.append(rb if isinstance(rb, str) else str(rb))
    except Exception:
        pass

    if explicit:
        lines.append("")
        lines.append("Task detail:")
        lines.append(explicit)

    lines.append("")
    lines.append("Stay inside the assigned workspace. When done, stop.")
    return "\n".join(lines)


def job_name(charter: dict) -> str:
    name = str(charter.get("name") or "job")
    # filesystem-safe
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "job"
    return name
