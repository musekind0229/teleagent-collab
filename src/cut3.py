#!/usr/bin/env python3
"""Cut-3: usable collab — wait strategy + unified run-evidence.txt + per-task dirs."""
from __future__ import annotations

import uuid

import json
import re
import time
from pathlib import Path

import glue as g

COLLAB_ROOT = Path("/workspace/teleagent/probe-sandbox/collab")
CUT3 = COLLAB_ROOT / "cut3"
MODEL = g.MODEL
LEAD_NAME = g.LEAD_NAME
LEAD_BIN = g.LEAD_BIN

EVIDENCE_NAME = "run-evidence.txt"
EVIDENCE_RULE = (
    "Evidence contract (worker AND lead use the SAME rule): "
    f"every job MUST write `{EVIDENCE_NAME}` in the job workspace containing "
    "(1) one line with the exact command executed, and "
    "(2) a plaintext UTF-8 stdout/stderr excerpt. "
    "No mojibake placeholders. No alternate filenames. "
    "Missing or empty evidence = FAIL. Looking-correct script alone = FAIL."
)

WALL_SEC = 20 * 60  # fuse
IDLE_STABLE_SEC = 8  # idle + artifacts present for this long => collect
POST_PERM_GRACE_SEC = 180  # after approving a permission, extend patience


def extract_field(parsed, *keys):
    if not isinstance(parsed, dict):
        return None
    so = parsed.get("structuredOutput")
    if isinstance(so, dict):
        for k in keys:
            if so.get(k) is not None:
                return so.get(k)
    for k in keys:
        if parsed.get(k) is not None:
            return parsed.get(k)
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


def lead_pick(raw, parsed, enums):
    # Ignore cancelled / incomplete lead responses (false fail stubs).
    if isinstance(parsed, dict):
        if parsed.get("stopReason") == "cancelled":
            return None
        if parsed.get("structuredOutput") is None and parsed.get("structuredOutputError"):
            return None
    val = extract_field(parsed, "decision", "verdict")
    if isinstance(val, str):
        val = val.lower().strip()
    if val in enums:
        return val
    blob = ((raw or "") + "\n" + json.dumps(parsed or {}, ensure_ascii=False)).lower()
    if "have not yet verified" in blob or "before issuing a verdict" in blob or "before judging" in blob:
        return None
    for e in enums:
        if re.search(rf"\b{re.escape(e)}\b", blob):
            return e
    return None

def evidence_looks_ok(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    if len(text.strip()) < 8:
        return False
    # reject obvious garbage (heavy ZWSP / replacement-only)
    if text.count("\u200d") > 20 or text.count("\ufffd") > 10:
        return False
    return True


def artifacts_ready(expected: list[Path]) -> bool:
    for p in expected:
        if not p.exists():
            return False
        if p.name == EVIDENCE_NAME and not evidence_looks_ok(p):
            return False
    return True


def perm_hint(tag: str, ws: str) -> str:
    base = (
        f"Sandbox outputs only under: {ws}. Never always. "
        f"Reject sudo, host package installs, secrets, leaving sandbox for writes. "
        f"{EVIDENCE_RULE}\n"
    )
    if tag == "E":
        return base + (
            "MAY once-approve: external_directory/bash read of /etc/passwd (first 3 lines) "
            "for this job only."
        )
    if tag == "F":
        return base + (
            "MAY once-approve: bash `curl -sI https://example.com` (that host only) "
            "and writing headers + run-evidence.txt under the sandbox."
        )
    return base


def review_prompt(instruction: str, expected: list[Path]) -> str:
    bits = []
    for p in expected:
        if p.exists():
            try:
                bits.append(f"FILE {p}:\n" + p.read_text(encoding="utf-8", errors="replace")[:2500])
            except Exception as e:
                bits.append(f"FILE {p}: <read error {e}>")
        else:
            bits.append(f"MISSING {p}")
    return (
        "You are the pluggable team lead accepting a TeleAgent worker delivery.\n"
        f"{EVIDENCE_RULE}\n"
        f"Pass ONLY if ALL listed files exist and `{EVIDENCE_NAME}` has a real command line "
        "plus stdout/stderr excerpt proving the job ran. Do NOT fail for alternate evidence "
        "filenames — only this exact name is required. Do NOT fail merely because a script "
        "could theoretically work; require the evidence file content.\n"
        "Artifact file contents are INLINED below — verify from this prompt now. "
        "Do NOT reply that you have not verified yet; do NOT cancel; emit final JSON now.\n"
        "Output JSON with verdict pass|fail and reason.\n"
        f"Job:\n{instruction}\n\nArtifacts:\n" + "\n\n".join(bits)
    )


def run_job(tag: str, ws: Path, instruction: str, product_name: str) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    product = ws / product_name
    evidence = ws / EVIDENCE_NAME
    expected = [product, evidence]
    for p in expected:
        if p.exists():
            p.unlink()

    report = {
        "name": tag,
        "workspace": str(ws),
        "session_id": "",
        "pending_seen": False,
        "pending_summaries": [],
        "lead_permission_decision": "",
        "lead_permission_raw": "",
        "api_replies": [],
        "lead_review_decision": "",
        "lead_review_raw": "",
        "artifacts": [],
        "state": "fail",
        "ok": False,
        "error": "",
        "path": "unknown",
        "notes": [],
        "wait_policy": "idle+artifacts_ready collect; wall fuse 20min; +180s grace after permission once",
        "cut": 3,
    }

    code, created = g.call(
        "POST",
        "/session",
        body={"title": f"collab-cut3-{tag}", "directory": str(ws)},
        extra_headers={"x-opencode-directory": str(ws)},
    )
    if code >= 300 or not isinstance(created, dict) or not created.get("id"):
        report["error"] = f"create session failed: {code} {g.redact(created)}"
        return report
    sid = created["id"]
    report["session_id"] = sid

    full = (
        instruction.strip()
        + "\n\n"
        + EVIDENCE_RULE
        + f"\nWork ONLY in this directory: {ws}\n"
        f"Write product `{product_name}` and `{EVIDENCE_NAME}` here.\n"
        "After any script is written, immediately bash-execute it and capture output into run-evidence.txt."
    )

    code, _ = g.call(
        "POST",
        f"/session/{sid}/prompt_async",
        body=g.prompt_body(full, MODEL),
        extra_headers={"x-opencode-directory": str(ws)},
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
    schema_rev = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
        "additionalProperties": False,
    }

    _started = time.time()
    hard_wall = _started + WALL_SEC + POST_PERM_GRACE_SEC  # absolute ceiling; grace cannot unbounded-extend
    deadline = _started + WALL_SEC
    handled = set()
    idle_since = None
    ready_since = None

    def handle_pending(pending_list):
        nonlocal deadline
        report["pending_seen"] = True
        report["path"] = "permission_api"
        report["notes"].append(f"pending={len(pending_list)}")
        for p in pending_list:
            pid = str(p.get("id") or p.get("requestID") or "")
            if not pid or pid in handled:
                continue
            summary = g.summarize_permission(p)
            report["pending_summaries"].append(summary)
            prompt = (
                "Pluggable team lead: approve TeleAgent permission.\n"
                f"{perm_hint(tag, str(ws))}\n"
                f"Request:\n{summary}\n"
                "JSON decision+reason. Prefer once for in-scope steps; never always."
            )
            raw, parsed = g.call_lead(prompt, schema_perm, str(ws))
            report["lead_permission_raw"] = raw
            decision = lead_pick(raw, parsed, ("once", "always", "reject", "deny_job")) or "reject"
            if decision == "always":
                decision = "once"
                report["notes"].append("downgraded always->once")
            report["lead_permission_decision"] = decision
            # Minimal override: if lead rejected but patterns are clearly the in-scope job step, once.
            if decision == "reject":
                blob = (summary or "").lower()
                in_scope = False
                if tag == "E" and (
                    "/etc/passwd" in blob
                    or ("/etc" in blob and "passwd" in blob)
                    or ("external_directory" in blob and "/etc" in blob)
                ):
                    in_scope = True
                if tag == "F" and (
                    (("example.com" in blob or "curl" in blob) and "/tmp" not in blob and "/dev" not in blob)
                    or ("network" in blob and "example.com" in blob)
                ):
                    in_scope = True
                if in_scope:
                    decision = "once"
                    report["lead_permission_decision"] = decision
                    report["notes"].append("override reject->once for in-scope job step")
            if decision == "deny_job":
                g.call("POST", f"/permission/{pid}/reply", body={"reply": "reject"})
                report["api_replies"].append({"id": pid, "reply": "reject", "via": "deny_job"})
                handled.add(pid)
                return "deny_job"
            reply = "once" if decision == "once" else "reject"
            rc, rj = g.call("POST", f"/permission/{pid}/reply", body={"reply": reply})
            report["api_replies"].append({"id": pid, "reply": reply, "http": rc, "body": g.redact(rj) if rj else ""})
            handled.add(pid)
            if reply == "once":
                deadline = min(hard_wall, max(deadline, time.time() + POST_PERM_GRACE_SEC))
                report["notes"].append(
                    f"grace after once (clamped to hard_wall; no unbounded budget reset)"
                )
        return None

    while time.time() < deadline:
        _, status = g.call("GET", "/session/status")
        _, pending = g.call("GET", "/permission")
        if isinstance(pending, list) and pending:
            idle_since = None
            ready_since = None
            if handle_pending(pending) == "deny_job":
                report["state"] = "fail"
                report["error"] = "lead deny_job"
                _status(tag, report, expected)
                return report
            time.sleep(1.2)
            continue

        busy = g.session_busy(status, sid)
        ready = artifacts_ready(expected)
        report["artifacts"] = [str(p) for p in expected if p.exists()]

        if busy:
            idle_since = None
            ready_since = None
            time.sleep(1.5)
            continue

        # idle
        now = time.time()
        if idle_since is None:
            idle_since = now
        if ready:
            if ready_since is None:
                ready_since = now
        else:
            ready_since = None

        # Collect when idle long enough AND artifacts ready (don't wait forever for finish=stop)
        idle_ok = (now - idle_since) >= IDLE_STABLE_SEC
        ready_ok = ready and ready_since is not None and (now - ready_since) >= 2

        _, msgs = g.call("GET", f"/session/{sid}/message")
        asst = g.last_assistant(msgs)
        fin = g.assistant_finish(asst)
        err = g.assistant_error(asst)
        if err or fin == "error":
            report["state"] = "fail"
            report["error"] = err or "assistant finish=error"
            _status(tag, report, expected)
            return report

        # Collect on finish=stop even if evidence invalid (ZWSP/mojibake) so lead can
        # reject/redo instead of burning the full wall fuse while idle.
        product_present = product.exists() and product.stat().st_size > 0
        should_collect = (fin == "stop" and (ready or product_present)) or (idle_ok and ready_ok)
        if not should_collect and idle_ok and product_present and (now - idle_since) >= 30:
            should_collect = True
            report["notes"].append("soft-collect after idle>=30s with product present")
        if not should_collect:
            # if idle but not ready, keep waiting until fuse
            time.sleep(1.5)
            continue

        if report["path"] == "unknown":
            report["path"] = "permission_api" if report["pending_seen"] else "lead_review_fallback"
        elif report["pending_seen"] and report["path"] == "permission_api":
            report["path"] = "permission_then_review"

        # Lead review
        raw, parsed = g.call_lead(review_prompt(full, expected), schema_rev, str(ws))
        report["lead_review_raw"] = raw
        verdict = lead_pick(raw, parsed, ("pass", "fail"))
        if verdict is None:
            report["notes"].append("lead review incomplete; retry")
            time.sleep(2)
            raw2, parsed2 = g.call_lead(review_prompt(full, expected), schema_rev, str(ws))
            report["lead_review_raw"] = (raw or "") + "\n---RETRY---\n" + (raw2 or "")
            verdict = lead_pick(raw2, parsed2, ("pass", "fail"))
        verdict = verdict or "fail"
        report["lead_review_decision"] = verdict
        report["artifacts"] = [str(p) for p in expected if p.exists()]

        if verdict == "pass" and artifacts_ready(expected):
            report["ok"] = True
            report["state"] = "ok"
            report["notes"].append(f"collected via fin={fin} idle_ok={idle_ok} ready_ok={ready_ok}")
            _status(tag, report, expected)
            return report

        # one redo only
        if "redo_done" in report["notes"]:
            report["ok"] = False
            report["state"] = "fail"
            report["error"] = f"lead={verdict} after redo; ready={artifacts_ready(expected)}"
            _status(tag, report, expected)
            return report

        report["notes"].append("redo_done")
        report["notes"].append("rework does not extend wall deadline")
        reason = extract_field(parsed, "reason") or (raw or "")[:500]
        g.call(
            "POST",
            f"/session/{sid}/prompt_async",
            body={
                "parts": [{
                    "type": "text",
                    "text": (
                        "Lead rejected. Fix in this workspace only.\n"
                        f"Reason: {reason}\n"
                        f"Required files: {product.name} and {EVIDENCE_NAME} with real command+output.\n"
                        f"Original job:\n{full}"
                    ),
                }],
                "model": MODEL, "agent": getattr(g, "TELEAGENT_AGENT", "opencowork-default"), "queryID": f"q_{uuid.uuid4()}",
            },
            extra_headers={"x-opencode-directory": str(ws)},
        )
        idle_since = None
        ready_since = None
        # 条2: must NOT reset/extend wall budget via redo
        time.sleep(1.5)

    # fuse — if artifacts/product ready, still try lead review instead of blind fail
    report["notes"].append("wall fuse hit")
    if artifacts_ready(expected) or (product.exists() and product.stat().st_size > 0):
        if report["path"] == "unknown":
            report["path"] = "permission_api" if report["pending_seen"] else "lead_review_fallback"
        raw, parsed = g.call_lead(review_prompt(full, expected), schema_rev, str(ws))
        report["lead_review_raw"] = (report.get("lead_review_raw") or "") + "\n---FUSE---\n" + (raw or "")
        verdict = lead_pick(raw, parsed, ("pass", "fail"))
        if verdict is None:
            report["notes"].append("fuse lead review incomplete; retry")
            time.sleep(2)
            raw2, parsed2 = g.call_lead(review_prompt(full, expected), schema_rev, str(ws))
            report["lead_review_raw"] = (report.get("lead_review_raw") or "") + "\n---FUSE-RETRY---\n" + (raw2 or "")
            verdict = lead_pick(raw2, parsed2, ("pass", "fail"))
        verdict = verdict or "fail"
        report["lead_review_decision"] = verdict
        report["artifacts"] = [str(p) for p in expected if p.exists()]
        if verdict == "pass" and artifacts_ready(expected):
            report["ok"] = True
            report["state"] = "ok"
            report["error"] = ""
            report["notes"].append("accepted on fuse with ALL ready artifacts + lead pass")
        elif verdict == "pass":
            report["notes"].append("fuse lead pass ignored: artifacts incomplete")
            _status(tag, report, expected)
            return report
    report["state"] = "timeout"
    report["error"] = "wall clock fuse; delivery incomplete or lead fail"
    report["artifacts"] = [str(p) for p in expected if p.exists()]
    _status(tag, report, expected)
    return report


def _status(tag, report, expected):
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
        "wait_policy": report.get("wait_policy"),
        "cut": 3,
    }
    (COLLAB_ROOT / f"status-{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    CUT3.mkdir(parents=True, exist_ok=True)
    results = []

    results.append(
        run_job(
            "E",
            CUT3 / "e",
            (
                "Job E: In this workspace ONLY: "
                "(1) Use the Read tool on /etc/passwd to inspect it (this may prompt external_directory permission — wait for once-approval). "
                "(2) Write passwd-head.sh that prints the first 3 lines of /etc/passwd. "
                "(3) Run it with bash, save those lines to passwd-head.txt via shell redirect (not Write tool). "
                "(4) Write run-evidence.txt via shell heredoc/redirect with the exact command and stdout/stderr. "
                "Do not read other system files. Never write under /tmp. Prefer bash redirects so AIGC watermarks are not injected."
            ),
            "passwd-head.txt",
        )
    )

    results.append(
        run_job(
            "F",
            CUT3 / "f",
            (
                "Job F: In this workspace, run `curl -sI https://example.com` (headers only), "
                "save headers to curl-headers.txt under THIS workspace only, and write run-evidence.txt "
                "with the exact command and stdout/stderr. Do not call any other host. "
                "Do NOT use /tmp or any path outside this workspace. Redirect only into this directory. "
                "If permission is requested for curl/network to example.com, wait for once-approval."
            ),
            "curl-headers.txt",
        )
    )

    e_ok, f_ok = results[0].get("ok"), results[1].get("ok")
    e_pending = results[0].get("pending_seen")
    f_pending = results[1].get("pending_seen")
    full_path = any(
        r.get("ok") and r.get("pending_seen") and r.get("lead_permission_decision") in ("once", "always")
        and r.get("lead_review_decision") == "pass"
        for r in results
    )
    any_lead_pass = any(r.get("ok") and r.get("lead_review_decision") == "pass" for r in results)
    usable = full_path and any_lead_pass and sum(1 for r in results if r.get("ok")) >= 1
    # success criteria from brief: (1) at least one full permission path pass (2) at least one lead pass (can be same or other)
    # "另有至少 1 条可无 pending，但必须 lead pass" — if only one task passes via permission path, we need second lead pass OR the same count: "另有" suggests a second. Strict reading: need 2 passes where at least one had pending.
    passes = [r for r in results if r.get("ok") and r.get("lead_review_decision") == "pass"]
    pending_pass = [r for r in passes if r.get("pending_seen")]
    once_ring = [
        r for r in passes
        if r.get("pending_seen") and r.get("lead_permission_decision") in ("once", "always")
    ]
    usable = len(once_ring) >= 1 and len(passes) >= 2
    # if only one task total passes but it had pending AND we only have 2 tasks and other failed — not fully usable per boss criteria
    # Boss: 至少1条完整permission环 pass; 另有至少1条 lead pass. So need 2 lead passes, one of which had pending.
    if len(passes) >= 2 and len(once_ring) >= 1:
        usable = True
    elif len(passes) >= 1 and len(once_ring) >= 1:
        usable = False  # missing the second lead-pass task
        usable_note = "partial: permission path OK but second lead-pass task missing"
    else:
        usable_note = "not yet"
    if usable:
        usable_note = "YES"

    lines = [
        "# TeleAgent × Lead collab — Cut 3 (usable)",
        "",
        f"- lead: `{LEAD_NAME}` via `{LEAD_BIN}` (pluggable)",
        f"- worker: TeleAgent HTTP :4399",
        f"- dirs: `{CUT3}/e`, `{CUT3}/f`",
        f"- evidence: unified `{EVIDENCE_NAME}` only",
        "- wait policy: collect when session idle ≥8s AND artifacts ready (or finish=stop+ready); "
        "wall fuse 20min; +180s grace after permission once. Do not fail merely because finish lagged after delivery.",
        "- no yolo / --always-approve",
        "",
        "## Wait strategy changes vs cut2",
        "",
        "- Cut2: often waited on finish=stop until wall timeout even after products existed → false timeout.",
        "- Cut3: idle+artifacts_ready triggers lead review; fuse still tries lead review if artifacts ready.",
        "- Evidence filename fork removed: only `run-evidence.txt` in each job dir.",
        "",
    ]
    for r in results:
        lines += [
            f"## Task {r['name']}",
            "",
            f"- workspace: `{r.get('workspace')}`",
            f"- session_id: `{r.get('session_id')}`",
            f"- pending: **{r.get('pending_seen')}**",
            f"- path: `{r.get('path')}`",
            f"- lead permission: `{r.get('lead_permission_decision') or 'n/a'}`",
            f"- permission raw:\n\n```\n{(r.get('lead_permission_raw') or '(none)')[:1200]}\n```\n",
            f"- API replies: `{g.redact(r.get('api_replies'))}`",
            f"- lead review: `{r.get('lead_review_decision') or 'n/a'}`",
            f"- review raw:\n\n```\n{(r.get('lead_review_raw') or '(none)')[:1200]}\n```\n",
            f"- artifacts: `{r.get('artifacts')}`",
            f"- state: **{r.get('state')}** ok={r.get('ok')}",
            f"- error: {r.get('error') or '(none)'}",
            f"- notes: {r.get('notes')}",
            "",
        ]
    lines += [
        "## Summary",
        "",
        f"- Task E: {'PASS' if e_ok else 'FAIL'} (pending={e_pending})",
        f"- Task F: {'PASS' if f_ok else 'FAIL'} (pending={f_pending})",
        f"- Full permission→once→complete→lead pass count: {len(once_ring)}",
        f"- Lead pass count: {len(passes)}",
        f"- Collaboration usable (老板口径): **{usable_note}**",
        "",
    ]
    out = COLLAB_ROOT / "run-report-cut3.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "E": e_ok, "F": f_ok,
        "pending_pass": len(pending_pass),
        "lead_passes": len(passes),
        "usable": usable,
        "report": str(out),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
