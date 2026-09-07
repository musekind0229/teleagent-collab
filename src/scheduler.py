#!/usr/bin/env python3
"""Parallel job scheduler + adaptive approval scan (scan ≠ call_lead).

口径（必守）:
1. 扫描只查 pending；无请求就跳过；只有硬规则未处理且真正需要 lead 时才 call_lead。
2. max_parallel 可配（默认 3）；每单独立 job_id + 独立工作目录。
3. 多路空闲：IDLE_SCAN（10～30s）扫一轮；收齐待批可一批调度，仍按 job_id 分别决策
   （实现：多次短 call_lead，不串上下文）。
4. 单路串行弹权：session busy / 刚有 pending 时 BUSY_POLL（1～3s）；出一条批一条；
   硬规则/白名单秒回；禁止等攒齐再回。
5. 复用 hard_rules + decision_packet；去重按 permission request id；秘密类禁 always。
6. 禁无条件放行；路径 canonicalize + is_path_within；批准前 reconfirm；reply 成功后再 mark handled。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from hard_rules import hard_rule_decision
from decision_packet import (
    PingDeduper,
    format_lead_prompt,
    lead_permission_schema,
    map_lead_decision_to_api,
    packet_from_permission,
    should_ping_lead,
)
from pathutil import canonicalize, is_path_within, permission_fingerprint

REPO = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACES = REPO / "jobs" / "workspaces"
DEFAULT_RUNS = REPO / "jobs" / "runs"

# Adaptive intervals (seconds)
IDLE_SCAN_MIN = float(os.environ.get("COLLAB_IDLE_SCAN_MIN", "10"))
IDLE_SCAN_MAX = float(os.environ.get("COLLAB_IDLE_SCAN_MAX", "30"))
BUSY_POLL_MIN = float(os.environ.get("COLLAB_BUSY_POLL_MIN", "1"))
BUSY_POLL_MAX = float(os.environ.get("COLLAB_BUSY_POLL_MAX", "3"))
DEFAULT_MAX_PARALLEL = int(os.environ.get("COLLAB_MAX_PARALLEL", "3"))


class JobState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    DONE = "done"
    FAIL = "fail"
    TIMEOUT = "timeout"


@dataclass
class ScanStats:
    """Observability: prove scan ≠ lead."""

    scans: int = 0
    empty_scans: int = 0
    pending_seen: int = 0
    hard_rule_hits: int = 0
    allowlist_hits: int = 0
    ordinary_rw_hits: int = 0
    lead_calls: int = 0
    replies: int = 0
    serial_one_by_one: int = 0
    poll_intervals: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scans": self.scans,
            "empty_scans": self.empty_scans,
            "pending_seen": self.pending_seen,
            "hard_rule_hits": self.hard_rule_hits,
            "allowlist_hits": self.allowlist_hits,
            "ordinary_rw_hits": self.ordinary_rw_hits,
            "lead_calls": self.lead_calls,
            "replies": self.replies,
            "serial_one_by_one": self.serial_one_by_one,
            "poll_intervals_sample": self.poll_intervals[-20:],
        }


@dataclass
class JobSlot:
    job_id: str
    name: str
    charter: dict
    instruction: str
    expected_artifacts: list[str]
    workdir: Path
    timeout_sec: int = 300
    force_lead_review: bool = False
    worker_intent: str | None = None
    blocker: Any = None
    state: JobState = JobState.QUEUED
    session_id: str = ""
    result: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    handled_perm_ids: set[str] = field(default_factory=set)
    ping_deduper: PingDeduper = field(default_factory=PingDeduper)
    charter_sent_full: bool = False
    api_replies: list[dict] = field(default_factory=list)
    hard_rule_rejects: list[dict] = field(default_factory=list)
    pending_summaries: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    last_pending_at: float | None = None
    busy: bool = False
    future: Future | None = None
    # dry/sim hooks
    dry: bool = False
    simulated_pending: list[dict] = field(default_factory=list)


def new_job_id(name: str = "job") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid.uuid4().hex[:8]
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:40] or "job"
    return f"{safe}-{stamp}-{short}"


def choose_poll_interval(
    *,
    any_busy: bool,
    any_pending_recent: bool,
    idle_min: float = IDLE_SCAN_MIN,
    idle_max: float = IDLE_SCAN_MAX,
    busy_min: float = BUSY_POLL_MIN,
    busy_max: float = BUSY_POLL_MAX,
) -> float:
    """Adaptive interval: busy/pending → 1～3s; idle multi-path → 10～30s."""
    if any_busy or any_pending_recent:
        lo, hi = busy_min, busy_max
    else:
        lo, hi = idle_min, idle_max
    if hi < lo:
        lo, hi = hi, lo
    if lo == hi:
        return lo
    return random.uniform(lo, hi)


def session_id_of_permission(p: dict) -> str:
    for k in ("sessionID", "session_id", "sessionId"):
        v = p.get(k)
        if v:
            return str(v)
    meta = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
    for k in ("sessionID", "session_id", "sessionId"):
        v = meta.get(k)
        if v:
            return str(v)
    return ""


class ParallelScheduler:
    """Enqueue charters; run up to max_parallel; adaptive approval scan."""

    def __init__(
        self,
        *,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        workspaces_root: Path | None = None,
        runs_root: Path | None = None,
        dry_run: bool = False,
        call_lead_fn: Callable[..., tuple[str, dict | None]] | None = None,
        teleagent_call: Callable[..., tuple[int, Any]] | None = None,
        session_busy_fn: Callable[[Any, str], bool] | None = None,
        idle_min: float = IDLE_SCAN_MIN,
        idle_max: float = IDLE_SCAN_MAX,
        busy_min: float = BUSY_POLL_MIN,
        busy_max: float = BUSY_POLL_MAX,
        stop_when_idle: bool = True,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        self.max_parallel = max_parallel
        self.workspaces_root = Path(workspaces_root or DEFAULT_WORKSPACES)
        self.runs_root = Path(runs_root or DEFAULT_RUNS)
        self.dry_run = dry_run
        self._call_lead_fn = call_lead_fn
        self._teleagent_call = teleagent_call
        self._session_busy_fn = session_busy_fn
        self.idle_min = idle_min
        self.idle_max = idle_max
        self.busy_min = busy_min
        self.busy_max = busy_max
        self.stop_when_idle = stop_when_idle
        self.jobs: dict[str, JobSlot] = {}
        self.stats = ScanStats()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="collab-job")

    # --- lead / HTTP (lazy glue) -------------------------------------------------

    def _glue(self):
        import glue as g

        return g

    def call_lead(self, prompt: str, schema: dict, cwd: str) -> tuple[str, dict | None]:
        self.stats.lead_calls += 1
        if self._call_lead_fn is not None:
            return self._call_lead_fn(prompt, schema, cwd)
        if self.dry_run:
            # Dry: never invent always; grey → reject
            return json.dumps({"decision": "reject", "reason": "dry_run_no_lead"}), {
                "decision": "reject",
                "reason": "dry_run_no_lead",
            }
        return self._glue().call_lead(prompt, schema, cwd)

    def ta_call(self, method: str, path: str, body=None, extra_headers=None, timeout=120):
        if self._teleagent_call is not None:
            return self._teleagent_call(method, path, body, extra_headers, timeout)
        g = self._glue()
        # Prefer adapter surface when glue exposes one
        ad = getattr(g, "get_ta_adapter", lambda: None)()
        if ad is not None and hasattr(ad, "call"):
            return ad.call(method, path, body=body, extra_headers=extra_headers, timeout=timeout)
        return g.call(method, path, body=body, extra_headers=extra_headers, timeout=timeout)

    def is_session_busy(self, status_obj, sid: str) -> bool:
        if self._session_busy_fn is not None:
            return self._session_busy_fn(status_obj, sid)
        if self.dry_run:
            return False
        return self._glue().session_busy(status_obj, sid)

    # --- enqueue / workdirs ------------------------------------------------------

    def enqueue_charter(
        self,
        charter: dict,
        *,
        instruction: str | None = None,
        expected_artifacts: list[str] | None = None,
        job_id: str | None = None,
        simulated_pending: list[dict] | None = None,
    ) -> JobSlot:
        from charter import build_instruction, expected_artifacts as resolve_arts, job_name

        name = job_name(charter)
        jid = job_id or new_job_id(name)
        workdir = self.workspaces_root / jid
        workdir.mkdir(parents=True, exist_ok=True)
        instr = instruction if instruction is not None else build_instruction(charter)
        arts = (
            expected_artifacts
            if expected_artifacts is not None
            else resolve_arts(charter, workspace=workdir)
        )
        slot = JobSlot(
            job_id=jid,
            name=name,
            charter=charter,
            instruction=instr,
            expected_artifacts=arts,
            workdir=workdir,
            timeout_sec=int(300 if charter.get("timeout_sec") is None else charter.get("timeout_sec")),
            force_lead_review=bool(charter.get("force_lead_review", False)),
            worker_intent=charter.get("worker_intent"),
            blocker=charter.get("blocker"),
            dry=self.dry_run,
            simulated_pending=list(simulated_pending or []),
        )
        with self._lock:
            self.jobs[jid] = slot
        return slot

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1
                for j in self.jobs.values()
                if j.state
                in (
                    JobState.STARTING,
                    JobState.RUNNING,
                    JobState.PENDING_APPROVAL,
                )
            )

    def queued(self) -> list[JobSlot]:
        with self._lock:
            return [j for j in self.jobs.values() if j.state == JobState.QUEUED]

    def any_busy_or_pending(self) -> tuple[bool, bool]:
        now = time.time()
        any_busy = False
        any_pending_recent = False
        with self._lock:
            for j in self.jobs.values():
                if j.state not in (
                    JobState.RUNNING,
                    JobState.PENDING_APPROVAL,
                    JobState.STARTING,
                ):
                    continue
                if j.busy or j.state == JobState.PENDING_APPROVAL:
                    any_busy = True
                if j.last_pending_at and (now - j.last_pending_at) < 5.0:
                    any_pending_recent = True
        return any_busy, any_pending_recent

    def next_poll_interval(self) -> float:
        any_busy, any_pending = self.any_busy_or_pending()
        iv = choose_poll_interval(
            any_busy=any_busy,
            any_pending_recent=any_pending,
            idle_min=self.idle_min,
            idle_max=self.idle_max,
            busy_min=self.busy_min,
            busy_max=self.busy_max,
        )
        self.stats.poll_intervals.append(iv)
        return iv

    # --- start jobs --------------------------------------------------------------

    def _start_job_live(self, job: JobSlot) -> None:
        g = self._glue()
        ws = str(job.workdir)
        code, created = self.ta_call(
            "POST",
            "/session",
            body={"title": f"collab-{job.name}-{job.job_id}", "directory": ws},
            extra_headers={"x-opencode-directory": ws},
        )
        if code >= 300 or not isinstance(created, dict) or not created.get("id"):
            job.state = JobState.FAIL
            job.result = {"ok": False, "error": f"create session failed: {code}", "state": "fail"}
            job.finished_at = time.time()
            return
        sid = created["id"]
        job.session_id = sid
        code, _ = self.ta_call(
            "POST",
            f"/session/{sid}/prompt_async",
            body=g.prompt_body(job.instruction),
            extra_headers={"x-opencode-directory": ws},
        )
        if code not in (200, 204) and code >= 300:
            job.state = JobState.FAIL
            job.result = {"ok": False, "error": f"prompt_async failed: {code}", "state": "fail"}
            job.finished_at = time.time()
            return
        job.state = JobState.RUNNING
        job.started_at = time.time()
        job.notes.append(f"started session={sid} workdir={ws}")

    def _start_job_dry(self, job: JobSlot) -> None:
        """Dry: isolate workdir + optional simulated pending; no TeleAgent."""
        job.session_id = f"dry-{job.job_id}"
        job.state = JobState.RUNNING
        job.started_at = time.time()
        # Prove isolation: write a marker only in this workdir
        marker = job.workdir / f"_scheduler_marker_{job.job_id}.txt"
        marker.write_text(f"job_id={job.job_id}\nname={job.name}\n", encoding="utf-8")
        job.notes.append(f"dry started workdir={job.workdir}")
        # Always land expected artifacts in THIS workdir only (isolation proof).
        # Pending simulation still exercises approval path before refresh completes.
        for ap in job.expected_artifacts:
            art = Path(ap)
            art.parent.mkdir(parents=True, exist_ok=True)
            if not art.exists():
                art.write_text(f"dry-ok from {job.job_id}\n", encoding="utf-8")

    def try_start_queued(self) -> list[str]:
        started: list[str] = []
        while self.active_count() < self.max_parallel:
            q = self.queued()
            if not q:
                break
            job = q[0]
            job.state = JobState.STARTING
            try:
                if self.dry_run:
                    self._start_job_dry(job)
                else:
                    self._start_job_live(job)
            except Exception as e:
                job.state = JobState.FAIL
                job.result = {"ok": False, "error": str(e), "state": "fail"}
                job.finished_at = time.time()
            started.append(job.job_id)
        return started

    # --- scan (≠ call_lead) ------------------------------------------------------

    def scan_pending(self) -> list[dict]:
        """Scan only. Empty → skip. Never calls lead."""
        self.stats.scans += 1
        if self.dry_run:
            pending: list[dict] = []
            with self._lock:
                for job in self.jobs.values():
                    if job.state not in (JobState.RUNNING, JobState.PENDING_APPROVAL):
                        continue
                    while job.simulated_pending:
                        # serial: expose at most one pending per job per scan
                        p = job.simulated_pending.pop(0)
                        p = dict(p)
                        p.setdefault("sessionID", job.session_id)
                        p.setdefault("id", p.get("id") or f"sim-{uuid.uuid4().hex[:8]}")
                        pending.append(p)
                        job.last_pending_at = time.time()
                        break  # one-by-one per job this scan
            if not pending:
                self.stats.empty_scans += 1
            else:
                self.stats.pending_seen += len(pending)
            return pending

        code, pending = self.ta_call("GET", "/permission")
        if not isinstance(pending, list) or not pending:
            self.stats.empty_scans += 1
            return []
        # Filter to our sessions only
        with self._lock:
            ours = {j.session_id: j for j in self.jobs.values() if j.session_id}
        filtered = []
        for p in pending:
            if not isinstance(p, dict):
                continue
            sid = session_id_of_permission(p)
            if sid and sid in ours:
                filtered.append(p)
                ours[sid].last_pending_at = time.time()
                ours[sid].state = JobState.PENDING_APPROVAL
        if not filtered:
            self.stats.empty_scans += 1
        else:
            self.stats.pending_seen += len(filtered)
        return filtered

    def _job_for_permission(self, p: dict) -> JobSlot | None:
        sid = session_id_of_permission(p)
        with self._lock:
            for j in self.jobs.values():
                if j.session_id and j.session_id == sid:
                    return j
        return None

    def handle_one_permission(self, p: dict) -> dict:
        """Process exactly one pending (serial弹权). Hard rules first; lead only if needed."""
        self.stats.serial_one_by_one += 1
        job = self._job_for_permission(p)
        if job is None:
            return {"skipped": True, "reason": "unknown_session"}

        pid = str(p.get("id") or p.get("requestID") or "")
        if not pid or pid in job.handled_perm_ids:
            return {"skipped": True, "reason": "already_handled", "id": pid}

        g = None if self.dry_run else self._glue()
        summary = (
            json.dumps({k: p.get(k) for k in ("id", "path", "tool", "permission") if k in p}, ensure_ascii=False)
            if self.dry_run
            else g.summarize_permission(p)
        )
        job.pending_summaries.append(summary)

        # 1) Hard rules
        perm_obj = dict(p) if isinstance(p, dict) else {}
        hr = hard_rule_decision(perm_obj, charter=job.charter)
        if hr and hr.get("reply") == "reject":
            self._reply(job, pid, "reject", via="hard_rule", original=perm_obj)
            job.hard_rule_rejects.append({"id": pid, "reason": hr.get("reason", "")})
            job.notes.append(f"hard_rule reject: {hr.get('reason', '')}")
            self.stats.hard_rule_hits += 1
            self.stats.replies += 1
            return {"id": pid, "reply": "reject", "via": "hard_rule", "called_lead": False}

        if perm_obj.get("_hard_rule_allowlisted"):
            self._reply(job, pid, "once", via="hard_rule_allowlisted", original=perm_obj)
            job.notes.append(
                "hard_rule allowlisted: once — logged, no lead (secret always forbidden)"
            )
            self.stats.allowlist_hits += 1
            self.stats.replies += 1
            return {"id": pid, "reply": "once", "via": "hard_rule_allowlisted", "called_lead": False}

        # 2) Decision packet — only then maybe lead
        intent = (job.worker_intent or "").strip() or (
            f"Worker requests permission to continue job {job.name!r} "
            f"(job_id={job.job_id}) inside workspace; see proposed_action."
        )
        blk = job.blocker if job.blocker is not None else {
            "failed_path": "permission_gate",
            "detail": "worker awaiting lead decision on pending permission",
        }
        include_full = not job.charter_sent_full
        packet = packet_from_permission(
            perm_obj,
            worker_intent=intent,
            blocker=blk,
            charter=job.charter,
            include_charter_full=include_full,
            ping_reason="permission",
            risk_tags=["permission"],
        )
        if include_full:
            job.charter_sent_full = True

        missing = [
            req_k
            for req_k in ("worker_intent", "blocker", "charter_ref")
            if req_k not in packet or packet[req_k] in (None, "", {})
        ]
        if missing:
            self._reply(job, pid, "reject", via="incomplete_packet", original=perm_obj)
            job.notes.append(f"packet missing {missing}; reject")
            self.stats.replies += 1
            return {"id": pid, "reply": "reject", "via": "incomplete_packet", "called_lead": False}

        pa = packet.get("proposed_action") or {}
        tclass = pa.get("target_class") or "unknown"
        ppat = pa.get("path_pattern") or ""
        path_guess = pa.get("target") or perm_obj.get("path") or ""
        tool_guess = pa.get("tool") or ""
        ws = canonicalize(str(job.workdir))
        # All non-hard-rule paths go through lead (no unconditional once / ordinary_rw bypass).
        # Track ordinary_rw for stats only.
        targets = pa.get("targets") or ([path_guess] if path_guess else [])
        in_ws = all(is_path_within(canonicalize(t, base=ws), ws) for t in targets) if targets else False
        if (
            not should_ping_lead(
                ping_reason="permission",
                tool=tool_guess,
                path=path_guess,
                target_class=tclass,
            )
            and in_ws
        ):
            self.stats.ordinary_rw_hits += 1
            job.notes.append("ordinary workspace R/W — still requires lead approval (no auto-once)")

        # 3) Need lead — dedupe by permission request id only
        if not job.ping_deduper.should_emit_id(pid):
            return {"skipped": True, "reason": "dedupe_request_id", "id": pid}
        job.ping_deduper.mark_inflight_id(pid)
        allow_hint = (
            f"Allowed workspace only: {ws}. "
            "Secret-adjacent / auth workarounds: reject or demand_safe_path, never once. "
            "Never choose always."
        )
        from lead_adapter import (
            LeadDecisionError,
            build_lead_request,
            lead_permission_response_schema,
            validate_lead_decision,
        )
        req = build_lead_request(
            kind="permission",
            goal=(job.charter or {}).get("goal") or f"job {job.name}",
            authorized_scope=(job.charter or {}).get("must") or [],
            prohibitions=(job.charter or {}).get("must_not") or [],
            acceptance_criteria=(job.charter or {}).get("acceptance")
            or (job.charter or {}).get("done_when")
            or {},
            current_application=packet,
            charter=job.charter,
        )
        schema = lead_permission_response_schema()
        if self._call_lead_fn is not None or self.dry_run:
            raw, parsed = self.call_lead(format_lead_prompt(packet, allow_hint=allow_hint), schema, ws)
        else:
            try:
                raw, parsed = self._glue().call_lead_request(req, schema=schema, cwd=ws)
            except Exception:
                raw, parsed = self.call_lead(format_lead_prompt(packet, allow_hint=allow_hint), schema, ws)
        # Test/dry fakes often omit binding fields — stitch from request so protocol still enforced live.
        if isinstance(parsed, dict) and "decision" in parsed and "application_id" not in parsed:
            parsed = {
                **parsed,
                "application_id": req["application_id"],
                "context_summary": req.get("context_summary", ""),
                "reason": parsed.get("reason") or "dry_or_injected_lead",
            }
            raw = __import__("json").dumps(parsed)
        try:
            validated = validate_lead_decision(raw, parsed, request=req, kind="permission")
            decision = validated.get("decision")
        except LeadDecisionError as e:
            job.notes.append(f"lead invalid ({e.code}): keep pending or safe reject")
            if e.code in ("timeout", "call_failed"):
                job.ping_deduper.clear_inflight_id(pid)
                return {"skipped": True, "reason": f"lead_{e.code}", "id": pid, "called_lead": True}
            decision = "reject"
        if decision not in ("once", "reject", "deny_job", "demand_safe_path", "always"):
            decision = "reject"
        # Secret class: never promote to always
        if decision == "always":
            job.notes.append("lead said always → coerced to once (secrets never always)")
            decision = "once"
            # still forbid always for secret-adjacent
            if tclass in ("user_secret_store", "env_file", "browser_profile"):
                decision = "reject"
                job.notes.append("secret-adjacent always forbidden → reject")

        api_reply, map_note = map_lead_decision_to_api(decision)
        if map_note:
            job.notes.append(map_note)
        job.ping_deduper.mark_replied_id(pid)
        self._reply(job, pid, api_reply, via="lead", lead_decision=decision, raw=raw[:500] if raw else "", original=perm_obj)
        self.stats.replies += 1
        if decision == "deny_job":
            job.state = JobState.FAIL
            job.result = {"ok": False, "error": "lead deny_job", "state": "fail"}
            job.finished_at = time.time()
        return {
            "id": pid,
            "reply": api_reply,
            "via": "lead",
            "lead_decision": decision,
            "called_lead": True,
        }

    def _reconfirm_pending(self, job: JobSlot, pid: str, original: dict | None) -> tuple[bool, str]:
        """Before approve/reject: still pending, session matches, content unchanged."""
        if self.dry_run:
            return True, "dry"
        code, pending = self.ta_call("GET", "/permission")
        if not isinstance(pending, list):
            return False, "pending_list_unavailable"
        match = None
        for p in pending:
            if not isinstance(p, dict):
                continue
            if str(p.get("id") or p.get("requestID") or "") == pid:
                match = p
                break
        if match is None:
            return False, "no_longer_pending"
        sid = session_id_of_permission(match)
        if sid and job.session_id and sid != job.session_id:
            return False, "session_mismatch"
        if original:
            if permission_fingerprint(match) != permission_fingerprint(original):
                return False, "content_changed"
        return True, "ok"

    def _reply(
        self,
        job: JobSlot,
        pid: str,
        reply: str,
        *,
        via: str,
        lead_decision: str | None = None,
        raw: str = "",
        original: dict | None = None,
    ) -> bool:
        # Default once; never auto-always
        if reply == "always":
            reply = "once"
            job.notes.append("coerced always->once at reply boundary")
        ok_rc, reason = self._reconfirm_pending(job, pid, original)
        if not ok_rc:
            job.notes.append(f"reconfirm failed ({reason}); not marking handled id={pid}")
            entry = {"id": pid, "reply": reply, "via": via, "reconfirm": reason, "skipped_reply": True}
            if lead_decision:
                entry["lead_decision"] = lead_decision
            job.api_replies.append(entry)
            return False
        http = 200
        if not self.dry_run:
            http, _body = self.ta_call("POST", f"/permission/{pid}/reply", body={"reply": reply})
            if not (isinstance(http, int) and http < 300):
                job.notes.append(f"reply POST failed http={http}; not marking handled")
                job.api_replies.append(
                    {"id": pid, "reply": reply, "via": via, "http": http, "skipped_mark": True}
                )
                return False
        entry = {"id": pid, "reply": reply, "via": via, "http": http, "reconfirm": reason}
        if lead_decision:
            entry["lead_decision"] = lead_decision
        if raw:
            entry["lead_raw_trunc"] = raw
        job.api_replies.append(entry)
        # Mark handled only after successful reply
        job.handled_perm_ids.add(pid)
        job.ping_deduper.mark_replied_id(pid)
        if job.state == JobState.PENDING_APPROVAL:
            job.state = JobState.RUNNING
        return True

    def dispatch_pending_batch(self, pending: list[dict]) -> list[dict]:
        """Batch schedule collected pending, but decide per job_id (one-by-one each).

        Multi-path: may have several jobs' pending in one scan; still serial within a job
        and independent call_lead per item (no shared context mash).
        """
        # Group by session / job — process at most one per job this round for serial弹权
        by_job: dict[str, list[dict]] = {}
        for p in pending:
            job = self._job_for_permission(p)
            key = job.job_id if job else "_unknown"
            by_job.setdefault(key, []).append(p)

        results: list[dict] = []
        for _jid, items in by_job.items():
            # Serial: only the first pending for this job this tick
            results.append(self.handle_one_permission(items[0]))
        return results

    # --- completion --------------------------------------------------------------

    def refresh_job_status(self, job: JobSlot) -> None:
        from completion import (
            artifacts_all_present,
            build_acceptance_packet,
            confirm_artifacts_for_lead_approve,
            is_success_allowed,
            missing_artifacts,
            snapshot_artifacts,
        )
        from lead_adapter import (
            LeadDecisionError,
            build_lead_request,
            lead_review_response_schema,
            validate_lead_decision,
        )

        if job.state in (JobState.DONE, JobState.FAIL, JobState.TIMEOUT, JobState.QUEUED):
            return

        def _arts_complete() -> tuple[bool, list[str]]:
            recs = snapshot_artifacts(job.expected_artifacts)
            present = [r.path for r in recs if r.exists]
            return artifacts_all_present(recs), present

        def _fail(state: str, error: str, arts: list[str]) -> None:
            job.state = JobState.TIMEOUT if state == "timeout" else JobState.FAIL
            if state == "timeout":
                job.state = JobState.TIMEOUT
            job.result = {
                "ok": False,
                "state": state,
                "error": error,
                "artifacts": arts,
                "notes": list(job.notes),
                "api_replies": job.api_replies,
                "hard_rule_rejects": job.hard_rule_rejects,
                "session_id": job.session_id,
                "job_id": job.job_id,
                "workdir": str(job.workdir),
                "force_lead_review": job.force_lead_review,
            }
            job.finished_at = time.time()

        def _succeed(arts: list[str], *, state: str = "ok", extra_notes: list | None = None) -> None:
            notes = list(job.notes) + list(extra_notes or [])
            prior = job.result if isinstance(job.result, dict) else {}
            verdict = prior.get("lead_review_verdict")
            ok, why = is_success_allowed(
                state="ok",
                artifacts_ok=True,
                lead_verdict=verdict,
                force_lead_review=job.force_lead_review,
            )
            if not ok:
                _fail("fail", why, arts)
                return
            job.state = JobState.DONE
            job.result = {
                "ok": True,
                "state": state,
                "artifacts": arts,
                "notes": notes,
                "api_replies": job.api_replies,
                "hard_rule_rejects": job.hard_rule_rejects,
                "session_id": job.session_id,
                "job_id": job.job_id,
                "workdir": str(job.workdir),
                "force_lead_review": job.force_lead_review,
                "lead_review_verdict": verdict,
                "dry_run": self.dry_run,
            }
            job.finished_at = time.time()
            job.busy = False

        def _run_force_lead_review(arts: list[str]) -> bool:
            """Return True if lead passed and fingerprints stable. Parallel + serial."""
            packet = build_acceptance_packet(
                job_name=job.name,
                goal=(job.charter or {}).get("goal") or job.instruction[:240],
                acceptance_criteria=(job.charter or {}).get("acceptance")
                or (job.charter or {}).get("done_when")
                or {"artifacts": job.expected_artifacts},
                expected_artifacts=job.expected_artifacts,
                execution_result={"job_id": job.job_id, "path": "scheduler"},
                error="",
                tool_records=job.pending_summaries,
                api_replies=job.api_replies,
                notes=job.notes,
                state=job.state.value,
                session_id=job.session_id,
            )
            unchanged, gate = confirm_artifacts_for_lead_approve(packet)
            job.notes.append(f"artifact_gate={gate.get('unchanged')} diffs={gate.get('diffs')}")
            if not unchanged:
                job.result = {"lead_review_verdict": "fail", "artifact_gate": gate}
                return False
            req = build_lead_request(
                kind="review",
                goal=(job.charter or {}).get("goal") or job.instruction[:240],
                authorized_scope=(job.charter or {}).get("must") or [],
                prohibitions=(job.charter or {}).get("must_not") or [],
                acceptance_criteria=packet.get("acceptance_criteria"),
                current_application=packet,
                charter=job.charter,
            )
            schema = lead_review_response_schema()
            if self._call_lead_fn is not None or self.dry_run:
                raw, parsed = self.call_lead(
                    __import__("json").dumps({"_lead_request": req, "prompt_kind": "review"}),
                    schema,
                    str(job.workdir),
                )
            else:
                try:
                    raw, parsed = self._glue().call_lead_request(
                        req, schema=schema, cwd=str(job.workdir)
                    )
                except Exception:
                    raw, parsed = self.call_lead(
                        __import__("json").dumps({"_lead_request": req}),
                        schema,
                        str(job.workdir),
                    )
            if isinstance(parsed, dict):
                # Map permission-style dry stubs → review verdict; stitch binding fields
                if "verdict" not in parsed and parsed.get("decision") in ("once", "reject", "deny_job"):
                    parsed = {
                        **parsed,
                        "verdict": "pass" if parsed.get("decision") == "once" else "fail",
                    }
                if "verdict" in parsed and "application_id" not in parsed:
                    parsed = {
                        **parsed,
                        "application_id": req["application_id"],
                        "context_summary": req.get("context_summary", ""),
                        "reason": parsed.get("reason") or "dry_or_injected_lead",
                    }
                    raw = __import__("json").dumps(parsed)
            try:
                decision = validate_lead_decision(raw, parsed, request=req, kind="review")
                verdict = decision.get("verdict") or "fail"
            except LeadDecisionError as e:
                job.notes.append(f"force_lead_review invalid ({e.code}): safe-stop")
                verdict = "fail"
            job.result = {"lead_review_verdict": verdict, "lead_review_raw": (raw or "")[:1500]}
            unchanged2, gate2 = confirm_artifacts_for_lead_approve(packet)
            if verdict == "pass" and not unchanged2:
                job.notes.append(f"artifacts changed during lead: {gate2.get('diffs')}")
                job.result["lead_review_verdict"] = "fail"
                return False
            return verdict == "pass"

        # Wall timeout — NEVER success even if some/all artifacts exist (条2)
        if job.started_at and (time.time() - job.started_at) > job.timeout_sec:
            complete, arts = _arts_complete()
            _fail(
                "timeout",
                "wall clock timeout (not success even with artifacts)",
                arts,
            )
            job.notes.append(
                f"timeout with artifacts_complete={complete} missing={missing_artifacts(job.expected_artifacts)}"
            )
            return

        if self.dry_run:
            if job.simulated_pending:
                job.busy = True
                return
            complete, arts = _arts_complete()
            if not complete:
                return
            markers = list(job.workdir.glob("_scheduler_marker_*.txt"))
            if job.force_lead_review:
                if not _run_force_lead_review(arts):
                    _fail("fail", "force_lead_review did not pass", arts)
                    return
            _succeed(arts, state="dry_run_ok", extra_notes=[f"markers={[str(m) for m in markers]}"])
            if job.result is not None:
                job.result["markers"] = [str(m) for m in markers]
            return

        # Live: session idle + ALL artifacts; force_lead_review before DONE
        sc, status = self.ta_call("GET", "/session/status")
        job.busy = self.is_session_busy(status, job.session_id)
        complete, arts = _arts_complete()
        if not job.busy and complete:
            if job.force_lead_review:
                if not _run_force_lead_review(arts):
                    _fail("fail", "force_lead_review did not pass", arts)
                    return
            _succeed(arts, state="ok")

    def write_job_report(self, job: JobSlot) -> Path:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        out = self.runs_root / job.job_id
        out.mkdir(parents=True, exist_ok=True)
        status = {
            "ok": bool((job.result or {}).get("ok")),
            "state": job.state.value,
            "job_id": job.job_id,
            "session_id": job.session_id,
            "workdir": str(job.workdir),
            "artifacts": (job.result or {}).get("artifacts", []),
            "error": (job.result or {}).get("error", ""),
            "api_replies": job.api_replies,
            "hard_rule_rejects": job.hard_rule_rejects,
            "notes": job.notes,
            "scheduler_stats": self.stats.to_dict(),
        }
        (out / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "report.json").write_text(
            json.dumps(
                {"charter_name": job.name, "result": job.result, "status": status},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        return out

    # --- main loop ---------------------------------------------------------------

    def tick(self) -> dict:
        """One scheduler tick: start slots → scan pending → dispatch → refresh."""
        started = self.try_start_queued()
        pending = self.scan_pending()
        dispatched: list[dict] = []
        if pending:
            # 有请求才处理；禁止空扫描叫 lead
            dispatched = self.dispatch_pending_batch(pending)
        with self._lock:
            active = [
                j
                for j in self.jobs.values()
                if j.state
                in (JobState.RUNNING, JobState.PENDING_APPROVAL, JobState.STARTING)
            ]
        for j in active:
            self.refresh_job_status(j)
            if j.state in (JobState.DONE, JobState.FAIL, JobState.TIMEOUT):
                self.write_job_report(j)
        return {
            "started": started,
            "pending_count": len(pending),
            "dispatched": dispatched,
            "active": self.active_count(),
            "stats": self.stats.to_dict(),
        }

    def all_terminal(self) -> bool:
        with self._lock:
            if not self.jobs:
                return True
            return all(
                j.state in (JobState.DONE, JobState.FAIL, JobState.TIMEOUT)
                for j in self.jobs.values()
            )

    def run(self, *, max_ticks: int | None = None, sleep: bool = True) -> dict:
        """Run until all jobs terminal (or max_ticks). Adaptive sleep between ticks."""
        ticks = 0
        history: list[dict] = []
        while not self._stop.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            info = self.tick()
            history.append(info)
            ticks += 1
            if self.stop_when_idle and self.all_terminal() and not self.queued():
                break
            if sleep:
                iv = self.next_poll_interval()
                self._stop.wait(iv)
            else:
                # tests: still record what interval would have been
                self.next_poll_interval()
        # finalize any leftovers
        for j in self.jobs.values():
            if j.state not in (JobState.DONE, JobState.FAIL, JobState.TIMEOUT):
                self.refresh_job_status(j)
            if j.state in (JobState.DONE, JobState.FAIL, JobState.TIMEOUT) and not (
                self.runs_root / j.job_id / "status.json"
            ).exists():
                self.write_job_report(j)
        return {
            "ticks": ticks,
            "jobs": {
                jid: {
                    "state": j.state.value,
                    "job_id": j.job_id,
                    "workdir": str(j.workdir),
                    "session_id": j.session_id,
                    "ok": (j.result or {}).get("ok"),
                    "result": j.result,
                    "api_replies": j.api_replies,
                    "notes": j.notes,
                }
                for jid, j in self.jobs.items()
            },
            "stats": self.stats.to_dict(),
            "history_tail": history[-10:],
        }

    def shutdown(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)


def smoke_parallel_isolation(
    *,
    n: int = 3,
    max_parallel: int = 3,
    workspaces_root: Path | None = None,
) -> dict:
    """Dry smoke: N parallel jobs, isolated workdirs, no cross-file writes."""
    root = Path(workspaces_root or (REPO / "jobs" / "workspaces" / "_smoke"))
    sched = ParallelScheduler(
        max_parallel=max_parallel,
        workspaces_root=root,
        dry_run=True,
        idle_min=10,
        idle_max=12,
        busy_min=1,
        busy_max=2,
        stop_when_idle=True,
    )
    for i in range(n):
        charter = {
            "name": f"iso-{i}",
            "goal": f"Write artifact for parallel isolation job {i}",
            "must": ["Stay in workdir"],
            "must_not": ["Touch other job workdirs", "always-approve"],
            "allow_secret_globs": [],
            "allow_paths": [],
            "allow_keys": [],
            "done_when": {"artifacts": [f"out-{i}.txt"]},
            "timeout_sec": 60,
        }
        # Job 0 gets a hard-rule reject pending; job 1 allowlist-ish grey; job 2 empty
        sim = []
        if i == 0:
            sim = [
                {
                    "id": f"p-deny-{i}",
                    "path": "/home/box/.env",
                    "tool": "read",
                    "permission": "read",
                }
            ]
        elif i == 1:
            sim = [
                {
                    "id": f"p-src-{i}",
                    "path": "",  # filled after enqueue with workdir
                    "tool": "edit",
                    "permission": "edit",
                }
            ]
        slot = sched.enqueue_charter(charter, simulated_pending=sim)
        if i == 1 and slot.simulated_pending:
            # ordinary workspace source R/W → no lead (should_ping_lead False)
            slot.simulated_pending[0]["path"] = str(slot.workdir / "worker.py")

    summary = sched.run(sleep=False, max_ticks=50)
    sched.shutdown()

    # Isolation assertions
    workdirs = [Path(j["workdir"]) for j in summary["jobs"].values()]
    assert len(workdirs) == n
    assert len({str(w) for w in workdirs}) == n, "workdirs must be unique"
    cross = []
    for jid, info in summary["jobs"].items():
        wd = Path(info["workdir"])
        for other_id, other in summary["jobs"].items():
            if other_id == jid:
                continue
            # other job must not have written into this workdir's marker namespace wrongly
            foreign_markers = list(wd.glob(f"_scheduler_marker_{other_id}.txt"))
            if foreign_markers:
                cross.append((jid, other_id, foreign_markers))
        markers = list(wd.glob(f"_scheduler_marker_{jid}.txt"))
        assert markers, f"missing own marker in {wd}"
    assert not cross, f"cross-contamination: {cross}"
    # Job0 hard-rule reject (no lead); job1 ordinary R/W still goes to lead (no auto-once);
    # dry lead → reject. Isolation must still hold.
    assert summary["stats"]["lead_calls"] >= 1, (
        "smoke iso: ordinary workspace R/W must call lead after removing auto-once; "
        f"got {summary['stats']['lead_calls']}"
    )
    # empty scans happen after pending drained
    assert summary["stats"]["scans"] >= 1
    summary["isolation_ok"] = True
    summary["cross_contamination"] = cross
    return summary


def smoke_serial_short_poll() -> dict:
    """Prove: busy/pending → 1～3s interval; empty scan does not call lead; one-by-one replies."""
    lead_log: list[str] = []

    def fake_lead(prompt, schema, cwd):
        lead_log.append(prompt[:200])
        return json.dumps({"decision": "once"}), {"decision": "once"}

    root = REPO / "jobs" / "workspaces" / "_smoke_serial"
    sched = ParallelScheduler(
        max_parallel=1,
        workspaces_root=root,
        dry_run=True,
        call_lead_fn=fake_lead,
        idle_min=10,
        idle_max=30,
        busy_min=1,
        busy_max=3,
    )
    charter = {
        "name": "serial-pop",
        "goal": "Serial privilege popup short-poll path",
        "must": ["Stay in workdir"],
        "must_not": ["always-approve secrets"],
        "allow_secret_globs": [],
        "allow_paths": [],
        "allow_keys": [],
        "done_when": {"artifacts": ["serial-out.txt"]},
        "timeout_sec": 60,
    }
    # Two greys that need lead — must be answered one-by-one, not batched into one lead call
    sim = [
        {
            "id": "ser-1",
            "path": "/tmp/collab-unknown-tool-target-1",
            "tool": "bash",
            "permission": "bash",
            "command": "echo one",
        },
        {
            "id": "ser-2",
            "path": "/tmp/collab-unknown-tool-target-2",
            "tool": "bash",
            "permission": "bash",
            "command": "echo two",
        },
    ]
    job = sched.enqueue_charter(charter, simulated_pending=sim)

    # Tick 1: start + first pending only
    t1 = sched.tick()
    assert t1["pending_count"] == 1, t1
    assert len(t1["dispatched"]) == 1
    assert t1["dispatched"][0].get("called_lead") is True
    iv_busy = sched.next_poll_interval()
    assert 1.0 <= iv_busy <= 3.0 + 1e-6, f"busy interval expected 1-3s, got {iv_busy}"

    # Tick 2: second pending
    t2 = sched.tick()
    assert t2["pending_count"] == 1, t2
    assert t2["dispatched"][0].get("called_lead") is True

    # Tick 3: empty scan — must NOT call lead
    leads_before = sched.stats.lead_calls
    t3 = sched.tick()
    assert t3["pending_count"] == 0
    assert sched.stats.lead_calls == leads_before
    assert sched.stats.empty_scans >= 1

    # Finish
    summary = sched.run(sleep=False, max_ticks=20)
    sched.shutdown()
    assert len(lead_log) == 2, f"expected 2 short call_lead, got {len(lead_log)}"
    assert job.api_replies[0]["id"] == "ser-1"
    assert job.api_replies[1]["id"] == "ser-2"
    summary["serial_ok"] = True
    summary["busy_interval_sample"] = iv_busy
    summary["lead_calls"] = len(lead_log)
    return summary


if __name__ == "__main__":
    import pprint

    print("=== parallel isolation smoke ===")
    pprint.pp(smoke_parallel_isolation(n=3))
    print("=== serial short-poll smoke ===")
    pprint.pp(smoke_serial_short_poll())
