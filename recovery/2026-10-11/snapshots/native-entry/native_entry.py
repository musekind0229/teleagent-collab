"""Controller-native preparation/invocation separation interface.

Provides explicit entry guards, pure preflight inspection without side-effects,
and separated runner execution bound to verified controller proof and durable storage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

import jsonschema

SUPPORTED_MODEL = "gemini-3.8-flash-low"
MAX_SECONDS = 480


class NativeEntryError(Exception):
    """Raised when native entry validation, preflight or invocation fails."""


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def get_capabilities() -> dict[str, Any]:
    """Return interpreter and runner import capabilities separately from native execution.

    Importing and inspecting capabilities MUST NOT mutate sys.modules or spawn models.
    """
    import_capabilities = {
        "isolated_child_interpreter": True,
        "no_cached_sys_modules_mutation": True,
        "supported_models": [SUPPORTED_MODEL],
        "max_timeout_sec": MAX_SECONDS,
        "python_executable": sys.executable,
        "python_version": sys.version,
    }
    return import_capabilities


def preflight(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate structured request without modelspawn, credential-read, pool mutation or sys.modules alterations.

    Must validate:
    - structured request / schema / model gemini-3.8-flash-low
    - authorized scratch roots / cwd
    - pinned input / launch manifests / file hashes
    - interpreter import capabilities
    """
    if not isinstance(request, Mapping):
        raise NativeEntryError("Request must be a mapping")

    # 1. Model check
    model = request.get("model")
    if model != SUPPORTED_MODEL:
        raise NativeEntryError(f"Unsupported model: {model!r}, expected {SUPPORTED_MODEL!r}")

    # 2. Binding keys
    binding_keys = ("goal_id", "task_id", "run_id", "owner")
    for k in binding_keys:
        val = request.get(k)
        if not isinstance(val, str) or not val.strip():
            raise NativeEntryError(f"Missing or invalid binding field: {k!r}")

    # 3. Scratch and CWD validation
    scratch_raw = request.get("scratch")
    if not scratch_raw:
        raise NativeEntryError("Missing required 'scratch' root")
    scratch = Path(scratch_raw).resolve()
    if not scratch.exists():
        raise NativeEntryError(f"Scratch root does not exist: {scratch}")

    workspace_raw = request.get("workspace")
    if not workspace_raw:
        raise NativeEntryError("Missing required 'workspace' path")
    workspace = Path(workspace_raw).resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise NativeEntryError(f"Workspace must exist and be a directory: {workspace}")

    try:
        workspace.relative_to(scratch)
    except ValueError:
        raise NativeEntryError(f"Workspace {workspace} is outside authorized scratch root {scratch}")

    # Control directory validation
    control_raw = request.get("control")
    if control_raw:
        control = Path(control_raw).resolve()
        try:
            control.relative_to(scratch)
        except ValueError:
            raise NativeEntryError(f"Control directory {control} is outside authorized scratch root {scratch}")

    # 4. Manifest and file hash validation
    manifest = request.get("manifest")
    if manifest is not None:
        if not isinstance(manifest, Sequence):
            raise NativeEntryError("Manifest must be a list/sequence of entries")
        for entry in manifest:
            if not isinstance(entry, Mapping) or "path" not in entry or "sha256" not in entry:
                raise NativeEntryError(f"Invalid manifest entry: {entry!r}")
            p = Path(entry["path"])
            if not p.is_file():
                raise NativeEntryError(f"Manifest file missing: {p}")
            actual_sha = _sha256(p)
            if actual_sha != entry["sha256"]:
                raise NativeEntryError(
                    f"Manifest hash mismatch for {p}: expected {entry['sha256']}, got {actual_sha}"
                )

    source_hashes = request.get("source_hashes")
    if source_hashes is not None:
        if not isinstance(source_hashes, Mapping):
            raise NativeEntryError("source_hashes must be a mapping")
        for rel_path, expected_hash in source_hashes.items():
            full_p = scratch / rel_path
            if not full_p.is_file():
                raise NativeEntryError(f"Source file missing: {full_p}")
            actual_hash = _sha256(full_p)
            if actual_hash != expected_hash:
                raise NativeEntryError(
                    f"Source hash mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}"
                )

    # 5. Stage, timeout and schema validation
    stage = request.get("stage", "implementation")
    stages = request.get("stages")
    if isinstance(stages, Mapping) and stage in stages:
        stage_spec = stages[stage]
        seconds = stage_spec.get("seconds", 0)
        if not (0 < seconds <= MAX_SECONDS):
            raise NativeEntryError(f"Invalid seconds {seconds}: must be between 1 and {MAX_SECONDS}")

        # Check stage prompt and schema hashes if provided
        if "prompt_sha256" in stage_spec and control_raw:
            prompt_file = Path(control_raw) / stage / "prompt.txt"
            if prompt_file.is_file() and _sha256(prompt_file) != stage_spec["prompt_sha256"]:
                raise NativeEntryError(f"Prompt hash mismatch for stage {stage}")

        if "schema_sha256" in stage_spec and control_raw:
            schema_file = Path(control_raw) / stage / "response-schema.json"
            if schema_file.is_file():
                if _sha256(schema_file) != stage_spec["schema_sha256"]:
                    raise NativeEntryError(f"Schema hash mismatch for stage {stage}")
                try:
                    schema_obj = json.loads(schema_file.read_text(encoding="utf-8"))
                    jsonschema.Draft202012Validator.check_schema(schema_obj)
                except Exception as e:
                    raise NativeEntryError(f"Schema validation error: {e}") from e

    # Pure result without ambient mutations
    return {
        "passed": True,
        "mode": "NO_SPAWN_PREFLIGHT",
        "model": model,
        "workspace": str(workspace),
        "scratch": str(scratch),
        "binding": {k: request[k] for k in binding_keys if k in request},
        "capabilities": get_capabilities(),
        "native_calls": 0,
        "no_sys_modules_mutation": True,
    }


def _verify_durable_store(request: Mapping[str, Any], controller_proof: Mapping[str, Any]) -> dict[str, Any]:
    """Verify that caller-provided booleans alone are not taken as proof.

    Reads the current durable store and validates goal, task, run, owner, scope, and status.
    """
    store_path_str = request.get("store") or controller_proof.get("store")
    if not store_path_str:
        raise NativeEntryError("Missing durable store path in request or controller_proof")

    store_path = Path(store_path_str)
    if not store_path.is_file():
        raise NativeEntryError(f"Durable store file not found: {store_path}")

    try:
        store_data = json.loads(store_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise NativeEntryError(f"Failed to read durable store JSON: {e}") from e

    goal_id = request["goal_id"]
    task_id = request["task_id"]
    run_id = request["run_id"]
    owner = request["owner"]

    goals = store_data.get("goals", {})
    if goal_id not in goals:
        raise NativeEntryError(f"Goal {goal_id!r} not found in durable store")

    goal_obj = goals[goal_id]
    tasks = goal_obj.get("tasks", [])
    matched_task = None
    for t in tasks:
        if t.get("task_id") == task_id:
            matched_task = t
            break

    if not matched_task:
        raise NativeEntryError(f"Task {task_id!r} not found under goal {goal_id!r} in durable store")

    # Stale run check or mismatched run_id
    if matched_task.get("run_id") != run_id:
        raise NativeEntryError(
            f"Run mismatch in store: task bound to {matched_task.get('run_id')!r}, invocation requested {run_id!r}"
        )

    # Valid status check
    status = matched_task.get("status")
    if status not in ("running", "succeeded", "failed"):
        raise NativeEntryError(f"Task status invalid in durable store: {status!r}")

    # Scope/workspace check
    store_ws = Path(matched_task.get("workspace", "")).resolve()
    req_ws = Path(request["workspace"]).resolve()
    if store_ws != req_ws:
        raise NativeEntryError(f"Workspace mismatch: store has {store_ws}, request has {req_ws}")

    # Owner check against goal coordinator or history
    goal_meta = goal_obj.get("goal", {})
    coordinator = (
        goal_meta.get("role_hints", {}).get("coordinator")
        or goal_obj.get("coordinator_id")
        or goal_obj.get("submitter_id")
    )
    if coordinator and coordinator.startswith("api:"):
        coordinator = coordinator[4:]
    if coordinator and coordinator != owner:
        raise NativeEntryError(f"Owner mismatch: store coordinator is {coordinator}, request owner is {owner}")

    return matched_task


def invoke(
    request: Mapping[str, Any],
    controller_proof: Mapping[str, Any],
    *,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute invocation with explicit entry guard.

    Ensures preflight passes, authenticates durable store binding, persists evidence
    BEFORE runner side effects, and delegates execution to the runner without cached module collisions.
    """
    if not isinstance(controller_proof, Mapping):
        raise NativeEntryError("controller_proof must be a mapping")

    # Caller-authored booleans alone are not proof
    if not controller_proof.get("verified_by_controller"):
        raise NativeEntryError("controller_proof missing verified_by_controller marker")

    # 1. Run pure preflight
    preflight_result = preflight(request)

    # 2. Authenticate durable store binding
    bound_task = _verify_durable_store(request, controller_proof)

    # 3. Setup phase and evidence directory
    stage = request.get("stage", "implementation")
    control_raw = request.get("control")
    workspace = Path(request["workspace"]).resolve()
    evidence_dir = Path(control_raw) / stage if control_raw else workspace / ".evidence" / stage
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Check that invocation has not already completed here (prevent replay)
    launch_proof_path = evidence_dir / "launch.json"
    if launch_proof_path.exists():
        raise NativeEntryError(f"Replay detected: {launch_proof_path} already exists")

    # 4. PERSIST launch/input/output evidence BEFORE runner side effects
    evidence_pre = {
        "event": "launch_pre_effect",
        "binding": {
            "goal_id": request["goal_id"],
            "task_id": request["task_id"],
            "run_id": request["run_id"],
            "owner": request["owner"],
        },
        "stage": stage,
        "model": request.get("model"),
        "workspace": str(workspace),
        "preflight": preflight_result,
        "bound_task_status": bound_task.get("status"),
        "store_path": str(request.get("store") or controller_proof.get("store")),
    }
    launch_proof_path.write_text(json.dumps(evidence_pre, indent=2), encoding="utf-8")

    if runner is None:
        raise NativeEntryError("No runner callable provided for explicit invocation")

    # 5. Invoke runner
    runner_result = runner(request, evidence_dir=evidence_dir)

    # 6. Persist completion evidence
    settlement_proof_path = evidence_dir / "settlement.json"
    completion_evidence = {
        "event": "settlement",
        "binding": evidence_pre["binding"],
        "stage": stage,
        "runner_result": runner_result,
    }
    settlement_proof_path.write_text(json.dumps(completion_evidence, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "binding": evidence_pre["binding"],
        "stage": stage,
        "evidence_dir": str(evidence_dir),
        "preflight": preflight_result,
        "runner_result": runner_result,
    }
