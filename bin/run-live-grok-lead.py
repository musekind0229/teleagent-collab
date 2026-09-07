#!/usr/bin/env python3
"""P3 live path: create → permission → GrokCli LeadAdapter → continue → acceptance.

Honest failures: login/quota/TeleAgent/auth gaps are written clearly — never fake PASS.

Usage:
  cd /workspace/teleagent-collab
  python3 bin/run-live-grok-lead.py
  python3 bin/run-live-grok-lead.py --timeout 180 --workdir /tmp/p3-live
Env:
  COLLAB_LEAD_BIN=/workspace/run-grok.sh   (default)
  TELEAGENT_MODEL_ID / TELEAGENT_PROVIDER_ID
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))


def _fail(report: dict, reason: str, **extra) -> int:
    report["ok"] = False
    report["status"] = "failed"
    report["reason"] = reason
    report.update(extra)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Live Grok lead loop against TeleAgent :4399")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--workdir", type=str, default="")
    ap.add_argument("--lead-bin", type=str, default=os.environ.get("COLLAB_LEAD_BIN", "/workspace/run-grok.sh"))
    ap.add_argument("--base-url", type=str, default="http://127.0.0.1:4399")
    args = ap.parse_args()

    report: dict = {
        "ok": False,
        "status": "starting",
        "simulated": False,
        "lead": "grok_cli",
        "lead_bin": args.lead_bin,
        "base_url": args.base_url,
        "steps": [],
        "reason": "",
    }

    # --- resolve workdir ---
    if args.workdir:
        ws = Path(args.workdir)
    else:
        ws = REPO / "jobs" / "workspaces" / f"p3-live-grok-{int(time.time())}"
    ws.mkdir(parents=True, exist_ok=True)
    report["workdir"] = str(ws)
    artifact = ws / "grok-live-ok.txt"

    try:
        from teleagent_adapter import get_adapter
        from teleagent_adapter.doctor import doctor
        from lead_adapter import (
            GrokCliLeadAdapter,
            build_lead_request,
            lead_permission_response_schema,
            lead_review_response_schema,
            validate_lead_decision,
            LeadDecisionError,
        )
        from lead_adapter.schema import unwrap_structured
        from question_api import probe_question_api, list_questions_for_session, handle_question_need_human
        from task_auth import authorize_action
    except Exception as e:
        return _fail(report, f"import error: {e}", traceback=traceback.format_exc())

    # --- doctor ---
    try:
        ad = get_adapter(base_url=args.base_url)
        ad.refresh_creds()
        rep = doctor(adapter=ad, base_url=args.base_url)
        report["doctor"] = rep.to_dict()
        report["steps"].append({"step": "doctor", "status": rep.status})
        if rep.status != "ok":
            return _fail(report, f"doctor not ok: {rep.status}: {rep.details}")
    except Exception as e:
        return _fail(report, f"doctor/creds failed: {type(e).__name__}: {e}")

    # --- question API probe (gap-aware) ---
    try:
        qp = probe_question_api(ad)
        report["question_api"] = qp.to_dict()
        report["steps"].append({"step": "question_probe", "available": qp.available})
    except Exception as e:
        report["question_api"] = {"available": False, "error": str(e)}

    # --- lead binary exists ---
    lead_bin = Path(args.lead_bin)
    if not lead_bin.exists():
        alt = Path.home() / ".grok" / "bin" / "grok"
        if alt.exists():
            lead_bin = alt
            report["lead_bin"] = str(lead_bin)
        else:
            return _fail(report, f"lead binary missing: {args.lead_bin}")

    lead = GrokCliLeadAdapter(bin_path=str(lead_bin))
    report["steps"].append({"step": "lead_adapter", "name": lead.name, "bin": str(lead_bin)})

    # --- create session ---
    try:
        code, created = ad.create_session(title="p3-live-grok-lead", directory=str(ws))
        report["steps"].append({"step": "create_session", "http": code})
        if code >= 300 or not isinstance(created, dict) or not created.get("id"):
            return _fail(report, f"create_session failed http={code}", body=created)
        sid = str(created["id"])
        report["session_id"] = sid
    except Exception as e:
        return _fail(report, f"create_session error: {e}")

    charter = {
        "name": "p3-live-grok",
        "task_kind": "file_task",
        "goal": f"Create {artifact.name} with one line GROK_LIVE_OK under workspace only",
        "must": ["stay in workspace", "write grok-live-ok.txt"],
        "must_not": ["secrets", "always-approve", "install software"],
        "allow_secret_globs": [],
        "allow_paths": [],
        "allow_keys": [],
        "done_when": {"artifacts": [artifact.name]},
        "acceptance": f"{artifact.name} exists with GROK_LIVE_OK",
        "force_lead_review": True,
        "timeout_sec": int(args.timeout),
    }

    instruction = (
        f"In directory {ws} only, create {artifact.name} containing exactly one line: GROK_LIVE_OK\n"
        "Then stop. Do not touch secrets, ~/.ssh, or install any packages."
    )
    try:
        code, _ = ad.prompt(sid, instruction, directory=str(ws))
        report["steps"].append({"step": "prompt", "http": code})
        if code not in (200, 204) and code >= 300:
            return _fail(report, f"prompt_async failed http={code}")
    except Exception as e:
        return _fail(report, f"prompt error: {e}")

    # --- poll: permissions via Grok lead; questions → need_human ---
    deadline = time.time() + float(args.timeout)
    approved = 0
    rejected = 0
    lead_calls = 0
    need_human_questions: list = []
    lead_errors: list = []

    while time.time() < deadline:
        # permissions
        try:
            _, pending = ad.list_permissions(session_id=sid)
        except Exception as e:
            lead_errors.append(f"list_permissions: {e}")
            pending = []
        for p in pending or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "")
            if not pid:
                continue
            auth_d = authorize_action(
                charter=charter,
                path=str(p.get("path") or "") or None,
                permission=p,
                workspace=ws,
            )
            if auth_d.needs_user or not auth_d.allowed:
                ad.reply_permission(pid, "reject")
                rejected += 1
                report["steps"].append({"step": "perm_reject_auth", "id": pid, "reason": auth_d.reason})
                continue
            req = build_lead_request(
                kind="permission",
                goal=charter["goal"],
                authorized_scope=charter["must"],
                prohibitions=charter["must_not"],
                acceptance_criteria=charter["done_when"],
                current_application=p,
                charter=charter,
            )
            lead_calls += 1
            try:
                raw, parsed = lead.decide(
                    req,
                    schema=lead_permission_response_schema(),
                    cwd=str(ws),
                    timeout_sec=min(120.0, args.timeout),
                )
                validated = validate_lead_decision(raw, parsed, request=req, kind="permission")
                decision = validated["decision"]
            except LeadDecisionError as e:
                lead_errors.append(f"lead {pid}: {e.code}: {e}")
                # keep pending? for live script we reject to avoid hang, record honestly
                ad.reply_permission(pid, "reject")
                rejected += 1
                report["steps"].append({"step": "lead_invalid", "id": pid, "error": str(e)})
                continue
            except Exception as e:
                lead_errors.append(f"lead call {pid}: {e}")
                return _fail(
                    report,
                    f"Grok lead call failed (login/quota/CLI?): {e}",
                    lead_errors=lead_errors,
                    approved=approved,
                )
            reply = "once" if decision == "once" else "reject"
            http, _ = ad.reply_permission(pid, reply)
            report["steps"].append(
                {
                    "step": "lead_perm",
                    "id": pid,
                    "decision": decision,
                    "reply": reply,
                    "http": http,
                    "reason": validated.get("reason"),
                }
            )
            if reply == "once" and http < 300:
                approved += 1
            else:
                rejected += 1

        # questions — session bound; default need_human
        try:
            _, qs = list_questions_for_session(ad, sid)
        except Exception:
            qs = []
        for q in qs or []:
            decision = handle_question_need_human(q, session_id=sid)
            if decision["action"] == "need_human":
                need_human_questions.append(decision)
                report["steps"].append({"step": "question_need_human", "id": decision.get("question_id")})
            elif decision["action"] == "skip":
                continue

        if artifact.exists():
            break
        # idle check
        try:
            _, status = ad.session_status(session_id=sid)
            busy = False
            if isinstance(status, dict):
                st = status.get(sid) or status
                if isinstance(st, dict):
                    busy = str(st.get("type") or st.get("status") or "").lower() in (
                        "busy",
                        "running",
                        "pending",
                        "retry",
                    )
            if not busy and artifact.exists():
                break
        except Exception:
            pass
        time.sleep(2)

    report["approved"] = approved
    report["rejected"] = rejected
    report["lead_calls"] = lead_calls
    report["lead_errors"] = lead_errors
    report["need_human_questions"] = need_human_questions
    report["artifact_exists"] = artifact.exists()

    if need_human_questions and not artifact.exists():
        return _fail(
            report,
            "blocked on Question API need_human (no auto answers)",
            status="blocked",
        )

    if not artifact.exists():
        return _fail(
            report,
            f"incomplete: artifact missing after {args.timeout}s "
            f"(approved={approved}, lead_calls={lead_calls}, errors={lead_errors[:3]})",
            status="incomplete",
        )

    # --- acceptance / force_lead_review via Grok ---
    req = build_lead_request(
        kind="review",
        goal=charter["goal"],
        authorized_scope=charter["must"],
        prohibitions=charter["must_not"],
        acceptance_criteria=charter["acceptance"],
        current_application={
            "artifacts": [artifact.name],
            "content_preview": artifact.read_text(encoding="utf-8")[:200],
        },
        charter=charter,
    )
    try:
        raw, parsed = lead.decide(
            req,
            schema=lead_review_response_schema(),
            cwd=str(ws),
            timeout_sec=min(120.0, args.timeout),
        )
        review = validate_lead_decision(raw, parsed, request=req, kind="review")
        report["steps"].append({"step": "lead_review", "verdict": review.get("verdict"), "reason": review.get("reason")})
        report["review"] = review
        if review.get("verdict") != "pass":
            return _fail(report, f"lead review verdict={review.get('verdict')}: {review.get('reason')}")
    except LeadDecisionError as e:
        return _fail(report, f"lead review invalid: {e.code}: {e}")
    except Exception as e:
        return _fail(report, f"lead review call failed: {e}")

    body = artifact.read_text(encoding="utf-8").strip()
    if "GROK_LIVE_OK" not in body:
        return _fail(report, f"artifact content unexpected: {body!r}")

    report["ok"] = True
    report["status"] = "passed"
    report["reason"] = "create→permission→Grok lead→continue→acceptance OK"
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
