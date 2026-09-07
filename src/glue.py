#!/usr/bin/env python3
"""TeleAgent worker + pluggable lead glue. Secrets never printed or written to reports."""
from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

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

_ADAPTER = None


def get_ta_adapter():
    """Lazy Linux local-v1 adapter (shared with scheduler). Dry tests may never call this."""
    global _ADAPTER
    if _ADAPTER is None:
        from teleagent_adapter import get_adapter
        _ADAPTER = get_adapter(platform="linux")
    return _ADAPTER

COLLAB = Path("/workspace/teleagent/probe-sandbox/collab")
BASE = "http://127.0.0.1:4399"
# Lead is swappable: default Grok Build; override with COLLAB_LEAD_BIN (Claude Code/Codex later).
LEAD_BIN = os.environ.get("COLLAB_LEAD_BIN", "/workspace/run-grok.sh")
LEAD_NAME = os.environ.get("COLLAB_LEAD_NAME", "grok")
MODEL = {
    "providerID": os.environ.get("TELEAGENT_PROVIDER_ID", "NewApi"),
    "modelID": os.environ.get("TELEAGENT_MODEL_ID", "chat-lite"),  # use chat-pro for portal 积分
}
TELEAGENT_AGENT = os.environ.get("TELEAGENT_AGENT", "opencowork-default")

def prompt_body(text: str, model=None) -> dict:
    """Build a TeleAgent prompt payload with fresh queryID (portal metering)."""
    return {
        "parts": [{"type": "text", "text": text}],
        "model": model or MODEL,
        "agent": TELEAGENT_AGENT,
        "queryID": f"q_{uuid.uuid4()}",
    }




def find_creds():
    keys = (b"OPENCODE_SERVER_PASSWORD", b"SUPER_AGENT_LOCAL_SESSION_KEY", b"OPENCODE_SERVER_USERNAME")
    for path in glob.glob("/proc/[0-9]*/environ"):
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if not all(k in data for k in keys):
            continue
        env = {}
        for item in data.split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                env[k] = v
        return (
            env[b"OPENCODE_SERVER_USERNAME"].decode(),
            env[b"OPENCODE_SERVER_PASSWORD"].decode(),
            env[b"SUPER_AGENT_LOCAL_SESSION_KEY"].decode(),
        )
    raise RuntimeError("TeleAgent local API creds not found in process environ")


_CREDS: tuple[str, str, str] | None = None


def _ensure_creds() -> tuple[str, str, str]:
    """Lazy TeleAgent creds — import glue without :4399 for dry/scheduler unit tests."""
    global _CREDS
    if _CREDS is None:
        _CREDS = find_creds()
    return _CREDS


def sign_headers(method: str, url: str) -> dict:
    user, pw, key = _ensure_creds()
    basic = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    n = urlparse(url)
    path = f'{n.path}{("?" + n.query) if n.query else ""}'
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(12)
    payload = "\n".join(["local-v1", method.upper(), path, ts, nonce])
    sig = (
        base64.urlsafe_b64encode(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
        .decode()
        .rstrip("=")
    )
    return {
        "X-SA-Sign-Version": "local-v1",
        "X-SA-Timestamp": ts,
        "X-SA-Nonce": nonce,
        "X-SA-Signature": sig,
        "Authorization": basic,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def call(method: str, path: str, body=None, extra_headers=None, timeout=120):
    """HTTP to TeleAgent — prefers teleagent_adapter.LinuxLocalV1Adapter."""
    try:
        ad = get_ta_adapter()
        # Keep adapter base in sync with glue.BASE
        if getattr(ad, "base_url", None) and ad.base_url.rstrip("/") != BASE.rstrip("/"):
            ad.base_url = BASE.rstrip("/")
        return ad.call(method, path, body=body, extra_headers=extra_headers, timeout=timeout)
    except Exception:
        # Fallback to legacy inline HMAC if adapter import/creds fail mid-flight
        url = f"{BASE}{path}"
        h = sign_headers(method, url)
        if extra_headers:
            h.update(extra_headers)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, headers=h, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, None
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                j = json.loads(raw) if raw else None
            except Exception:
                j = raw.decode("utf-8", "replace") if raw else None
            return e.code, j


def redact(obj):
    s = json.dumps(obj, ensure_ascii=False, default=str)
    s = re.sub(r"(?i)(authorization|password|token|signature|key)(\s*[:=]\s*)([^\s,\"']+)", r"\1\2<redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", "<jwt>", s)
    return s[:4000]


def summarize_permission(p: dict) -> str:
    keep = {k: p.get(k) for k in ("id", "type", "permission", "path", "patterns", "sessionID", "message", "title", "tool", "command") if k in p}
    # keep only useful non-empty
    slim = {}
    for k, v in p.items():
        if k.lower() in ("id", "type", "permission", "permissions", "path", "patterns", "sessionid", "session_id", "message", "title", "tool", "command", "description", "metadata"):
            slim[k] = v
        elif isinstance(v, (str, int, bool)) and k.lower() not in ("token", "password", "key"):
            if len(slim) < 12:
                slim[k] = v
    return redact(slim)


def call_lead(prompt: str, schema: dict, cwd: str) -> tuple[str, dict | None]:
    """Ask the lead via pluggable LeadAdapter (default Grok CLI).

    Backward-compatible entry: when *prompt* is already a formatted string and no
    structured request is supplied, wrap it as a review/permission-agnostic ask.
    Prefer call_lead_request() for 条3 full-context binding.

    Never retries with disallowed-tools stripped. Timeout/call failure → status envelope.
    """
    from lead_adapter import get_lead_adapter, build_lead_request

    adapter = get_lead_adapter()
    # Legacy path: treat free-form prompt as current_application text under a fresh request.
    request = build_lead_request(
        kind="permission" if "decision" in json.dumps(schema) and "verdict" not in json.dumps(schema) else "review",
        goal="(legacy call_lead prompt)",
        authorized_scope=["workspace"],
        prohibitions=["always-approve", "secret exfiltration"],
        acceptance_criteria={},
        current_application={"legacy_prompt": prompt[:8000]},
    )
    # Preserve caller prompt as the primary text while still binding application_id in schema expectations.
    from lead_adapter.schema import format_lead_request_prompt

    # If caller already built a full prompt, still send structured request via adapter.decide
    # but merge legacy prompt into request extra for adapters that format themselves.
    request = dict(request)
    request["extra"] = {"legacy_prompt": prompt}
    # For grok_cli, decide() formats from request; inject legacy prompt as task overlay
    request["task_goal"] = prompt[:500]
    return adapter.decide(request, schema=schema, cwd=cwd, timeout_sec=180)


def call_lead_request(
    request: dict,
    *,
    schema: dict,
    cwd: str,
    timeout_sec: float = 180,
    adapter=None,
) -> tuple[str, dict | None]:
    """Structured lead call (条3). Returns (raw, parsed_or_status_envelope)."""
    from lead_adapter import get_lead_adapter

    ad = adapter or get_lead_adapter()
    return ad.decide(request, schema=schema, cwd=cwd, timeout_sec=timeout_sec)



def session_busy(status_obj, sid: str) -> bool:
    if not status_obj:
        return False
    if isinstance(status_obj, dict):
        # shapes: {sid: {type:busy}} or list
        if sid in status_obj:
            st = status_obj[sid]
            if isinstance(st, dict):
                return st.get("type") == "busy" or st.get("status") == "busy"
        for v in status_obj.values():
            if isinstance(v, dict) and v.get("sessionID") == sid and v.get("type") == "busy":
                return True
        return False
    if isinstance(status_obj, list):
        for v in status_obj:
            if isinstance(v, dict) and (v.get("sessionID") == sid or v.get("id") == sid):
                if v.get("type") == "busy" or v.get("status") == "busy":
                    return True
    return False


def last_assistant(messages):
    if not isinstance(messages, list):
        return None
    for m in reversed(messages):
        info = m.get("info") if isinstance(m, dict) else None
        if not isinstance(info, dict):
            # sometimes role at top
            if isinstance(m, dict) and m.get("role") == "assistant":
                return m
            continue
        if info.get("role") == "assistant":
            return m
    return None


def assistant_finish(msg) -> str | None:
    if not msg:
        return None
    info = msg.get("info") if isinstance(msg, dict) else None
    if isinstance(info, dict):
        return info.get("finish") or (info.get("error") and "error")
    return msg.get("finish")


def assistant_error(msg) -> str:
    if not msg:
        return ""
    info = msg.get("info") if isinstance(msg, dict) else {}
    if isinstance(info, dict) and info.get("error"):
        return redact(info.get("error"))
    return ""


def expected_exists(paths: list[str]) -> list[str]:
    """Inventory helper only — does not imply job success (see completion.artifacts_all_present)."""
    from completion import expected_exists as _ee
    return _ee(paths)



def session_id_of_permission(p: dict) -> str:
    for k in ("sessionID", "session_id", "sessionId"):
        v = p.get(k) if isinstance(p, dict) else None
        if v:
            return str(v)
    meta = p.get("metadata") if isinstance(p, dict) and isinstance(p.get("metadata"), dict) else {}
    for k in ("sessionID", "session_id", "sessionId"):
        v = meta.get(k)
        if v:
            return str(v)
    return ""


def _reconfirm_and_reply(sid: str, pid: str, reply: str, original: dict | None, report: dict, via: str, **extra) -> bool:
    """Reconfirm still-pending + session + fingerprint; POST reply; mark handled only on success."""
    if reply == "always":
        reply = "once"
        report.setdefault("notes", []).append("coerced always->once at reply boundary")
    code, pending = call("GET", "/permission")
    match = None
    if isinstance(pending, list):
        for p in pending:
            if isinstance(p, dict) and str(p.get("id") or p.get("requestID") or "") == pid:
                match = p
                break
    if match is None:
        report.setdefault("notes", []).append(f"reconfirm: no_longer_pending id={pid}")
        report.setdefault("api_replies", []).append(
            {"id": pid, "reply": reply, "via": via, "reconfirm": "no_longer_pending", "skipped_reply": True, **extra}
        )
        return False
    msid = session_id_of_permission(match)
    if msid and sid and msid != sid:
        report.setdefault("notes", []).append(f"reconfirm: session_mismatch id={pid}")
        report.setdefault("api_replies", []).append(
            {"id": pid, "reply": reply, "via": via, "reconfirm": "session_mismatch", "skipped_reply": True, **extra}
        )
        return False
    if original is not None and permission_fingerprint(match) != permission_fingerprint(original):
        report.setdefault("notes", []).append(f"reconfirm: content_changed id={pid}")
        report.setdefault("api_replies", []).append(
            {"id": pid, "reply": reply, "via": via, "reconfirm": "content_changed", "skipped_reply": True, **extra}
        )
        return False
    rc, rj = call("POST", f"/permission/{pid}/reply", body={"reply": reply})
    entry = {"id": pid, "reply": reply, "via": via, "http": rc, "reconfirm": "ok", **extra}
    if rj:
        entry["body"] = redact(rj)
    report.setdefault("api_replies", []).append(entry)
    if not (isinstance(rc, int) and rc < 300):
        report.setdefault("notes", []).append(f"reply POST failed http={rc}; not marking handled")
        return False
    return True


def _handle_permission_for_session(
    p: dict,
    *,
    sid: str,
    ws: str,
    name: str,
    job_charter: dict,
    worker_intent,
    blocker,
    ping_deduper: PingDeduper,
    charter_sent_full: bool,
    handled_perm_ids: set,
    report: dict,
) -> tuple[bool, bool]:
    """Process one permission strictly for *sid*. Returns (handled_ok, charter_sent_full)."""
    if not isinstance(p, dict):
        return False, charter_sent_full
    psid = session_id_of_permission(p)
    if psid and psid != sid:
        return False, charter_sent_full  # never touch other sessions
    pid = str(p.get("id") or p.get("requestID") or "")
    if not pid or pid in handled_perm_ids:
        return False, charter_sent_full
    if ping_deduper.already_handled(pid):
        return False, charter_sent_full

    summary = summarize_permission(p)
    report["pending_summaries"].append(summary)
    perm_obj = p
    hr = hard_rule_decision(perm_obj, charter=job_charter)
    if hr and hr.get("reply") == "reject":
        if _reconfirm_and_reply(sid, pid, "reject", perm_obj, report, "hard_rule"):
            report["hard_rule_rejects"].append({"id": pid, "reason": hr.get("reason", "")})
            report["notes"].append(f"hard_rule reject: {hr.get('reason', '')}")
            report["grok_permission_decision"] = "reject"
            handled_perm_ids.add(pid)
            ping_deduper.mark_replied_id(pid)
        return True, charter_sent_full
    if perm_obj.get("_hard_rule_allowlisted"):
        if _reconfirm_and_reply(sid, pid, "once", perm_obj, report, "hard_rule_allowlisted"):
            report["notes"].append(
                "hard_rule allowlisted (charter allow_secret_globs/allow_paths): "
                "once — legitimate small-risk secret path; logged, no lead"
            )
            report["grok_permission_decision"] = "once"
            handled_perm_ids.add(pid)
            ping_deduper.mark_replied_id(pid)
        return True, charter_sent_full

    intent = (worker_intent or "").strip() or (
        f"Worker requests permission to continue job {name!r} "
        f"inside workspace; tool/path from pending (see proposed_action)."
    )
    blk = blocker if blocker is not None else {
        "failed_path": "permission_gate",
        "detail": "worker awaiting lead decision on pending permission",
    }
    include_full = not charter_sent_full
    packet = packet_from_permission(
        perm_obj,
        worker_intent=intent,
        blocker=blk,
        charter=job_charter,
        include_charter_full=include_full,
        ping_reason="permission",
        risk_tags=["permission"],
    )
    if include_full:
        charter_sent_full = True
    missing = [
        req_k
        for req_k in ("worker_intent", "blocker", "charter_ref")
        if req_k not in packet or packet[req_k] in (None, "", {})
    ]
    if missing:
        report["notes"].append(f"packet missing {missing}; forcing reject")
        if _reconfirm_and_reply(sid, pid, "reject", perm_obj, report, "incomplete_packet"):
            handled_perm_ids.add(pid)
            ping_deduper.mark_replied_id(pid)
        return True, charter_sent_full

    pa = packet.get("proposed_action") or {}
    tclass = pa.get("target_class") or "unknown"
    path_guess = pa.get("target") or ""
    tool_guess = pa.get("tool") or ""
    targets = pa.get("targets") or ([path_guess] if path_guess else [])
    ws_can = canonicalize(ws)
    in_ws = all(is_path_within(canonicalize(t, base=ws_can), ws_can) for t in targets) if targets else False
    # No unconditional once — even ordinary R/W goes to lead
    if (
        not should_ping_lead(
            ping_reason="permission",
            tool=tool_guess,
            path=path_guess,
            target_class=tclass,
        )
        and in_ws
    ):
        report["notes"].append("ordinary workspace R/W — still requires lead approval (no auto-once)")

    if not ping_deduper.should_emit_id(pid):
        return False, charter_sent_full
    ping_deduper.mark_inflight_id(pid)
    allow_hint = (
        f"Allowed workspace only: {ws_can}. "
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
        goal=job_charter.get("goal") or f"job {name}",
        authorized_scope=job_charter.get("must") or [f"stay inside workspace {ws}"],
        prohibitions=job_charter.get("must_not") or [],
        acceptance_criteria=job_charter.get("acceptance") or job_charter.get("done_when") or {},
        current_application=packet,
        charter=job_charter,
    )
    schema = lead_permission_response_schema()
    # Keep allow_hint in request extra for prompt formatting
    req = dict(req)
    req["extra"] = {"allow_hint": allow_hint, "legacy_packet_prompt": format_lead_prompt(packet, allow_hint=allow_hint)}
    raw, parsed = call_lead_request(req, schema=schema, cwd=ws)
    report["grok_permission_raw"] = raw
    try:
        validated = validate_lead_decision(raw, parsed, request=req, kind="permission")
        decision = validated.get("decision")
        if validated.get("_coerced_always"):
            report["notes"].append("lead said always → coerced to once")
    except LeadDecisionError as e:
        # 条3: illegal/timeout/call failure → keep pending OR safe-stop. Never strip tool limits to retry.
        report["notes"].append(f"lead decision invalid ({e.code}): {e}")
        if e.code in ("timeout", "call_failed"):
            report["notes"].append("keeping permission pending (no reply)")
            ping_deduper.clear_inflight_id(pid)
            return False, charter_sent_full
        decision = "reject"  # illegal JSON / mismatch → safe-stop reject
    if decision not in ("once", "reject", "deny_job", "demand_safe_path"):
        decision = "reject"
    if decision == "once" and tclass in ("user_secret_store", "env_file", "browser_profile"):
        # still forbid once for secret-adjacent if lead somehow returned once after always coerce edge
        pass
    if decision == "always":
        report["notes"].append("lead said always → coerced to once")
        decision = "once"
        if tclass in ("user_secret_store", "env_file", "browser_profile"):
            decision = "reject"
            report["notes"].append("secret-adjacent always forbidden → reject")
    report["grok_permission_decision"] = decision
    api_reply, map_note = map_lead_decision_to_api(decision)
    if map_note:
        report["notes"].append(map_note)
    if decision == "deny_job":
        if _reconfirm_and_reply(sid, pid, "reject", perm_obj, report, "deny_job", lead_decision=decision):
            handled_perm_ids.add(pid)
            ping_deduper.mark_replied_id(pid)
        report["state"] = "fail"
        report["error"] = "lead deny_job"
        return True, charter_sent_full
    if _reconfirm_and_reply(
        sid, pid, api_reply, perm_obj, report, "lead", lead_decision=decision
    ):
        handled_perm_ids.add(pid)
        ping_deduper.mark_replied_id(pid)
    return True, charter_sent_full


def run_job(
    name: str,
    instruction: str,
    expected_artifacts: list[str],
    force_lead_review: bool = False,
    timeout_sec: int = 300,
    charter: dict | None = None,
    worker_intent: str | None = None,
    blocker: dict | str | None = None,
    workspace: str | Path | None = None,
) -> dict:
    """Run one TeleAgent job. workspace= isolates files per job_id (parallel-safe)."""
    ws_path = Path(workspace) if workspace else COLLAB
    ws_path.mkdir(parents=True, exist_ok=True)
    ws = str(ws_path)
    # Charter for decision packets (first ping may include full text; later charter_ref only).
    job_charter = charter or {
        "goal": (instruction or "")[:240],
        "must": [f"stay inside workspace {ws}"],
        "must_not": [
            "read or copy credentials, tokens, cookies, or secret files",
            "use always-approve / yolo",
        ],
        "allowed_surfaces": ["workspace_fs", "lead_approved_tools"],
    }
    ping_deduper = PingDeduper()
    charter_sent_full = False
    report = {
        "name": name,
        "session_id": "",
        "pending_seen": False,
        "pending_summaries": [],
        "grok_permission_raw": "",
        "grok_permission_decision": "",
        "api_replies": [],
        "grok_review_raw": "",
        "grok_review_decision": "",
        "artifacts": [],
        "state": "fail",
        "ok": False,
        "error": "",
        "path": "permission_api" if not force_lead_review else "lead_review",
        "notes": [],
        "hard_rule_rejects": [],
    }

    code, created = call(
        "POST",
        "/session",
        body={"title": f"collab-{name}", "directory": ws},
        extra_headers={"x-opencode-directory": ws},
    )
    if code >= 300 or not isinstance(created, dict) or not created.get("id"):
        report["error"] = f"create session failed: {code} {redact(created)}"
        return report
    sid = created["id"]
    report["session_id"] = sid

    code, _ = call(
        "POST",
        f"/session/{sid}/prompt_async",
        body=prompt_body(instruction),
        extra_headers={"x-opencode-directory": ws},
    )
    if code not in (200, 204) and code >= 300:
        report["error"] = f"prompt_async failed: {code}"
        return report

    from completion import (
        ReworkBudget,
        artifacts_all_present,
        build_acceptance_packet,
        confirm_artifacts_for_lead_approve,
        format_acceptance_prompt,
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

    budget = ReworkBudget.start(timeout_sec, max_reworks=int((charter or {}).get("max_reworks") or 1))
    report["rework_budget"] = budget.to_dict()
    handled_perm_ids = set()

    def _all_arts_ok() -> bool:
        recs = snapshot_artifacts(expected_artifacts)
        report["artifacts"] = [r.path for r in recs if r.exists]
        report["artifact_records"] = [r.to_dict() for r in recs]
        return artifacts_all_present(recs)

    def _do_lead_review(*, phase: str) -> str:
        """Submit acceptance packet; confirm fingerprints; return verdict pass|fail|error."""
        arts_ok = _all_arts_ok()
        packet = build_acceptance_packet(
            job_name=name,
            goal=job_charter.get("goal") or instruction[:240],
            acceptance_criteria=job_charter.get("acceptance")
            or job_charter.get("done_when")
            or {"artifacts": expected_artifacts},
            expected_artifacts=expected_artifacts,
            execution_result={
                "finish": report.get("finish"),
                "pending_seen": report.get("pending_seen"),
                "path": report.get("path"),
                "phase": phase,
            },
            error=report.get("error") or "",
            tool_records=report.get("pending_summaries") or [],
            api_replies=report.get("api_replies") or [],
            notes=report.get("notes") or [],
            state=report.get("state") or "",
            session_id=sid,
        )
        report["acceptance_packet"] = packet
        if not arts_ok:
            report["notes"].append("acceptance blocked: required artifacts incomplete")
            return "fail"
        unchanged, gate = confirm_artifacts_for_lead_approve(packet)
        report["artifact_gate"] = gate
        if not unchanged:
            report["notes"].append(f"artifact fingerprint changed before lead: {gate.get('diffs')}")
            return "fail"

        req = build_lead_request(
            kind="review",
            goal=job_charter.get("goal") or instruction[:240],
            authorized_scope=job_charter.get("must") or [f"stay inside workspace {ws}"],
            prohibitions=job_charter.get("must_not") or [],
            acceptance_criteria=packet.get("acceptance_criteria"),
            current_application=packet,
            charter=job_charter,
        )
        schema = lead_review_response_schema()
        # Also embed human-readable acceptance prompt for legacy adapters
        prompt = format_acceptance_prompt(packet, charter=job_charter)
        req = dict(req)
        req["extra"] = {"acceptance_prompt": prompt}
        raw, parsed = call_lead_request(req, schema=schema, cwd=ws)
        report["grok_review_raw"] = (report.get("grok_review_raw") or "") + (
            f"\n---{phase}---\n" if report.get("grok_review_raw") else ""
        ) + (raw or "")
        try:
            decision = validate_lead_decision(raw, parsed, request=req, kind="review")
        except LeadDecisionError as e:
            report["notes"].append(f"lead review invalid ({e.code}): {e} → fail/safe-stop")
            report["grok_review_decision"] = "fail"
            return "fail"
        verdict = decision.get("verdict") or "fail"
        report["grok_review_decision"] = verdict
        # Reconfirm artifacts unchanged after lead returns (approve gate)
        unchanged2, gate2 = confirm_artifacts_for_lead_approve(packet)
        report["artifact_gate_post"] = gate2
        if verdict == "pass" and not unchanged2:
            report["notes"].append(f"artifacts changed during lead review: {gate2.get('diffs')}")
            return "fail"
        return verdict

    def _mark_success(verdict: str | None = None) -> dict:
        arts_ok = _all_arts_ok()
        ok, why = is_success_allowed(
            state="ok",
            artifacts_ok=arts_ok,
            lead_verdict=verdict if force_lead_review else (verdict or "pass"),
            force_lead_review=force_lead_review,
            finish=report.get("finish"),
            error=report.get("error"),
        )
        if ok:
            report["ok"] = True
            report["state"] = "ok"
            report["error"] = ""
        else:
            report["ok"] = False
            report["state"] = "fail"
            report["error"] = why
        report["rework_budget"] = budget.to_dict()
        _write_status(name, report)
        return report

    while not budget.exhausted_wall():
        sc, status = call("GET", "/session/status")
        pc, pending = call("GET", "/permission")
        if isinstance(pending, list) and pending:
            report["pending_seen"] = True
            mine = []
            for p in pending:
                if not isinstance(p, dict):
                    continue
                psid = session_id_of_permission(p)
                if psid and psid != sid:
                    continue
                if not psid:
                    report["notes"].append("skip permission without sessionID")
                    continue
                mine.append(p)
            report["notes"].append(f"pending count={len(pending)} mine={len(mine)}")
            for p in mine:
                handled, charter_sent_full = _handle_permission_for_session(
                    p,
                    sid=sid,
                    ws=ws,
                    name=name,
                    job_charter=job_charter,
                    worker_intent=worker_intent,
                    blocker=blocker,
                    ping_deduper=ping_deduper,
                    charter_sent_full=charter_sent_full,
                    handled_perm_ids=handled_perm_ids,
                    report=report,
                )
                if report.get("error") == "lead deny_job":
                    _write_status(name, report)
                    return report

        busy = session_busy(status, sid)
        # NOTE: no early-accept / timeout-with-partial-arts success (条2).
        if busy:
            time.sleep(1.5)
            continue

        mc, msgs = call("GET", f"/session/{sid}/message")
        asst = last_assistant(msgs)
        fin = assistant_finish(asst)
        err = assistant_error(asst)
        report["finish"] = fin
        arts_ok = _all_arts_ok()
        if err or fin == "error":
            report["state"] = "fail"
            report["error"] = err or "assistant finish=error"
            report["ok"] = False
            _write_status(name, report)
            return report
        if fin in ("cancelled", "cancel"):
            report["state"] = "cancelled"
            report["error"] = f"finish={fin}"
            report["ok"] = False
            _write_status(name, report)
            return report

        # Need stop (or idle with full artifacts) before judging
        if fin != "stop" and not arts_ok:
            time.sleep(1.5)
            continue
        if fin != "stop" and arts_ok:
            # brief settle; still require eventual stop or proceed to review only if forced
            time.sleep(1.0)
            if session_busy(call("GET", "/session/status")[1], sid):
                continue

        # force_lead_review always applies (serial path); also when charter requests it
        need_review = bool(force_lead_review)
        if need_review or (name == "B" and not report["pending_seen"]):
            # keep B fallback only when force not set? 条2: force must work; B heuristic retained
            # but never succeed without full arts
            if not force_lead_review and name == "B" and not report["pending_seen"]:
                need_review = True
                report["path"] = "lead_review_fallback"

        if need_review:
            report["path"] = "lead_review" if force_lead_review else report.get("path") or "lead_review"
            verdict = _do_lead_review(phase="review1")
            if verdict == "pass" and _all_arts_ok():
                return _mark_success("pass")
            # rework under budget — wall deadline NEVER extended
            if not budget.consume_rework():
                report["ok"] = False
                report["state"] = "fail"
                report["error"] = f"lead verdict={verdict}; rework budget exhausted; missing={missing_artifacts(expected_artifacts)}"
                report["rework_budget"] = budget.to_dict()
                _write_status(name, report)
                return report
            report["notes"].append("lead fail or missing arts -> rework (budget-limited)")
            call(
                "POST",
                f"/session/{sid}/prompt_async",
                body=prompt_body(
                    "Lead rejected previous delivery. Fix now. "
                    f"Reason: {(report.get('grok_review_raw') or '')[:500]}. "
                    f"Original job: {instruction}"
                ),
                extra_headers={"x-opencode-directory": ws},
            )
            redo_deadline = budget.clamp_subdeadline(180)
            while time.time() < redo_deadline and not budget.exhausted_wall():
                sc, status = call("GET", "/session/status")
                pc, pending = call("GET", "/permission")
                if isinstance(pending, list) and pending:
                    report["pending_seen"] = True
                    report["notes"].append("redo phase: permissions go through full approval (no auto-once)")
                    for p in pending:
                        if not isinstance(p, dict):
                            continue
                        handled, charter_sent_full = _handle_permission_for_session(
                            p,
                            sid=sid,
                            ws=ws,
                            name=name,
                            job_charter=job_charter,
                            worker_intent=worker_intent,
                            blocker=blocker,
                            ping_deduper=ping_deduper,
                            charter_sent_full=charter_sent_full,
                            handled_perm_ids=handled_perm_ids,
                            report=report,
                        )
                        if report.get("error") == "lead deny_job":
                            break
                if not session_busy(status, sid):
                    break
                time.sleep(1.5)
            mc, msgs = call("GET", f"/session/{sid}/message")
            asst = last_assistant(msgs)
            report["finish"] = assistant_finish(asst)
            verdict2 = _do_lead_review(phase="review2")
            if verdict2 == "pass" and _all_arts_ok():
                return _mark_success("pass")
            report["ok"] = False
            report["state"] = "fail"
            report["error"] = (
                f"lead verdict={verdict2} arts_ok={_all_arts_ok()} "
                f"missing={missing_artifacts(expected_artifacts)} finish={report.get('finish')}"
            )
            report["rework_budget"] = budget.to_dict()
            _write_status(name, report)
            return report

        # Non-force path: require ALL artifacts + finish=stop (no or True)
        if _all_arts_ok() and fin == "stop":
            return _mark_success(None)
        if _all_arts_ok() and not fin:
            time.sleep(1)
            continue
        if fin == "stop" and not _all_arts_ok():
            report["state"] = "fail"
            report["ok"] = False
            report["error"] = f"finish=stop but artifacts incomplete: missing={missing_artifacts(expected_artifacts)}"
            _write_status(name, report)
            return report
        time.sleep(1.5)

    # Wall timeout — NEVER success (条2)
    report["state"] = "timeout"
    report["error"] = "wall clock timeout"
    report["ok"] = False
    _all_arts_ok()
    report["rework_budget"] = budget.to_dict()
    report["notes"].append("timeout is not success even if partial artifacts exist")
    _write_status(name, report)
    return report


def _write_status(name: str, report: dict):
    out = {
        "ok": report.get("ok", False),
        "state": report.get("state"),
        "session_id": report.get("session_id"),
        "exit_code": None,
        "artifacts": report.get("artifacts", []),
        "log_path": "",
        "pending_permissions": report.get("pending_summaries", []),
        "error": report.get("error", ""),
        "grok_permission_decision": report.get("grok_permission_decision"),
        "grok_review_decision": report.get("grok_review_decision"),
        "path": report.get("path"),
    }
    (COLLAB / f"status-{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    COLLAB.mkdir(parents=True, exist_ok=True)
    results = []

    # Task A
    a_path = str(COLLAB / "hello-from-worker.txt")
    if Path(a_path).exists():
        Path(a_path).unlink()
    results.append(
        run_job(
            "A",
            (
                "In the current workspace only, create file hello-from-worker.txt "
                "with exactly one line of text (any short greeting). Do not touch other directories. "
                "When done, stop."
            ),
            [a_path],
            force_lead_review=False,
            timeout_sec=240,
        )
    )

    # Task B
    b_script = str(COLLAB / "sum.sh")
    for p in (b_script,):
        if Path(p).exists():
            Path(p).unlink()
    results.append(
        run_job(
            "B",
            (
                "In the current workspace only: write a small shell script sum.sh that prints exactly "
                "'1+2=3' (compute 1+2 in the script), then execute it with bash and show the output. "
                "Do not touch other directories. When done, stop."
            ),
            [b_script],
            force_lead_review=True,
            timeout_sec=360,
        )
    )

    # Write run-report.md
    lines = [
        "# TeleAgent × Grok Build collab run report",
        "",
        "- Roles: TeleAgent=worker (pinned); lead=pluggable (this run: `" + LEAD_NAME + " via `" + LEAD_BIN + "`)",
        "- Workspace: `/workspace/teleagent/probe-sandbox/collab/`",
        "- No `--always-approve` / yolo / global auto-approve",
        "- Secrets redacted; API keys not written",
        "",
    ]
    for r in results:
        lines += [
            f"## Task {r['name']}",
            "",
            f"- session_id: `{r.get('session_id')}`",
            f"- pending appeared: **{r.get('pending_seen')}**",
            f"- path: `{r.get('path')}`",
            f"- Grok permission decision: `{r.get('grok_permission_decision') or 'n/a'}`",
            f"- Grok permission raw (truncated):",
            "",
            "```",
            (r.get("grok_permission_raw") or "(none)")[:1500],
            "```",
            "",
            f"- API replies: `{redact(r.get('api_replies'))}`",
            f"- Grok review decision: `{r.get('grok_review_decision') or 'n/a'}`",
            f"- Grok review raw (truncated):",
            "",
            "```",
            (r.get("grok_review_raw") or "(none)")[:1500],
            "```",
            "",
            f"- artifacts: `{r.get('artifacts')}`",
            f"- state: **{r.get('state')}** ok={r.get('ok')}",
            f"- error: {r.get('error') or '(none)'}",
            f"- notes: {r.get('notes')}",
            "",
        ]
    a_ok = results[0].get("ok")
    b_ok = results[1].get("ok")
    collab_ok = bool(a_ok and b_ok)
    lines += [
        "## Summary",
        "",
        f"- Task A: {'PASS' if a_ok else 'FAIL'}",
        f"- Task B: {'PASS' if b_ok else 'FAIL'}",
        f"- Collaboration established: {'YES' if collab_ok else 'PARTIAL/NO'} "
        f"(worker HTTP delivery + lead Grok decision loop; pending API path "
        f"{'was' if any(r.get('pending_seen') for r in results) else 'was NOT'} exercised)",
        "",
    ]
    (COLLAB / "run-report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"A": a_ok, "B": b_ok, "report": str(COLLAB / "run-report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
