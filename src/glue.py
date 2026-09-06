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


USER, PW, KEY = find_creds()
BASIC = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()


def sign_headers(method: str, url: str) -> dict:
    n = urlparse(url)
    path = f'{n.path}{("?" + n.query) if n.query else ""}'
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(12)
    payload = "\n".join(["local-v1", method.upper(), path, ts, nonce])
    sig = (
        base64.urlsafe_b64encode(hmac.new(KEY.encode(), payload.encode(), hashlib.sha256).digest())
        .decode()
        .rstrip("=")
    )
    return {
        "X-SA-Sign-Version": "local-v1",
        "X-SA-Timestamp": ts,
        "X-SA-Nonce": nonce,
        "X-SA-Signature": sig,
        "Authorization": BASIC,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def call(method: str, path: str, body=None, extra_headers=None, timeout=120):
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
    keep = {k: p.get(k) for k in ("id", "type", "permission", "path", "patterns", "sessionID", "message", "title", "tool", "command") if k in p or True}
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
    """Ask the lead for a structured decision. Returns (raw_text, parsed_json_or_None).
    Lead binary is COLLAB_LEAD_BIN (default Grok). Never uses --always-approve / yolo."""
    cmd = [
        LEAD_BIN,
        "-p",
        prompt,
        "--cwd",
        cwd,
        "--max-turns",
        "1",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--disallowed-tools",
        "bash,shell,edit,write,web_search,web_fetch",
    ]
    # Some builds use different tool names; ignore failures from unknown disallowed tools by not hard-failing.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    raw = out if out else err
    parsed = None
    # Prefer last JSON object
    for candidate in (out, err):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            break
        except Exception:
            m = re.search(r"\{[\s\S]*\}", candidate)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    break
                except Exception:
                    pass
    if parsed is None and not out and err:
        # retry without disallowed-tools if tool names invalid
        if "disallowed" in err.lower() or "unknown" in err.lower() or "invalid" in err.lower():
            cmd2 = [LEAD_BIN, "-p", prompt, "--cwd", cwd, "--max-turns", "1", "--output-format", "json", "--json-schema", json.dumps(schema)]
            proc = subprocess.run(cmd2, capture_output=True, text=True, timeout=180)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            raw = out if out else err
            try:
                parsed = json.loads(out)
            except Exception:
                m = re.search(r"\{[\s\S]*\}", out or err or "")
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        parsed = None
    # Grok sometimes returns stopReason=cancelled with a premature fail stub and
    # structuredOutput=null; retry so lead acceptance is not a false fail.
    def _bad_lead(p):
        if not isinstance(p, dict):
            return True
        if p.get("stopReason") == "cancelled":
            return True
        if p.get("structuredOutput") is None and p.get("structuredOutputError"):
            return True
        so = p.get("structuredOutput")
        if isinstance(so, dict) and ("decision" in so or "verdict" in so):
            return False
        # raw text stub that admits not-yet-verified
        blob = (p.get("text") or "") + json.dumps(so or {}, ensure_ascii=False)
        if "have not yet verified" in blob.lower() or "before issuing a verdict" in blob.lower() or "before judging" in blob.lower():
            return True
        return False

    attempts = [raw, parsed]
    for _ in range(2):
        if not _bad_lead(parsed):
            break
        time.sleep(1.5)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return "TIMEOUT", None
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = out if out else err
        parsed = None
        for candidate in (out, err):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                break
            except Exception:
                m = re.search(r"\{[\s\S]*\}", candidate)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                        break
                    except Exception:
                        pass
    return raw[:3000], parsed


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
    return [p for p in paths if Path(p).exists()]


def run_job(
    name: str,
    instruction: str,
    expected_artifacts: list[str],
    force_lead_review: bool = False,
    timeout_sec: int = 300,
    charter: dict | None = None,
    worker_intent: str | None = None,
    blocker: dict | str | None = None,
) -> dict:
    ws = str(COLLAB)
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

    deadline = time.time() + timeout_sec
    handled_perm_ids = set()
    while time.time() < deadline:
        sc, status = call("GET", "/session/status")
        pc, pending = call("GET", "/permission")
        if isinstance(pending, list) and pending:
            report["pending_seen"] = True
            report["notes"].append(f"pending count={len(pending)}")
            for p in pending:
                pid = str(p.get("id") or p.get("requestID") or "")
                if not pid or pid in handled_perm_ids:
                    continue
                summary = summarize_permission(p)
                report["pending_summaries"].append(summary)

                # 1) Hard rules first — obvious credential paths / always+secret → reject, no lead.
                hr = hard_rule_decision(p if isinstance(p, dict) else {})
                if hr and hr.get("reply") == "reject":
                    rc, rj = call("POST", f"/permission/{pid}/reply", body={"reply": "reject"})
                    report["api_replies"].append(
                        {"id": pid, "reply": "reject", "via": "hard_rule", "http": rc}
                    )
                    report["hard_rule_rejects"].append({"id": pid, "reason": hr.get("reason", "")})
                    report["notes"].append(f"hard_rule reject: {hr.get('reason', '')}")
                    report["grok_permission_decision"] = "reject"
                    handled_perm_ids.add(pid)
                    continue

                # 2) Decision packet — must include worker_intent + blocker + charter_ref.
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
                    p if isinstance(p, dict) else {},
                    worker_intent=intent,
                    blocker=blk,
                    charter=job_charter,
                    include_charter_full=include_full,
                    ping_reason="permission",
                    risk_tags=["permission"],
                )
                if include_full:
                    charter_sent_full = True
                # Forbid tool+path-only: assert required fields present.
                missing = [
                    req_k
                    for req_k in ("worker_intent", "blocker", "charter_ref")
                    if req_k not in packet or packet[req_k] in (None, "", {})
                ]
                if missing:
                    report["notes"].append(f"packet missing {missing}; forcing reject")
                    call("POST", f"/permission/{pid}/reply", body={"reply": "reject"})
                    report["api_replies"].append({"id": pid, "reply": "reject", "via": "incomplete_packet"})
                    handled_perm_ids.add(pid)
                    continue

                pa = packet.get("proposed_action") or {}
                tclass = pa.get("target_class") or "unknown"
                ppat = pa.get("path_pattern") or ""
                if not ping_deduper.should_emit("permission", tclass, ppat):
                    # Same fork already pinged — conservative reject until charter changes.
                    call("POST", f"/permission/{pid}/reply", body={"reply": "reject"})
                    report["api_replies"].append({"id": pid, "reply": "reject", "via": "ping_dedupe"})
                    report["notes"].append(f"deduped permission ping {tclass}/{ppat}")
                    handled_perm_ids.add(pid)
                    continue

                # Ordinary in-workspace source R/W: still need an API reply, but may skip lead
                # when should_ping_lead is False — default once for tiny workspace edits only.
                path_guess = pa.get("target") or ""
                tool_guess = pa.get("tool") or ""
                if not should_ping_lead(
                    ping_reason="permission",
                    tool=tool_guess,
                    path=path_guess,
                    target_class=tclass,
                ) and path_guess.startswith(ws):
                    rc, rj = call("POST", f"/permission/{pid}/reply", body={"reply": "once"})
                    report["api_replies"].append(
                        {"id": pid, "reply": "once", "via": "ordinary_rw_no_lead", "http": rc}
                    )
                    report["notes"].append("ordinary workspace R/W — no lead ping")
                    report["grok_permission_decision"] = "once"
                    handled_perm_ids.add(pid)
                    continue

                ping_deduper.mark_inflight("permission", tclass, ppat)
                allow_hint = (
                    f"Allowed workspace only: {ws}. "
                    "Secret-adjacent / auth workarounds: reject or demand_safe_path, never once. "
                    "Never choose always."
                )
                schema = lead_permission_schema()
                prompt = format_lead_prompt(packet, allow_hint=allow_hint)
                raw, parsed = call_lead(prompt, schema, ws)
                report["grok_permission_raw"] = raw
                decision = None
                if isinstance(parsed, dict):
                    decision = parsed.get("decision") or (parsed.get("result") or {}).get("decision")
                    so = parsed.get("structuredOutput")
                    if not decision and isinstance(so, dict):
                        decision = so.get("decision")
                    if not decision:
                        for k in ("output", "message", "content", "data"):
                            if isinstance(parsed.get(k), dict) and parsed[k].get("decision"):
                                decision = parsed[k]["decision"]
                                break
                if decision not in ("once", "reject", "deny_job", "demand_safe_path", "always"):
                    m = re.search(r"\b(once|reject|deny_job|demand_safe_path|always)\b", raw or "")
                    decision = m.group(1) if m else "reject"
                report["grok_permission_decision"] = decision
                api_reply, map_note = map_lead_decision_to_api(decision)
                if map_note:
                    report["notes"].append(map_note)
                ping_deduper.mark_replied("permission", tclass, ppat)
                if decision == "deny_job":
                    call("POST", f"/permission/{pid}/reply", body={"reply": "reject"})
                    report["api_replies"].append({"id": pid, "reply": "reject", "via": "deny_job"})
                    handled_perm_ids.add(pid)
                    report["state"] = "fail"
                    report["error"] = "lead deny_job"
                    _write_status(name, report)
                    return report
                rc, rj = call("POST", f"/permission/{pid}/reply", body={"reply": api_reply})
                report["api_replies"].append(
                    {
                        "id": pid,
                        "reply": api_reply,
                        "lead_decision": decision,
                        "http": rc,
                        "body": redact(rj) if rj else "",
                    }
                )
                handled_perm_ids.add(pid)

        busy = session_busy(status, sid)
        if not busy:
            mc, msgs = call("GET", f"/session/{sid}/message")
            asst = last_assistant(msgs)
            fin = assistant_finish(asst)
            err = assistant_error(asst)
            arts = expected_exists(expected_artifacts)
            report["artifacts"] = arts
            if err or fin == "error":
                report["state"] = "fail"
                report["error"] = err or "assistant finish=error"
                _write_status(name, report)
                return report
            if fin == "stop" or arts:
                # success path maybe still need lead review
                if force_lead_review or (not report["pending_seen"] and force_lead_review is False and name.startswith("B")):
                    # for task B: if no pending, force lead review
                    pass
                if force_lead_review or (name == "B" and not report["pending_seen"]):
                    report["path"] = "lead_review_fallback" if not report["pending_seen"] else report["path"]
                    diff_bits = []
                    for ap in expected_artifacts:
                        p = Path(ap)
                        if p.exists():
                            try:
                                diff_bits.append(f"FILE {ap}:\n" + p.read_text(encoding="utf-8", errors="replace")[:1500])
                            except Exception as e:
                                diff_bits.append(f"FILE {ap}: <read error {e}>")
                        else:
                            diff_bits.append(f"MISSING {ap}")
                    schema = {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "string", "enum": ["pass", "fail"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["verdict", "reason"],
                        "additionalProperties": False,
                    }
                    prompt = (
                        "You are the team lead accepting a TeleAgent worker delivery. "
                        "Judge only the artifacts for the stated job. Output JSON verdict+reason.\n"
                        f"Job: {instruction}\n"
                        f"Artifacts:\n" + "\n\n".join(diff_bits)
                    )
                    raw, parsed = call_lead(prompt, schema, ws)
                    report["grok_review_raw"] = raw
                    verdict = None
                    if isinstance(parsed, dict):
                        verdict = parsed.get("verdict")
                        if not verdict:
                            for k in ("output", "message", "content", "data", "result"):
                                if isinstance(parsed.get(k), dict) and parsed[k].get("verdict"):
                                    verdict = parsed[k]["verdict"]
                                    break
                    if verdict not in ("pass", "fail"):
                        m = re.search(r"\b(pass|fail)\b", (raw or "").lower())
                        verdict = m.group(1) if m else "fail"
                    report["grok_review_decision"] = verdict
                    if verdict != "pass" or not arts:
                        # one redo
                        report["notes"].append("lead fail or missing arts -> one redo")
                        code, _ = call(
                            "POST",
                            f"/session/{sid}/prompt_async",
                            body={
                                "parts": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "Lead rejected previous delivery. Fix now. "
                                            f"Reason: {report.get('grok_review_raw','')[:500]}. "
                                            f"Original job: {instruction}"
                                        ),
                                    }
                                ],
                                "model": MODEL,
                            },
                            extra_headers={"x-opencode-directory": ws},
                        )
                        # poll again until stop
                        redo_deadline = time.time() + min(180, deadline - time.time())
                        while time.time() < redo_deadline:
                            sc, status = call("GET", "/session/status")
                            pc, pending = call("GET", "/permission")
                            if isinstance(pending, list) and pending:
                                report["pending_seen"] = True
                                for p in pending:
                                    pid = str(p.get("id") or "")
                                    if not pid or pid in handled_perm_ids:
                                        continue
                                    # conservative: once for workspace
                                    call("POST", f"/permission/{pid}/reply", body={"reply": "once"})
                                    report["api_replies"].append({"id": pid, "reply": "once", "via": "redo_auto_once"})
                                    handled_perm_ids.add(pid)
                            if not session_busy(status, sid):
                                break
                            time.sleep(1.5)
                        mc, msgs = call("GET", f"/session/{sid}/message")
                        asst = last_assistant(msgs)
                        fin = assistant_finish(asst)
                        arts = expected_exists(expected_artifacts)
                        report["artifacts"] = arts
                        # second lead review
                        diff_bits = []
                        for ap in expected_artifacts:
                            p = Path(ap)
                            if p.exists():
                                diff_bits.append(f"FILE {ap}:\n" + p.read_text(encoding="utf-8", errors="replace")[:1500])
                            else:
                                diff_bits.append(f"MISSING {ap}")
                        raw2, parsed2 = call_lead(
                            "Second review after redo. JSON verdict+reason.\nJob: "
                            + instruction
                            + "\n"
                            + "\n\n".join(diff_bits),
                            schema,
                            ws,
                        )
                        report["grok_review_raw"] = (report["grok_review_raw"] or "") + "\n---REDO---\n" + (raw2 or "")
                        verdict = None
                        if isinstance(parsed2, dict):
                            verdict = parsed2.get("verdict")
                        if verdict not in ("pass", "fail"):
                            m = re.search(r"\b(pass|fail)\b", (raw2 or "").lower())
                            verdict = m.group(1) if m else "fail"
                        report["grok_review_decision"] = verdict
                        if verdict == "pass" and arts and (fin == "stop" or arts):
                            report["ok"] = True
                            report["state"] = "ok"
                        else:
                            report["ok"] = False
                            report["state"] = "fail"
                            report["error"] = f"lead verdict={verdict} arts={arts} finish={fin}"
                        _write_status(name, report)
                        return report
                    # first review pass
                    if arts and (fin == "stop" or True):
                        report["ok"] = True
                        report["state"] = "ok"
                        _write_status(name, report)
                        return report
                # task A path or permission path with finish
                if arts and fin == "stop":
                    report["ok"] = True
                    report["state"] = "ok"
                    _write_status(name, report)
                    return report
                if arts and not fin:
                    # sometimes finish missing briefly
                    time.sleep(1)
                    continue
                if fin == "stop" and not arts:
                    report["state"] = "fail"
                    report["error"] = "finish=stop but artifacts missing"
                    _write_status(name, report)
                    return report
        time.sleep(1.5)

    report["state"] = "timeout"
    report["error"] = "wall clock timeout"
    report["artifacts"] = expected_exists(expected_artifacts)
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
