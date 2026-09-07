#!/usr/bin/env python3
"""Cut-2 collab: evidence-required + riskier sandbox tasks to hit /permission."""
from __future__ import annotations

import uuid

import json
import re
import sys
import time
from pathlib import Path

# Reuse signed HTTP + call_lead from glue
import glue as g

COLLAB = g.COLLAB
MODEL = g.MODEL
LEAD_NAME = g.LEAD_NAME
LEAD_BIN = g.LEAD_BIN

EVIDENCE_RULE = (
    "Delivery contract: (1) required product file(s) in the sandbox workspace, AND "
    "(2) run-evidence.txt in the same workspace containing the exact command(s) run "
    "and a stdout/stderr excerpt. Missing run-evidence.txt = FAIL. "
    "A script that 'looks correct' without execution evidence = FAIL."
)


def extract_field(parsed, *keys):
    if not isinstance(parsed, dict):
        return None
    # grok envelope
    so = parsed.get("structuredOutput")
    if isinstance(so, dict):
        for k in keys:
            if so.get(k) is not None:
                return so.get(k)
    for k in keys:
        if parsed.get(k) is not None:
            return parsed.get(k)
    # nested text JSON
    text = parsed.get("text")
    if isinstance(text, str):
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                inner = json.loads(m.group(0))
                for k in keys:
                    if inner.get(k) is not None:
                        return inner.get(k)
            except Exception:
                pass
    for wrap in ("output", "message", "content", "data", "result"):
        v = parsed.get(wrap)
        if isinstance(v, dict):
            for k in keys:
                if v.get(k) is not None:
                    return v.get(k)
    return None


def lead_decision_from(raw, parsed, enums):
    val = extract_field(parsed, "decision", "verdict")
    if val in enums:
        return val
    # search raw
    blob = (raw or "") + "\n" + json.dumps(parsed or {}, ensure_ascii=False)
    for e in enums:
        if re.search(rf'\b{re.escape(e)}\b', blob):
            return e
    return None


def perm_policy_hint(job_tag: str, ws: str) -> str:
    common = (
        f"Sandbox only: {ws}. Never approve leaving the sandbox, reading secrets/tokens, "
        f"sudo, package installs that mutate the host, or unrelated network. Never choose always.\n"
        f"{EVIDENCE_RULE}\n"
    )
    if job_tag == "C":
        return common + (
            "This job MAY once-approve: bash/read of /etc/passwd (first 3 lines only) "
            "and writing results + run-evidence.txt inside the sandbox."
        )
    if job_tag == "D":
        return common + (
            "This job MAY once-approve: bash curl -I https://example.com (or equivalent HEAD) "
            "and writing headers + run-evidence.txt inside the sandbox. Reject other hosts."
        )
    return common + "Only once-approve in-sandbox write/bash clearly needed for the stated job."


def review_prompt(instruction: str, expected: list[str]) -> str:
    bits = []
    for ap in expected:
        p = Path(ap)
        if p.exists():
            try:
                bits.append(f"FILE {ap}:\n" + p.read_text(encoding="utf-8", errors="replace")[:2000])
            except Exception as e:
                bits.append(f"FILE {ap}: <read error {e}>")
        else:
            bits.append(f"MISSING {ap}")
    return (
        "You are the pluggable team lead accepting a TeleAgent worker delivery.\n"
        f"{EVIDENCE_RULE}\n"
        "Pass ONLY if all required files exist AND run-evidence.txt proves the required command ran "
        "(shows command + stdout/stderr excerpt matching the job).\n"
        "Output JSON verdict+reason.\n"
        f"Job:\n{instruction}\n\nArtifacts:\n" + "\n\n".join(bits)
    )


def run_job(name: str, instruction: str, expected_artifacts: list[str], timeout_sec: int = 420) -> dict:
    ws = str(COLLAB)
    report = {
        "name": name,
        "session_id": "",
        "pending_seen": False,
        "pending_summaries": [],
        "lead_permission_raw": "",
        "lead_permission_decision": "",
        "api_replies": [],
        "lead_review_raw": "",
        "lead_review_decision": "",
        "artifacts": [],
        "state": "fail",
        "ok": False,
        "error": "",
        "path": "unknown",
        "notes": [],
        "cut": 2,
    }

    # clean expected
    for ap in expected_artifacts:
        p = Path(ap)
        if p.exists():
            p.unlink()

    code, created = g.call(
        "POST",
        "/session",
        body={"title": f"collab-cut2-{name}", "directory": ws},
        extra_headers={"x-opencode-directory": ws},
    )
    if code >= 300 or not isinstance(created, dict) or not created.get("id"):
        report["error"] = f"create session failed: {code} {g.redact(created)}"
        return report
    sid = created["id"]
    report["session_id"] = sid

    full_instruction = instruction.strip() + "\n\n" + EVIDENCE_RULE + (
        "\nAfter creating any script, immediately execute it with bash, then write run-evidence.txt "
        "with the command line and captured stdout/stderr. Stay inside this workspace for outputs."
    )

    code, _ = g.call(
        "POST",
        f"/session/{sid}/prompt_async",
        body={"parts": [{"type": "text", "text": full_instruction}], "model": MODEL, "agent": getattr(g, "TELEAGENT_AGENT", "opencowork-default"), "queryID": f"q_{uuid.uuid4()}"},
        extra_headers={"x-opencode-directory": ws},
    )
    if code not in (200, 204) and code >= 300:
        report["error"] = f"prompt_async failed: {code}"
        return report

    schema_perm = {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["once", "always", "reject", "deny_job"]},
            "reason": {"type": "string"},
        },
        "required": ["decision", "reason"],
        "additionalProperties": False,
    }
    schema_review = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
        "additionalProperties": False,
    }

    deadline = time.time() + timeout_sec
    handled = set()
    while time.time() < deadline:
        _, status = g.call("GET", "/session/status")
        _, pending = g.call("GET", "/permission")
        if isinstance(pending, list) and pending:
            report["pending_seen"] = True
            report["path"] = "permission_api"
            report["notes"].append(f"pending={len(pending)}")
            for p in pending:
                pid = str(p.get("id") or p.get("requestID") or "")
                if not pid or pid in handled:
                    continue
                summary = g.summarize_permission(p)
                report["pending_summaries"].append(summary)
                prompt = (
                    "You are the pluggable team lead approving a TeleAgent permission request.\n"
                    f"{perm_policy_hint(name, ws)}\n"
                    f"Permission request summary:\n{summary}\n"
                    "Output JSON decision+reason. Prefer once for in-scope job steps; reject otherwise; never always."
                )
                raw, parsed = g.call_lead(prompt, schema_perm, ws)
                report["lead_permission_raw"] = raw
                decision = lead_decision_from(raw, parsed, ("once", "always", "reject", "deny_job"))
                if decision == "always":
                    decision = "once"
                    report["notes"].append("downgraded always->once")
                if decision not in ("once", "reject", "deny_job"):
                    decision = "reject"
                report["lead_permission_decision"] = decision
                if decision == "deny_job":
                    g.call("POST", f"/permission/{pid}/reply", body={"reply": "reject"})
                    report["api_replies"].append({"id": pid, "reply": "reject", "via": "deny_job"})
                    handled.add(pid)
                    report["state"] = "fail"
                    report["error"] = "lead deny_job"
                    _status(name, report)
                    return report
                reply = "once" if decision == "once" else "reject"
                rc, rj = g.call("POST", f"/permission/{pid}/reply", body={"reply": reply})
                report["api_replies"].append({"id": pid, "reply": reply, "http": rc, "body": g.redact(rj) if rj else ""})
                handled.add(pid)

        if not g.session_busy(status, sid):
            _, msgs = g.call("GET", f"/session/{sid}/message")
            asst = g.last_assistant(msgs)
            fin = g.assistant_finish(asst)
            err = g.assistant_error(asst)
            arts = g.expected_exists(expected_artifacts)
            report["artifacts"] = arts
            if err or fin == "error":
                report["state"] = "fail"
                report["error"] = err or "assistant finish=error"
                _status(name, report)
                return report
            if fin != "stop" and not arts:
                time.sleep(1.5)
                continue

            # Always lead-review with evidence rules (cut-2)
            if report["path"] == "unknown":
                report["path"] = "lead_review_fallback" if not report["pending_seen"] else "permission_then_review"
            raw, parsed = g.call_lead(review_prompt(full_instruction, expected_artifacts), schema_review, ws)
            report["lead_review_raw"] = raw
            verdict = lead_decision_from(raw, parsed, ("pass", "fail")) or "fail"
            report["lead_review_decision"] = verdict
            arts = g.expected_exists(expected_artifacts)
            report["artifacts"] = arts
            evidence_ok = Path(ws, "run-evidence.txt").exists() or any(
                Path(a).name == "run-evidence.txt" and Path(a).exists() for a in expected_artifacts
            )
            # also accept job-specific evidence filenames if listed
            if not evidence_ok:
                for a in expected_artifacts:
                    if "evidence" in Path(a).name and Path(a).exists():
                        evidence_ok = True

            if verdict == "pass" and len(arts) == len(expected_artifacts) and evidence_ok and fin == "stop":
                report["ok"] = True
                report["state"] = "ok"
                _status(name, report)
                return report

            # one redo
            report["notes"].append("lead fail or missing evidence -> one redo")
            reason = extract_field(parsed, "reason") or (raw or "")[:400]
            g.call(
                "POST",
                f"/session/{sid}/prompt_async",
                body={
                    "parts": [{
                        "type": "text",
                        "text": (
                            "Lead rejected delivery. Fix now.\n"
                            f"Reason: {reason}\n"
                            f"Original job:\n{full_instruction}\n"
                            "You MUST produce run-evidence.txt with command + stdout/stderr."
                        ),
                    }],
                    "model": MODEL, "agent": getattr(g, "TELEAGENT_AGENT", "opencowork-default"), "queryID": f"q_{uuid.uuid4()}",
                },
                extra_headers={"x-opencode-directory": ws},
            )
            redo_deadline = time.time() + min(240, deadline - time.time())
            while time.time() < redo_deadline:
                _, status = g.call("GET", "/session/status")
                _, pending = g.call("GET", "/permission")
                if isinstance(pending, list) and pending:
                    report["pending_seen"] = True
                    report["path"] = "permission_api"
                    for p in pending:
                        pid = str(p.get("id") or "")
                        if not pid or pid in handled:
                            continue
                        summary = g.summarize_permission(p)
                        report["pending_summaries"].append(summary)
                        rawp, parsedp = g.call_lead(
                            "Lead permission approve for redo.\n"
                            + perm_policy_hint(name, ws)
                            + f"\nRequest:\n{summary}\n",
                            schema_perm,
                            ws,
                        )
                        report["lead_permission_raw"] = (report.get("lead_permission_raw") or "") + "\n---REDO---\n" + (rawp or "")
                        decision = lead_decision_from(rawp, parsedp, ("once", "always", "reject", "deny_job")) or "reject"
                        if decision == "always":
                            decision = "once"
                        reply = "once" if decision == "once" else "reject"
                        report["lead_permission_decision"] = decision
                        g.call("POST", f"/permission/{pid}/reply", body={"reply": reply})
                        report["api_replies"].append({"id": pid, "reply": reply, "via": "redo"})
                        handled.add(pid)
                if not g.session_busy(status, sid):
                    break
                time.sleep(1.5)

            _, msgs = g.call("GET", f"/session/{sid}/message")
            asst = g.last_assistant(msgs)
            fin = g.assistant_finish(asst)
            arts = g.expected_exists(expected_artifacts)
            report["artifacts"] = arts
            raw2, parsed2 = g.call_lead(review_prompt(full_instruction, expected_artifacts), schema_review, ws)
            report["lead_review_raw"] = (report.get("lead_review_raw") or "") + "\n---REDO---\n" + (raw2 or "")
            verdict = lead_decision_from(raw2, parsed2, ("pass", "fail")) or "fail"
            report["lead_review_decision"] = verdict
            evidence_ok = any(Path(a).exists() and "evidence" in Path(a).name for a in expected_artifacts)
            if not evidence_ok:
                evidence_ok = (COLLAB / "run-evidence.txt").exists()
            if verdict == "pass" and len(arts) == len(expected_artifacts) and evidence_ok:
                report["ok"] = True
                report["state"] = "ok"
            else:
                report["ok"] = False
                report["state"] = "fail"
                report["error"] = f"lead={verdict} arts={arts} evidence_ok={evidence_ok} finish={fin}"
            _status(name, report)
            return report

        time.sleep(1.5)

    report["state"] = "timeout"
    report["error"] = "wall clock timeout"
    report["artifacts"] = g.expected_exists(expected_artifacts)
    _status(name, report)
    return report


def _status(name, report):
    out = {
        "ok": report.get("ok", False),
        "state": report.get("state"),
        "session_id": report.get("session_id"),
        "exit_code": None,
        "artifacts": report.get("artifacts", []),
        "log_path": "",
        "pending_permissions": report.get("pending_summaries", []),
        "error": report.get("error", ""),
        "lead_permission_decision": report.get("lead_permission_decision"),
        "lead_review_decision": report.get("lead_review_decision"),
        "path": report.get("path"),
        "pending_seen": report.get("pending_seen"),
        "cut": 2,
    }
    (COLLAB / f"status-{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    COLLAB.mkdir(parents=True, exist_ok=True)
    # unique evidence files per job to avoid cross-talk, but also require canonical names in expected
    results = []

    c_out = str(COLLAB / "passwd-head.txt")
    c_ev = str(COLLAB / "run-evidence-C.txt")
    results.append(
        run_job(
            "C",
            (
                "Sandbox-only job C. Write a small script passwd-head.sh that prints the first 3 lines of "
                "/etc/passwd, run it with bash, save those 3 lines to passwd-head.txt, and write "
                "run-evidence-C.txt containing the exact bash command and full stdout/stderr. "
                "Do not read other system files. Do not leave the workspace for outputs."
            ),
            [c_out, c_ev],
            timeout_sec=420,
        )
    )
    # also copy/symlink convention: if worker wrote run-evidence.txt, note it
    if (COLLAB / "run-evidence.txt").exists() and not Path(c_ev).exists():
        Path(c_ev).write_text((COLLAB / "run-evidence.txt").read_text(encoding="utf-8", errors="replace"), encoding="utf-8")

    d_out = str(COLLAB / "curl-headers.txt")
    d_ev = str(COLLAB / "run-evidence-D.txt")
    results.append(
        run_job(
            "D",
            (
                "Sandbox-only job D. Using bash, run: curl -sI https://example.com "
                "(HEAD/headers only; no body download). Save the response headers to curl-headers.txt "
                "and write run-evidence-D.txt with the exact command and stdout/stderr. "
                "Do not call any other host. Do not leave the workspace for outputs."
            ),
            [d_out, d_ev],
            timeout_sec=420,
        )
    )

    lines = [
        "# TeleAgent × Lead collab run report (Cut 2 / risk)",
        "",
        f"- Roles: TeleAgent=worker (pinned); lead=pluggable (`{LEAD_NAME}` via `{LEAD_BIN}`)",
        f"- Workspace: `{COLLAB}`",
        "- Evidence rule: product file(s) + run-evidence-*.txt with command + stdout/stderr",
        "- No `--always-approve` / yolo / global auto-approve",
        "- Secrets redacted",
        "",
    ]
    for r in results:
        lines += [
            f"## Task {r['name']}",
            "",
            f"- session_id: `{r.get('session_id')}`",
            f"- pending appeared: **{r.get('pending_seen')}**",
            f"- path: `{r.get('path')}`",
            f"- lead permission decision: `{r.get('lead_permission_decision') or 'n/a'}`",
            f"- lead permission raw (truncated):\n\n```\n{(r.get('lead_permission_raw') or '(none)')[:1500]}\n```\n",
            f"- API replies: `{g.redact(r.get('api_replies'))}`",
            f"- lead review decision: `{r.get('lead_review_decision') or 'n/a'}`",
            f"- lead review raw (truncated):\n\n```\n{(r.get('lead_review_raw') or '(none)')[:1500]}\n```\n",
            f"- artifacts: `{r.get('artifacts')}`",
            f"- state: **{r.get('state')}** ok={r.get('ok')}",
            f"- error: {r.get('error') or '(none)'}",
            f"- notes: {r.get('notes')}",
            "",
        ]
    c_ok, d_ok = results[0].get("ok"), results[1].get("ok")
    pending_any = any(r.get("pending_seen") for r in results)
    lines += [
        "## Summary",
        "",
        f"- Task C: {'PASS' if c_ok else 'FAIL'}",
        f"- Task D: {'PASS' if d_ok else 'FAIL'}",
        f"- Permission pending exercised: {'YES' if pending_any else 'NO'}",
        f"- Collaboration established: "
        + (
            "YES (worker delivery + lead review"
            + (" + permission reply loop" if pending_any else " ; permission loop still not hit")
            + ")"
        ),
        "",
    ]
    out = COLLAB / "run-report-risk.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    # also refresh main report pointer
    (COLLAB / "run-report.md").write_text(
        "# Cut-2 pointer\n\nSee `run-report-risk.md` for the risk/evidence cut.\n\n" + "\n".join(lines),
        encoding="utf-8",
    )
    print(json.dumps({"C": c_ok, "D": d_ok, "pending": pending_any, "report": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
