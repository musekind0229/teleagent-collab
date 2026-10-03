"""Goal-level artifact index for status readers (``--full`` / raw API).

Task results keep their raw ``artifacts`` untouched. This view lists each
produced file once per task as ``{task_id, path}``, with ``path`` relative to
the task workspace and ``/`` separators, the same spelling the client summary
uses. Absolute paths outside the workspace stay as given.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:/|//)")
_WS_KEYS = ("workspace", "workdir", "workspace_path", "workspace_dir")


def workspace_relative(path: str, workspace: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    unified = text.replace("\\", "/")
    ws = str(workspace or "").strip().replace("\\", "/").rstrip("/")
    is_abs = unified.startswith("/") or bool(_WIN_ABS_RE.match(unified))
    if is_abs:
        if not ws:
            return text
        fold = bool(_WIN_ABS_RE.match(unified)) or bool(_WIN_ABS_RE.match(ws))
        a, b = (unified.lower(), ws.lower()) if fold else (unified, ws)
        if not a.startswith(b + "/"):
            return text
        unified = unified[len(ws) + 1 :]
    parts = [seg for seg in unified.split("/") if seg not in ("", ".")]
    return "/".join(parts) if parts else text


def _labels(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_labels(item))
        return out
    if isinstance(value, Mapping):
        for key in ("path", "name", "file"):
            if isinstance(value.get(key), str):
                return [value[key]]
        return [k for k in value.keys() if isinstance(k, str)]
    return []


def _workspace(task: Mapping[str, Any], result: Mapping[str, Any] | None) -> str:
    for source in (task, result or {}):
        for key in _WS_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def goal_artifact_index(tasks: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, Mapping):
            continue
        result = task.get("result") if isinstance(task.get("result"), Mapping) else None
        if result is None:
            continue
        task_id = str(task.get("task_id") or "")
        ws = _workspace(task, result)
        for label in _labels(result.get("artifacts")):
            path = workspace_relative(label, ws)
            if not path or (task_id, path) in seen:
                continue
            seen.add((task_id, path))
            out.append({"task_id": task_id, "path": path})
    return out
