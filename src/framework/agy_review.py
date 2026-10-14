"""Post-run lead review for workers that cannot raise their own review gate (agy).

TeleAgent asks for a review itself; agy just exits. So the coordinator takes a
workspace baseline right before dispatch, computes what the run actually
changed, flags changes outside the task's expected artifacts, and hands that
evidence to the same pluggable lead. ``fail`` sends the task back with the
lead's reason (bounded by ``budget.max_reworks``).

The baseline lives beside the workspaces (``<workspaces_root>/.collab-review``),
not inside the task workspace, so the worker cannot edit it. It is written once
per task and kept across reworks: every review sees the cumulative change.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

BASELINE_DIRNAME = ".collab-review"
IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".git"})
IGNORED_SUFFIXES = (".pyc", ".pyo")
MAX_FILES = 400
MAX_FILE_BYTES = 2_000_000
MAX_TEXT_KEEP = 64_000  # baseline keeps text up to this size for diffs
MAX_DIFF_PER_FILE = 4_000
MAX_DIFF_TOTAL = 16_000
MAX_WORKER_RESPONSE = 2_000

SCOPE_RULE = (
    "Workspace changes were computed by the coordinator, not reported by the worker. "
    "changed_files lists every file this run added, modified or deleted; "
    "out_of_scope_changes are the ones that are not this task's expected artifacts. "
    "Files present before the run (handed off from earlier tasks or inputs) belong to "
    "other tasks: modifying or deleting them is out of scope unless the task instruction "
    "explicitly asks for it. Fail the review when an out-of-scope change is not justified "
    "by the instruction, and say in reason exactly what must be undone or redone. "
    "Write reason in the same language as the task goal; it is shown to the caller."
)


def baseline_path(workspaces_root: str | Path, goal_id: str, task_id: str) -> Path:
    return Path(workspaces_root) / BASELINE_DIRNAME / str(goal_id) / f"{task_id}.json"


def _ignored(rel: Path) -> bool:
    if any(part in IGNORED_DIRS for part in rel.parts):
        return True
    return rel.name.endswith(IGNORED_SUFFIXES)


def _read_text(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_workspace(root: str | Path) -> dict[str, dict[str, Any]]:
    """{relative posix path: {sha256, size, text?}} for regular files under root."""
    base = Path(root)
    out: dict[str, dict[str, Any]] = {}
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if len(out) >= MAX_FILES:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(base)
            if _ignored(rel):
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                out[rel.as_posix()] = {"sha256": f"size:{size}:{path.stat().st_mtime_ns}", "size": size}
                continue
            data = path.read_bytes()
        except OSError:
            continue
        row: dict[str, Any] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if len(data) <= MAX_TEXT_KEEP:
            text = _read_text(data)
            if text is not None:
                row["text"] = text
        out[rel.as_posix()] = row
    return out


def record_baseline(workspaces_root: str | Path, goal_id: str, task_id: str, workdir: str | Path) -> bool:
    """Snapshot workdir once per task. A rework keeps the first snapshot."""
    target = baseline_path(workspaces_root, goal_id, task_id)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": scan_workspace(workdir)}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return True


def load_baseline(workspaces_root: str | Path, goal_id: str, task_id: str) -> dict[str, dict[str, Any]] | None:
    target = baseline_path(workspaces_root, goal_id, task_id)
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = body.get("files") if isinstance(body, Mapping) else None
    return files if isinstance(files, dict) else None


def _diff_text(rel: str, status: str, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> str:
    old = (before or {}).get("text")
    new = (after or {}).get("text")
    if status == "deleted":
        return f"--- {rel} (deleted, {int((before or {}).get('size') or 0)} bytes)\n"
    if status == "added":
        if new is None:
            return f"+++ {rel} (added, binary or large, {int((after or {}).get('size') or 0)} bytes)\n"
        body = new[:MAX_DIFF_PER_FILE]
        more = "" if len(new) <= MAX_DIFF_PER_FILE else f"\n... ({len(new) - MAX_DIFF_PER_FILE} more chars)"
        return f"+++ {rel} (added)\n{body}{more}\n"
    if old is None or new is None:
        return f"*** {rel} (modified, binary or large, no text diff)\n"
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        n=2,
    )
    text = "".join(lines)
    if len(text) > MAX_DIFF_PER_FILE:
        text = text[:MAX_DIFF_PER_FILE] + f"\n... ({len(text) - MAX_DIFF_PER_FILE} more chars of diff)"
    return text if text.endswith("\n") else text + "\n"


def change_set(
    baseline: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    expected_artifacts: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """What the run changed, which changes are out of scope, and bounded diffs."""
    expected = {str(Path(str(x)).as_posix()).lstrip("./") for x in expected_artifacts or [] if str(x).strip()}
    rows: list[dict[str, Any]] = []
    for rel in sorted(set(baseline) | set(current)):
        before = baseline.get(rel)
        after = current.get(rel)
        if before is None:
            status = "added"
        elif after is None:
            status = "deleted"
        elif before.get("sha256") != after.get("sha256"):
            status = "modified"
        else:
            continue
        rows.append(
            {
                "path": rel,
                "status": status,
                "in_scope": rel in expected,
                "preexisting": before is not None,
                "_before": before,
                "_after": after,
            }
        )
    diffs: list[str] = []
    used = 0
    # Out-of-scope first: those are what the lead must judge.
    for row in sorted(rows, key=lambda r: (r["in_scope"], r["path"])):
        piece = _diff_text(row["path"], row["status"], row["_before"], row["_after"])
        if used + len(piece) > MAX_DIFF_TOTAL:
            diffs.append(f"... diff budget exhausted; {row['path']} ({row['status']}) not shown\n")
            continue
        diffs.append(piece)
        used += len(piece)
    public = [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]
    return {
        "changed_files": public,
        "out_of_scope_changes": [r for r in public if not r["in_scope"]],
        "diff": "".join(diffs),
    }


def restore_preexisting(
    workdir: str | Path,
    baseline: Mapping[str, Mapping[str, Any]],
    out_of_scope: list[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Before a rework, put back files that existed before the run and were
    modified/deleted out of scope. Only text kept in the baseline can be put
    back; anything else is reported so the worker is told to fix it."""
    base = Path(workdir)
    restored: list[str] = []
    unrestorable: list[str] = []
    for row in out_of_scope:
        rel = str(row.get("path") or "")
        if not rel or not row.get("preexisting") or row.get("status") not in {"modified", "deleted"}:
            continue
        text = (baseline.get(rel) or {}).get("text")
        target = base / rel
        try:
            if text is None or base.resolve() not in target.resolve().parents:
                unrestorable.append(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
            restored.append(rel)
        except OSError:
            unrestorable.append(rel)
    return {"restored": restored, "unrestorable": unrestorable}


def review_payload(
    *,
    changes: Mapping[str, Any],
    worker_response: str,
    rework_round: int,
    max_reworks: int,
) -> dict[str, Any]:
    response = str(worker_response or "")
    if len(response) > MAX_WORKER_RESPONSE:
        response = response[:MAX_WORKER_RESPONSE] + " ..."
    return {
        "review_source": "collab_post_run",
        "scope_rule": SCOPE_RULE,
        "rework_round": int(rework_round),
        "max_reworks": int(max_reworks),
        "changed_files": list(changes.get("changed_files") or []),
        "out_of_scope_changes": list(changes.get("out_of_scope_changes") or []),
        "diff": str(changes.get("diff") or ""),
        "worker_self_report": response,
    }


def rework_feedback_entry(
    *,
    round_no: int,
    reason: str,
    out_of_scope: list[Mapping[str, Any]],
    restored: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "round": int(round_no),
        "reason": str(reason or "")[:1500],
        "out_of_scope": [f"{r.get('path')} ({r.get('status')})" for r in out_of_scope][:20],
        "restored": list(restored or [])[:20],
    }


def instruction_with_feedback(instruction: str, feedback: list[Any] | None) -> str:
    """Append the lead's rework notes to the worker instruction (latest last)."""
    rows = [f for f in (feedback or []) if isinstance(f, Mapping)]
    if not rows:
        return instruction
    parts = [instruction.rstrip(), "", "组长审查没有通过，这是返工。请在当前工作区修正后再交付："]
    for f in rows:
        parts.append(f"- 第 {f.get('round')} 次审查意见：{f.get('reason')}")
        scope = f.get("out_of_scope") or []
        if scope:
            parts.append(f"  越界改动：{', '.join(str(x) for x in scope)}")
        restored = f.get("restored") or []
        if restored:
            parts.append(f"  collab 已把这些文件恢复成本任务开始前的样子：{', '.join(str(x) for x in restored)}")
    parts.append("只改本任务要求交付的文件；不要改动之前任务交来的文件。")
    return "\n".join(parts)
