"""Local application facade: external request -> lead plan -> worker tasks.

The caller only speaks in Goals. It does not address a lead or worker. The
coordinator owns those implementation details and persists public state in
``DurableLayer``. The first release intentionally defaults to the deterministic
in-process backend; Windows TeleAgent can be selected after its live auth path
passes acceptance.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from execution_backend.base import ExecutionBackend
from execution_backend.inprocess_v1 import InProcessExecutionBackend
from framework.durable_api import DurableLayer
from lead_adapter.schema import context_summary_of, unwrap_structured

API_VERSION = "collab-app.v0.1"
MAX_BODY_BYTES = 1024 * 1024
MAX_PLAN_TASKS = 8


class AppError(ValueError):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request") -> None:
        self.status = int(status)
        self.code = code
        super().__init__(message)


class GoalPlanner(Protocol):
    name: str

    def plan(self, goal_snapshot: Mapping[str, Any]) -> dict[str, Any]: ...


def _strings(value: Any, *, field: str, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise AppError(f"{field} is required")
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise AppError(f"{field} must be an array of non-empty strings")
    return [x.strip() for x in value]


def _safe_artifacts(value: Any) -> list[str]:
    artifacts = _strings(value, field="acceptance.artifacts", required=True)
    out: list[str] = []
    for raw in artifacts:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise AppError(f"artifact must be a relative path inside the task workspace: {raw!r}")
        out.append(path.as_posix())
    return out


def _task_id(goal_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{goal_id}\0{key}".encode("utf-8")).hexdigest()[:16]
    return f"task_{digest}"


def planning_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["application_id", "context_summary", "summary", "tasks"],
        "properties": {
            "application_id": {"type": "string"},
            "context_summary": {"type": "string"},
            "summary": {"type": "string"},
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PLAN_TASKS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_key", "title", "instruction", "depends_on", "artifacts"],
                    "properties": {
                        "task_key": {"type": "string"},
                        "title": {"type": "string"},
                        "instruction": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "artifacts": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }


def validate_plan(plan: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise AppError("lead did not return a plan object", code="invalid_plan")
    if str(plan.get("application_id") or "") != str(request.get("application_id") or ""):
        raise AppError("lead plan application_id mismatch", code="stale_plan")
    if str(plan.get("context_summary") or "") != str(request.get("context_summary") or ""):
        raise AppError("lead plan context_summary mismatch", code="stale_plan")
    rows = plan.get("tasks")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PLAN_TASKS:
        raise AppError(f"lead plan must contain 1..{MAX_PLAN_TASKS} tasks", code="invalid_plan")
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise AppError("plan task must be an object", code="invalid_plan")
        key = str(row.get("task_key") or "").strip()
        title = str(row.get("title") or "").strip()
        instruction = str(row.get("instruction") or "").strip()
        if not key or key in seen or not title or not instruction:
            raise AppError("plan task keys must be unique and task fields non-empty", code="invalid_plan")
        seen.add(key)
        clean.append(
            {
                "task_key": key,
                "title": title,
                "instruction": instruction,
                "depends_on": _strings(row.get("depends_on") or [], field="task.depends_on"),
                "artifacts": _safe_artifacts(row.get("artifacts")),
            }
        )
    for row in clean:
        if any(dep not in seen or dep == row["task_key"] for dep in row["depends_on"]):
            raise AppError("task dependency references an unknown/self task", code="invalid_plan")
    return {
        "application_id": str(plan["application_id"]),
        "context_summary": str(plan["context_summary"]),
        "summary": str(plan.get("summary") or "").strip(),
        "tasks": clean,
    }


class DeterministicPlanner:
    """Safe bootstrap planner: one bounded implementation task per Goal."""

    name = "deterministic.single_task"

    def plan(self, goal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        goal = goal_snapshot.get("goal") if isinstance(goal_snapshot.get("goal"), Mapping) else {}
        acceptance = goal.get("acceptance") if isinstance(goal.get("acceptance"), Mapping) else {}
        artifacts = _safe_artifacts(acceptance.get("artifacts"))
        request = build_planning_request(goal_snapshot)
        raw = {
            "application_id": request["application_id"],
            "context_summary": request["context_summary"],
            "summary": "Single bounded task generated by the bootstrap planner",
            "tasks": [
                {
                    "task_key": "implement",
                    "title": str(goal.get("title") or "implementation"),
                    "instruction": str(goal.get("desired_outcome") or "Complete the requested outcome"),
                    "depends_on": [],
                    "artifacts": artifacts,
                }
            ],
        }
        return validate_plan(raw, request)


class LeadAdapterPlanner:
    """Use any existing LeadAdapter to turn a Goal into a bounded task graph."""

    name = "lead_adapter.plan_v1"

    def __init__(self, adapter: Any, *, cwd: str | Path, timeout_sec: float = 180) -> None:
        self.adapter = adapter
        self.cwd = str(Path(cwd).resolve())
        self.timeout_sec = float(timeout_sec)

    def plan(self, goal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        request = build_planning_request(goal_snapshot)
        _raw, parsed = self.adapter.decide(
            request,
            schema=planning_response_schema(),
            cwd=self.cwd,
            timeout_sec=self.timeout_sec,
        )
        parsed = unwrap_structured(parsed)
        if isinstance(parsed, Mapping) and parsed.get("_lead_status"):
            raise AppError(
                f"lead planning failed: {parsed.get('error') or parsed.get('_lead_status')}",
                status=503,
                code="lead_unavailable",
            )
        return validate_plan(parsed, request)


def build_planning_request(goal_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    goal = goal_snapshot.get("goal") if isinstance(goal_snapshot.get("goal"), Mapping) else {}
    boundaries = goal.get("boundaries") if isinstance(goal.get("boundaries"), Mapping) else {}
    body = {
        "protocol": "collab-plan-v1",
        "kind": "plan",
        "application_id": f"plan_{goal_snapshot.get('goal_id') or uuid.uuid4().hex}",
        "task_goal": str(goal.get("desired_outcome") or ""),
        "authorized_scope": list(boundaries.get("must") or []),
        "prohibitions": list(boundaries.get("must_not") or []),
        "acceptance_criteria": dict(goal.get("acceptance") or {}),
        "budget": dict(goal.get("budget") or {}),
        "max_tasks": MAX_PLAN_TASKS,
        "current_application": {"goal_id": goal_snapshot.get("goal_id"), "task_count": 0},
        "issued_at": time.time(),
    }
    body["context_summary"] = context_summary_of(body)
    return body


class AppCoordinator:
    """Plan Goals and dispatch ready Tasks through one ExecutionBackend."""

    def __init__(
        self,
        layer: DurableLayer,
        *,
        planner: GoalPlanner | None = None,
        backend: ExecutionBackend | None = None,
        workspaces_root: str | Path,
    ) -> None:
        self.layer = layer
        self.planner = planner or DeterministicPlanner()
        self.backend = backend or InProcessExecutionBackend()
        self.workspaces_root = Path(workspaces_root)
        # HTTP ticks and the background loop may fire together.  Serialize
        # coordination so a queued Task is dispatched at most once by this
        # service instance.
        self._lock = threading.RLock()

    def process_all(self) -> dict[str, Any]:
        with self._lock:
            rows = self.layer.list_goals().get("goals") or []
            outcomes = [self._process_goal(str(row.get("goal_id") or "")) for row in rows]
            return {"ok": all(x.get("ok") for x in outcomes), "processed": outcomes}

    def process_goal(self, goal_id: str) -> dict[str, Any]:
        with self._lock:
            return self._process_goal(goal_id)

    def _process_goal(self, goal_id: str) -> dict[str, Any]:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            return current
        snap = current["goal"]
        if snap.get("state") in {"completed", "failed", "cancelled", "cancel_requested"}:
            return {"ok": True, "goal_id": goal_id, "state": snap.get("state"), "action": "terminal"}
        tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]
        if not tasks:
            try:
                plan = self.planner.plan(snap)
            except Exception as e:
                failed = self.layer.fail_goal(
                    goal_id,
                    phase="planning",
                    error=f"planner failed: {type(e).__name__}",
                )
                return {**failed, "action": "planning_failed"}
            key_to_id = {row["task_key"]: _task_id(goal_id, row["task_key"]) for row in plan["tasks"]}
            for row in plan["tasks"]:
                added = self.layer.add_child_task(
                    goal_id,
                    task={
                        "task_id": key_to_id[row["task_key"]],
                        "title": row["title"],
                        "status": "queued",
                        "depends_on": [key_to_id[x] for x in row["depends_on"]],
                        "inputs": {
                            "instruction": row["instruction"],
                            "planner": self.planner.name,
                        },
                        "expected_artifacts": row["artifacts"],
                        "done_when": {"artifacts": row["artifacts"]},
                        "assignee_role": "executor",
                        "backend_requirement": getattr(self.backend, "backend_id", ""),
                    },
                )
                if not added.get("ok"):
                    return added
            current = self.layer.get_goal(goal_id)
            snap = current["goal"]
            tasks = [dict(t) for t in (snap.get("tasks") or []) if isinstance(t, Mapping)]

        # A real worker backend is normally asynchronous.  Resume polling a
        # persisted Run before considering new queued work.
        for task in tasks:
            if task.get("status") != "running":
                continue
            run_id = str(task.get("run_id") or "").strip()
            if not run_id:
                return self.layer.finish_task(
                    goal_id,
                    str(task["task_id"]),
                    succeeded=False,
                    result={"ok": False, "error": "running task has no persisted run handle"},
                )
            try:
                observation = self.backend.observe_run(run_id)
                if observation.get("busy"):
                    return {"ok": True, "goal_id": goal_id, "state": "running", "action": "worker_running"}
                result = self.backend.collect_result(run_id)
            except Exception as e:
                result = {
                    "ok": False,
                    "run_id": run_id,
                    "error": f"backend resume failed: {type(e).__name__}",
                }
            finished = self.layer.finish_task(
                goal_id,
                str(task["task_id"]),
                succeeded=bool(result.get("ok")),
                result=result,
            )
            return {**finished, "action": "task_finished"}

        status_by_id = {str(t.get("task_id")): str(t.get("status")) for t in tasks}
        for task in tasks:
            if task.get("status") != "queued":
                continue
            deps = [str(x) for x in (task.get("depends_on") or [])]
            if any(status_by_id.get(dep) == "failed" for dep in deps):
                continue
            if any(status_by_id.get(dep) != "succeeded" for dep in deps):
                continue
            started = self.layer.start_task(goal_id, str(task["task_id"]))
            if not started.get("ok"):
                return started
            root = self.workspaces_root / goal_id / str(task["task_id"])
            root.mkdir(parents=True, exist_ok=True)
            try:
                launched = self.backend.start_run(
                    title=str(task.get("title") or task["task_id"]),
                    directory=str(root),
                    instruction=str((task.get("inputs") or {}).get("instruction") or ""),
                    artifacts=[str(x) for x in (task.get("expected_artifacts") or [])],
                    charter={
                        "goal": ((snap.get("goal") or {}).get("desired_outcome") if isinstance(snap.get("goal"), Mapping) else ""),
                        "must": (((snap.get("goal") or {}).get("boundaries") or {}).get("must") if isinstance(snap.get("goal"), Mapping) else []),
                        "must_not": (((snap.get("goal") or {}).get("boundaries") or {}).get("must_not") if isinstance(snap.get("goal"), Mapping) else []),
                        "done_when": task.get("done_when") or {},
                    },
                )
            except Exception as e:
                launched = {"ok": False, "error": f"backend dispatch failed: {type(e).__name__}"}
            run_id = str(launched.get("run_id") or launched.get("native_handle") or "")
            if not launched.get("ok") or not run_id:
                return self.layer.finish_task(goal_id, str(task["task_id"]), succeeded=False, result=launched)
            bound = self.layer.bind_task_run(
                goal_id,
                str(task["task_id"]),
                run_id=run_id,
                native_handle=str(launched.get("native_handle") or ""),
                backend=str(launched.get("backend") or getattr(self.backend, "backend_id", "")),
            )
            if not bound.get("ok"):
                # The Run may already exist, so fail closed and never launch a
                # replacement.  The returned binding error remains durable.
                return bound
            try:
                observation = self.backend.observe_run(run_id)
                if observation.get("busy"):
                    return {"ok": True, "goal_id": goal_id, "state": "running", "action": "worker_running"}
                result = self.backend.collect_result(run_id)
            except Exception as e:
                result = {
                    "ok": False,
                    "run_id": run_id,
                    "error": f"backend observation failed: {type(e).__name__}",
                }
            finished = self.layer.finish_task(
                goal_id,
                str(task["task_id"]),
                succeeded=bool(result.get("ok")),
                result=result,
            )
            return {**finished, "action": "task_finished"}
        return {"ok": True, "goal_id": goal_id, "state": snap.get("state"), "action": "waiting"}


class CollabApplication:
    def __init__(
        self,
        persist_root: str | Path,
        *,
        planner: GoalPlanner | None = None,
        backend: ExecutionBackend | None = None,
        coordinator_id: str = "app-coordinator",
    ) -> None:
        self.persist_root = Path(persist_root)
        self.layer = DurableLayer.open(self.persist_root, use_cache=False)
        self.coordinator_id = coordinator_id
        self.coordinator = AppCoordinator(
            self.layer,
            planner=planner,
            backend=backend,
            workspaces_root=self.persist_root / "workspaces",
        )

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AppError("request body must be a JSON object")
        goal_text = str(payload.get("goal") or payload.get("desired_outcome") or "").strip()
        if not goal_text:
            raise AppError("goal is required")
        acceptance = payload.get("acceptance")
        if not isinstance(acceptance, Mapping):
            raise AppError("acceptance object is required")
        acceptance_obj = dict(acceptance)
        acceptance_obj["artifacts"] = _safe_artifacts(acceptance.get("artifacts"))
        boundaries = payload.get("boundaries") if isinstance(payload.get("boundaries"), Mapping) else {}
        must = _strings(boundaries.get("must") or payload.get("must") or [], field="boundaries.must")
        must_not = _strings(boundaries.get("must_not") or payload.get("must_not") or [], field="boundaries.must_not")
        must_not = must_not or [
            "Do not access credentials or account secrets",
            "Do not modify system settings or install software",
            "Do not write outside the assigned task workspace",
        ]
        budget = payload.get("budget") if isinstance(payload.get("budget"), Mapping) else {"wall_sec": 300, "max_reworks": 1}
        submit_key = str(payload.get("idempotency_key") or payload.get("request_id") or "").strip()
        if not submit_key:
            submit_key = f"req_{uuid.uuid4().hex}"
        client_id = str(payload.get("client_id") or "local-api").strip() or "local-api"
        title = str(payload.get("title") or goal_text[:80]).strip()
        goal = {
            "title": title,
            "desired_outcome": goal_text,
            "boundaries": {"must": must, "must_not": must_not},
            "acceptance": acceptance_obj,
            "budget": dict(budget),
            "platform_allowlist": [str(x) for x in (payload.get("platform_allowlist") or ["windows"])],
            "role_hints": {"entrypoint": API_VERSION},
        }
        result = self.layer.submit_goal(
            submit_key=submit_key,
            title=title,
            desired_outcome=goal_text,
            goal=goal,
            submitter_id=f"api:{client_id}",
            external_goal_ref=str(payload.get("external_goal_ref") or submit_key),
            autonomy=payload.get("autonomy") or "bounded_autonomy",
            coordinator_id=self.coordinator_id,
        )
        if not result.get("ok"):
            status = 409 if result.get("reason") == "submit_content_conflict" else 400
            raise AppError(str(result.get("error") or result.get("reason")), status=status, code=str(result.get("reason") or "submit_failed"))
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": result["goal_id"],
            "goal_id": result["goal_id"],
            "state": result["goal"].get("state"),
            "created": bool(result.get("created")),
            "duplicate": bool(result.get("duplicate")),
            "status_url": f"/v1/requests/{result['goal_id']}",
        }

    def status(self, goal_id: str) -> dict[str, Any]:
        result = self.layer.get_goal(goal_id)
        if not result.get("ok"):
            raise AppError(str(result.get("error")), status=404, code="not_found")
        snap = result["goal"]
        return {
            "ok": True,
            "api_version": API_VERSION,
            "request_id": goal_id,
            "state": snap.get("state"),
            "goal": snap.get("goal"),
            "tasks": snap.get("tasks") or [],
            "pending_decisions": snap.get("pending_decisions") or [],
            "failure": snap.get("failure"),
            "updated_at": snap.get("updated_at_iso"),
        }

    def list_requests(self) -> dict[str, Any]:
        out = self.layer.list_goals()
        return {"ok": True, "api_version": API_VERSION, "requests": out.get("goals") or []}

    def events(self, goal_id: str) -> dict[str, Any]:
        out = self.layer.list_events(goal_id)
        if not out.get("ok"):
            raise AppError(str(out.get("error")), status=404, code="not_found")
        return out

    def report(self, goal_id: str) -> dict[str, Any]:
        out = self.layer.get_report(goal_id)
        if not out.get("ok") and out.get("reason") == "unknown_goal":
            raise AppError(str(out.get("error")), status=404, code="not_found")
        return out

    def cancel(self, goal_id: str, reason: str = "") -> dict[str, Any]:
        out = self.layer.cancel_goal(goal_id, reason=reason)
        if not out.get("ok"):
            raise AppError(str(out.get("error") or out.get("reason")), status=409, code=str(out.get("reason")))
        # If no task remains in flight, cancellation can be confirmed immediately.
        # For asynchronous backends, confirm every backend cancellation before
        # changing cancel_requested into the terminal cancelled state.
        snap = self.layer.get_goal(goal_id).get("goal") or {}
        active = [
            t
            for t in (snap.get("tasks") or [])
            if isinstance(t, Mapping)
            and t.get("status") in {"running", "cancel_requested", "awaiting_decision", "review"}
        ]
        if not active:
            return self.layer.effect_cancel(goal_id, reason=reason)
        cancelled_runs: list[dict[str, Any]] = []
        all_stopped = True
        for task in active:
            run_id = str(task.get("run_id") or "").strip()
            if not run_id:
                all_stopped = False
                cancelled_runs.append({"ok": False, "task_id": task.get("task_id"), "error": "missing run_id"})
                continue
            try:
                code, body = self.coordinator.backend.cancel(run_id)
                ok = int(code) < 300 and isinstance(body, Mapping) and bool(body.get("ok"))
                cancelled_runs.append({"ok": ok, "task_id": task.get("task_id"), "run_id": run_id, "http": code})
                all_stopped = all_stopped and ok
            except Exception as e:
                all_stopped = False
                cancelled_runs.append(
                    {
                        "ok": False,
                        "task_id": task.get("task_id"),
                        "run_id": run_id,
                        "error": f"backend cancel failed: {type(e).__name__}",
                    }
                )
        if all_stopped:
            effected = self.layer.effect_cancel(goal_id, reason=reason)
            effected["backend_cancellations"] = cancelled_runs
            return effected
        return {**out, "backend_cancellations": cancelled_runs}

    def resolve(self, goal_id: str, decision_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        current = self.layer.get_goal(goal_id)
        if not current.get("ok"):
            raise AppError("unknown request", status=404, code="not_found")
        actor = str(current.get("submitter_id") or "")
        out = self.layer.resolve_decision(
            goal_id,
            decision_id=decision_id,
            verdict=str(payload.get("verdict") or ""),
            reason=str(payload.get("reason") or ""),
            actions=payload.get("actions"),
            extra={
                "actor_id": actor,
                "grant": payload.get("grant") if isinstance(payload.get("grant"), Mapping) else {},
            },
        )
        if not out.get("ok"):
            raise AppError(str(out.get("error") or out.get("reason")), status=409, code=str(out.get("reason")))
        return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "CollabApp/0.1"

    @property
    def app(self) -> CollabApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "api_token", "") or "")  # type: ignore[attr-defined]
        if not token:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as e:
            raise AppError("invalid Content-Length") from e
        if length <= 0 or length > MAX_BODY_BYTES:
            raise AppError("JSON body required and must be <= 1 MiB", status=413, code="body_size")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            raise AppError(f"invalid JSON: {e}") from e
        if not isinstance(value, dict):
            raise AppError("JSON body must be an object")
        return value

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _run(self) -> None:
        if not self._authorized() and self.path != "/health":
            self._send(401, {"ok": False, "error": "unauthorized", "code": "unauthorized"})
            return
        parsed = urlparse(self.path)
        # Goal ids may contain a Unicode title slug.  HTTP clients percent-
        # encode it, so decode each already-separated path segment before
        # looking up durable ids.
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if self.command == "GET" and parsed.path == "/health":
            self._send(200, {"ok": True, "api_version": API_VERSION})
            return
        if parts == ["v1", "requests"]:
            if self.command == "POST":
                self._send(202, self.app.submit(self._json_body()))
            elif self.command == "GET":
                self._send(200, self.app.list_requests())
            else:
                raise AppError("method not allowed", status=405, code="method_not_allowed")
            return
        if len(parts) >= 3 and parts[:2] == ["v1", "requests"]:
            gid = parts[2]
            if len(parts) == 3 and self.command == "GET":
                self._send(200, self.app.status(gid))
                return
            if len(parts) == 4 and parts[3] == "events" and self.command == "GET":
                self._send(200, self.app.events(gid))
                return
            if len(parts) == 4 and parts[3] == "report" and self.command == "GET":
                self._send(200, self.app.report(gid))
                return
            if len(parts) == 4 and parts[3] == "cancel" and self.command == "POST":
                body = self._json_body()
                self._send(200, self.app.cancel(gid, str(body.get("reason") or "")))
                return
            if len(parts) == 5 and parts[3] == "decisions" and self.command == "POST":
                self._send(200, self.app.resolve(gid, parts[4], self._json_body()))
                return
        if parts == ["v1", "coordinator", "tick"] and self.command == "POST":
            self._send(200, self.app.coordinator.process_all())
            return
        raise AppError("route not found", status=404, code="not_found")

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._run()
        except AppError as e:
            self._send(e.status, {"ok": False, "code": e.code, "error": str(e)})
        except Exception as e:  # fail closed without a traceback/body leak
            self._send(500, {"ok": False, "code": "internal_error", "error": type(e).__name__})

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()


class CollabHttpServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: CollabApplication, *, api_token: str = "") -> None:
        host = address[0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise AppError("v0.1 application API only binds loopback", code="non_loopback_refused")
        super().__init__(address, _Handler)
        self.app = app
        self.api_token = api_token


class CoordinatorLoop:
    def __init__(self, app: CollabApplication, *, interval: float = 0.5) -> None:
        self.app = app
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="collab-coordinator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.app.coordinator.process_all()
            except Exception:
                # Goal stays durable and will be retried on the next tick.
                continue


__all__ = [
    "API_VERSION",
    "AppCoordinator",
    "AppError",
    "CollabApplication",
    "CollabHttpServer",
    "CoordinatorLoop",
    "DeterministicPlanner",
    "GoalPlanner",
    "LeadAdapterPlanner",
    "build_planning_request",
    "planning_response_schema",
    "validate_plan",
]
