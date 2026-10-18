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
            out['\x00scan_incomplete'] = {'reason': 'workspace scan file capacity exceeded'}
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(base)
            if _ignored(rel):
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024*1024), b''):
                        digest.update(chunk)
                out[rel.as_posix()] = {'sha256': digest.hexdigest(), 'size': size, 'raw_bytes_omitted': True}
                continue
            data = path.read_bytes()
        except OSError:
            out['\x00scan_incomplete'] = {'reason': 'workspace scan read failed'}
            continue
        row: dict[str, Any] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "raw_hex": data.hex()}
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


def _diff_text(rel: str, status: str, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> tuple[str, bool, dict[str, Any]]:
    """Generate diff text and return (diff_string, is_truncated, metrics_dict)."""
    old = (before or {}).get("text")
    new = (after or {}).get("text")
    metrics: dict[str, Any] = {
        "rel": rel,
        "status": status,
        "original_len": 0,
        "provided_len": 0,
        "truncated_at": None,
        "truncated": False,
    }
    if status == "deleted":
        sz = int((before or {}).get('size') or 0)
        text = f"--- {rel} (deleted, {sz} bytes)\n"
        metrics["original_len"] = len(text)
        metrics["provided_len"] = len(text)
        return text, False, metrics
    if status == "added":
        if new is None:
            sz = int((after or {}).get('size') or 0)
            text = f"+++ {rel} (added, binary or large, {sz} bytes)\n"
            metrics["original_len"] = len(text)
            metrics["provided_len"] = len(text)
            return text, False, metrics
        orig_len = len(new)
        metrics["original_len"] = orig_len
        if orig_len > MAX_DIFF_PER_FILE:
            body = new[:MAX_DIFF_PER_FILE]
            more = f"\n... ({orig_len - MAX_DIFF_PER_FILE} more chars)"
            metrics["provided_len"] = len(body)
            metrics["truncated_at"] = MAX_DIFF_PER_FILE
            metrics["truncated"] = True
            return f"+++ {rel} (added)\n{body}{more}\n", True, metrics
        metrics["provided_len"] = orig_len
        return f"+++ {rel} (added)\n{new}\n", False, metrics
    if old is None or new is None:
        text = f"*** {rel} (modified, binary or large, no text diff)\n"
        metrics["original_len"] = len(text)
        metrics["provided_len"] = len(text)
        metrics['source_omitted'] = True
        metrics['source_before_bytes'] = (before or {}).get('size')
        metrics['source_after_bytes'] = (after or {}).get('size')
        metrics['truncated'] = True
        return text, True, metrics
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        n=2,
    )
    text = "".join(lines)
    orig_len = len(text)
    metrics["original_len"] = orig_len
    if orig_len > MAX_DIFF_PER_FILE:
        more = f"\n... ({orig_len - MAX_DIFF_PER_FILE} more chars of diff)"
        res = text[:MAX_DIFF_PER_FILE] + more
        metrics["provided_len"] = MAX_DIFF_PER_FILE
        metrics["truncated_at"] = MAX_DIFF_PER_FILE
        metrics["truncated"] = True
        return (res if res.endswith("\n") else res + "\n"), True, metrics
    metrics["provided_len"] = orig_len
    return (text if text.endswith("\n") else text + "\n"), False, metrics


def change_set(
    baseline: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    expected_artifacts: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """What the run changed, which changes are out of scope, and bounded diffs."""
    expected = {str(Path(str(x)).as_posix()).lstrip("./") for x in expected_artifacts or [] if str(x).strip()}
    rows: list[dict[str, Any]] = []
    for rel in sorted(set(baseline) | set(current)):
        if rel == '\x00scan_incomplete':
            continue
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
    diff_truncated = '\x00scan_incomplete' in baseline or '\x00scan_incomplete' in current
    truncated_files: list[str] = []
    truncation_metrics: list[dict[str, Any]] = []
    # Out-of-scope first: those are what the lead must judge.
    for row in sorted(rows, key=lambda r: (r["in_scope"], r["path"])):
        piece, piece_truncated, metric = _diff_text(row["path"], row["status"], row["_before"], row["_after"])
        if piece_truncated:
            diff_truncated = True
            if row["path"] not in truncated_files:
                truncated_files.append(row["path"])
            truncation_metrics.append(metric)
        if used + len(piece) > MAX_DIFF_TOTAL:
            diff_truncated = True
            if row["path"] not in truncated_files:
                truncated_files.append(row["path"])
            truncation_metrics.append({
                "rel": row["path"],
                "status": row["status"],
                "original_len": len(piece),
                "provided_len": 0,
                "truncated_at": 0,
                "truncated": True,
                "reason": "diff budget exhausted",
            })
            diffs.append(f"... diff budget exhausted; {row['path']} ({row['status']}) not shown\n")
            continue
        diffs.append(piece)
        used += len(piece)
    public = [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]
    return {
        "changed_files": public,
        "out_of_scope_changes": [r for r in public if not r["in_scope"]],
        "diff": "".join(diffs),
        "diff_truncated": diff_truncated,
        "truncated_files": truncated_files,
        "truncation_metrics": truncation_metrics,
    }


def extract_function_block(source_text: str, func_name: str) -> str | None:
    """Extract a complete function definition from python source text using ast."""
    import ast
    try:
        tree = ast.parse(source_text)
    except Exception:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = source_text.splitlines(keepends=True)
            # ast lineno is 1-indexed
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else len(lines)
            return "".join(lines[start:end])
    return None


def build_review_material_packet(
    *,
    workdir: str | Path | None = None,
    required_blocks: list[dict[str, Any]] | None = None,
    capacity_limit_bytes: int = 100_000,
) -> dict[str, Any]:
    """Build minimal necessary source packet containing complete function bodies and direct caller dependencies.

    required_blocks item:
      {
        "path": "relative/path.py",
        "function": "target_func",
        "dependency": {
            "path": "relative/path.py",
            "function": "caller_or_helper_func",
            "reason": "direct call from target_func"
        }
      }
    """
    provided_files: dict[str, str] = {}
    provided_blocks: list[dict[str, Any]] = []
    unprovided_files: list[str] = []
    missing_blocks: list[dict[str, Any]] = []
    unreviewed_dependencies: list[dict[str, Any]] = []
    dependency_reasons: dict[str, str] = {}

    total_bytes = 0
    capacity_exceeded = False
    base = Path(workdir) if workdir else None

    # Strict caller required_blocks validation:
    # missing, not a list, empty list, or empty/invalid items must immediately block.
    if required_blocks is None:
        return {
            "review_status": "review_blocked",
            "is_blocked": True,
            "blocked_reason": "missing_required_blocks",
            "provided_files": {},
            "provided_blocks": [],
            "unprovided_files": [],
            "missing_blocks": [{"path": None, "function": None, "reason": "required_blocks field is missing"}],
            "unreviewed_dependencies": [],
            "dependency_reasons": {},
            "capacity_limit_bytes": capacity_limit_bytes,
            "total_bytes": 0,
            "capacity_exceeded": False,
        }

    if not isinstance(required_blocks, list) or len(required_blocks) == 0:
        return {
            "review_status": "review_blocked",
            "is_blocked": True,
            "blocked_reason": "empty_required_blocks",
            "provided_files": {},
            "provided_blocks": [],
            "unprovided_files": [],
            "missing_blocks": [{"path": None, "function": None, "reason": "required_blocks is empty or not a list"}],
            "unreviewed_dependencies": [],
            "dependency_reasons": {},
            "capacity_limit_bytes": capacity_limit_bytes,
            "total_bytes": 0,
            "capacity_exceeded": False,
        }

    for req in required_blocks:
        if not isinstance(req, dict) or not req.get("path") or not req.get("function"):
            missing_blocks.append({
                "path": req.get("path") if isinstance(req, dict) else None,
                "function": req.get("function") if isinstance(req, dict) else None,
                "reason": "missing target path or function",
            })
            continue

        rel_path = req.get("path")
        func_name = req.get("function")
        dep = req.get("dependency")
        dep_reason = req.get("dependency_reason") or (dep.get("reason") if isinstance(dep, dict) else "")

        # Try to read target file
        file_text = None
        if base and rel_path:
            target_file = base / rel_path
            try:
                if target_file.is_file():
                    file_text = target_file.read_text(encoding="utf-8")
            except OSError:
                file_text = None

        if file_text is None:
            if rel_path and rel_path not in unprovided_files:
                unprovided_files.append(rel_path)
            missing_blocks.append({"path": rel_path, "function": func_name, "reason": "file unreadable or missing"})
            continue

        func_block = extract_function_block(file_text, func_name) if func_name else file_text
        if func_block is None:
            missing_blocks.append({"path": rel_path, "function": func_name, "reason": f"function {func_name} not found"})
            continue

        block_bytes = len(func_block.encode("utf-8"))
        if total_bytes + block_bytes > capacity_limit_bytes:
            capacity_exceeded = True
            missing_blocks.append({"path": rel_path, "function": func_name, "reason": "capacity limit exceeded"})
            continue

        provided_blocks.append({
            "path": rel_path,
            "function": func_name,
            "code": func_block,
            "byte_len": block_bytes,
        })
        provided_files[rel_path] = str(base / rel_path) if base else rel_path
        total_bytes += block_bytes

        # Check dependency
        if isinstance(dep, dict):
            dep_path = dep.get("path") or rel_path
            dep_func = dep.get("function")
            dep_why = dep.get("reason") or dep_reason or "direct caller dependency"
            dependency_reasons[f"{rel_path}:{func_name} -> {dep_path}:{dep_func}"] = dep_why

            dep_file_text = None
            if base and dep_path:
                d_file = base / dep_path
                try:
                    if d_file.is_file():
                        dep_file_text = d_file.read_text(encoding="utf-8")
                except OSError:
                    dep_file_text = None

            if dep_file_text is None:
                if dep_path and dep_path not in unprovided_files:
                    unprovided_files.append(dep_path)
                unreviewed_dependencies.append({
                    "path": dep_path,
                    "function": dep_func,
                    "target": f"{rel_path}:{func_name}",
                    "reason": dep_why,
                    "status": "dependency_file_missing",
                })
                continue

            dep_block = extract_function_block(dep_file_text, dep_func) if dep_func else dep_file_text
            if dep_block is None:
                unreviewed_dependencies.append({
                    "path": dep_path,
                    "function": dep_func,
                    "target": f"{rel_path}:{func_name}",
                    "reason": dep_why,
                    "status": "dependency_function_missing",
                })
                continue

            dep_bytes = len(dep_block.encode("utf-8"))
            if total_bytes + dep_bytes > capacity_limit_bytes:
                capacity_exceeded = True
                unreviewed_dependencies.append({
                    "path": dep_path,
                    "function": dep_func,
                    "target": f"{rel_path}:{func_name}",
                    "reason": dep_why,
                    "status": "capacity_exceeded",
                })
                continue

            provided_blocks.append({
                "path": dep_path,
                "function": dep_func,
                "code": dep_block,
                "byte_len": dep_bytes,
                "is_dependency_for": f"{rel_path}:{func_name}",
            })
            provided_files[dep_path] = str(base / dep_path) if base else dep_path
            total_bytes += dep_bytes

    is_blocked = bool(unprovided_files or missing_blocks or unreviewed_dependencies or capacity_exceeded)
    review_status = "review_blocked" if is_blocked else "complete"

    return {
        "review_status": review_status,
        "is_blocked": is_blocked,
        "provided_files": provided_files,
        "provided_blocks": provided_blocks,
        "unprovided_files": unprovided_files,
        "missing_blocks": missing_blocks,
        "unreviewed_dependencies": unreviewed_dependencies,
        "dependency_reasons": dependency_reasons,
        "capacity_limit_bytes": capacity_limit_bytes,
        "total_bytes": total_bytes,
        "capacity_exceeded": capacity_exceeded,
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


def build_complete_delta(*, baseline, current, binding, evidence_root, capacity_bytes=100_000):
    """Exact byte-edit supply, bound to task and BOTH endpoint digests.
    Validate reconstruction of every changed path; never flip the old diff flag.
    """
    import difflib
    if type(capacity_bytes) is not int or capacity_bytes <= 0 or not isinstance(binding, dict) or not binding.get('task_id'):
        return {'complete': False, 'reason': 'invalid capacity or current task binding', 'binding': binding}
    if '\x00scan_incomplete' in baseline or '\x00scan_incomplete' in current:
        return {'complete': False, 'reason': 'workspace snapshot incomplete', 'binding': binding}
    root = Path(evidence_root)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for rel in sorted(set(baseline) | set(current)):
        before, after = baseline.get(rel), current.get(rel)
        if before and after and before.get("sha256") == after.get("sha256"):
            continue
        try:
            old = bytes.fromhex(before["raw_hex"]) if before else b""
            new = bytes.fromhex(after["raw_hex"]) if after else b""
            if before and hashlib.sha256(old).hexdigest() != before["sha256"]:
                raise ValueError("before hash mismatch")
            if after and hashlib.sha256(new).hexdigest() != after["sha256"]:
                raise ValueError("after hash mismatch")
        except (KeyError, ValueError, TypeError):
            return {"complete": False, "reason": "endpoint bytes missing or wrong hash", "path": rel, "binding": binding}
        refs = {}
        for side, data in (("before", old), ("after", new)):
            digest = hashlib.sha256(data).hexdigest()
            path = root / (digest + ".bin")
            try:
                with path.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError("endpoint evidence changed")
            refs[side] = {"path": str(path), "sha256": digest, "bytes": len(data)}
        # Byte-level matching has pathological quadratic costs for large repeated
        # sources; use line chunks preserving exact original bytes instead.
        a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
        ops = []
        rebuilt = []
        for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                rebuilt.extend(a[i:j])
            else:
                ops.append({"before_line_start": i, "before_line_end": j,
                            "before_hex": b"".join(a[i:j]).hex(), "after_hex": b"".join(b[k:l]).hex()})
                rebuilt.extend(b[k:l])
        if b"".join(rebuilt) != new:
            raise ValueError("delta does not reconstruct candidate")
        rows.append({"path": rel, "before": refs["before"], "after": refs["after"],
                     "before_exists": before is not None, "after_exists": after is not None, "edits": ops})
    packet = {"complete": True, "binding": binding, "paths": rows,
              "coverage": "all changed paths; every changed byte reconstructs endpoint"}
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(raw) > capacity_bytes:
        return {"complete": False, "reason": "complete delta over capacity", "required_bytes": len(raw), "capacity_bytes": capacity_bytes, "binding": binding}
    packet["logical_bytes"] = len(raw)
    packet["logical_sha256"] = hashlib.sha256(raw).hexdigest()
    return packet


def build_snapshot_material_packet(*,current,capacity_limit_bytes=100_000):
    """Supply complete verified in-scope snapshot, not truncated function hints."""
    blocks=[];missing=[];total=0
    if type(capacity_limit_bytes) is not int or capacity_limit_bytes<=0:
        missing.append({'reason':'invalid capacity'})
    if not current or '\x00scan_incomplete' in current:
        missing.append({'reason':'empty or incomplete snapshot'})
    for rel,row in sorted(current.items()):
        try:
            raw=bytes.fromhex(row['raw_hex'])
            if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('hash mismatch')
            text=raw.decode('utf-8')
            if total+len(raw)>capacity_limit_bytes:raise ValueError('capacity exceeded')
            blocks.append({'path':rel,'function':'__whole_file__','code':text,'byte_len':len(raw),'sha256':row['sha256']});total+=len(raw)
        except (KeyError,ValueError,TypeError) as exc:
            missing.append({'path':rel,'reason':str(exc)})
    return {'review_status':'review_blocked' if missing else 'complete','is_blocked':bool(missing),'provided_blocks':blocks,'provided_files':{r['path']:r['path'] for r in blocks},'missing_blocks':missing,'unprovided_files':[r.get('path') for r in missing],'unreviewed_dependencies':[],'total_bytes':total,'coverage':'all snapshot files in full, including direct callers and unchanged dependencies','capacity_limit_bytes':capacity_limit_bytes}


def review_payload(
    *,
    changes: Mapping[str, Any],
    worker_response: str,
    rework_round: int,
    max_reworks: int,
    source_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    response = str(worker_response or "")
    if len(response) > MAX_WORKER_RESPONSE:
        response = response[:MAX_WORKER_RESPONSE] + " ..."
    payload: dict[str, Any] = {
        "review_source": "collab_post_run",
        "scope_rule": SCOPE_RULE,
        "rework_round": int(rework_round),
        "max_reworks": int(max_reworks),
        "changed_files": list(changes.get("changed_files") or []),
        "out_of_scope_changes": list(changes.get("out_of_scope_changes") or []),
        "diff": str(changes.get("diff") or ""),
        "worker_self_report": response,
    }
    if changes.get("diff_truncated"):
        payload["diff_truncated"] = True
        payload["truncated_files"] = list(changes.get("truncated_files") or [])
        if "truncation_metrics" in changes:
            payload["truncation_metrics"] = list(changes["truncation_metrics"])
        # Diff truncation constitutes incomplete material -> review_blocked
        payload["review_status"] = "review_blocked"

    if source_packet is not None:
        payload["source_packet"] = dict(source_packet)
        if source_packet.get("is_blocked"):
            payload["review_status"] = "review_blocked"
            payload["unreviewed_dependencies"] = list(source_packet.get("unreviewed_dependencies") or [])
            payload["unprovided_files"] = list(source_packet.get("unprovided_files") or [])
            payload["missing_blocks"] = list(source_packet.get("missing_blocks") or [])
    return payload


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
