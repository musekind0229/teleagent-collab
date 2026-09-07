"""Question API orchestration (P3): list / reply / reject with session binding.

TeleAgent SAC exposes:
  GET  /question
  POST /question/:id/reply   body {"answers": [][]string}
  POST /question/:id/reject

When the route is present we bind strictly by sessionID (same as permissions).
When absent / incompatible, doctor reports the gap — never invent answers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from teleagent_adapter.base import AdapterStatus, filter_by_session, session_id_of


@dataclass
class QuestionProbe:
    available: bool
    status: str
    details: list[str] = field(default_factory=list)
    sample: Any = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "status": self.status,
            "details": list(self.details),
            "sample": self.sample,
        }


def probe_question_api(adapter: Any) -> QuestionProbe:
    """Probe GET /question (and reply shape docs). Honest gap reporting."""
    details: list[str] = []
    try:
        code, body = adapter.call("GET", "/question", timeout=15)
    except Exception as e:
        return QuestionProbe(
            available=False,
            status=AdapterStatus.API_INCOMPATIBLE.value,
            details=[f"GET /question raised: {e}"],
        )
    if code in (401, 403):
        return QuestionProbe(False, AdapterStatus.AUTH_FAILED.value, [f"HTTP {code}"])
    if code == 404:
        return QuestionProbe(
            False,
            AdapterStatus.API_INCOMPATIBLE.value,
            ["GET /question → 404 (route missing)"],
        )
    if code >= 500:
        return QuestionProbe(
            False,
            AdapterStatus.API_INCOMPATIBLE.value,
            [f"GET /question → HTTP {code}"],
        )
    if code >= 400:
        return QuestionProbe(
            False,
            AdapterStatus.API_INCOMPATIBLE.value,
            [f"GET /question → HTTP {code} body={body!r}"],
        )
    if not isinstance(body, list):
        details.append(f"unexpected body type {type(body).__name__} (want list)")
        return QuestionProbe(False, AdapterStatus.API_INCOMPATIBLE.value, details, sample=body)
    details.append(f"GET /question ok; pending_count={len(body)}")
    details.append('reply body schema: {"answers": [[string, ...], ...]} ([][]string)')
    details.append("reject: POST /question/:id/reject")
    return QuestionProbe(True, AdapterStatus.OK.value, details, sample=body[:3] if body else [])


def list_questions_for_session(adapter: Any, session_id: str | None) -> tuple[int, list]:
    """Prefer adapter.list_questions; fall back to call+filter."""
    if hasattr(adapter, "list_questions"):
        return adapter.list_questions(session_id=session_id)
    code, qs = adapter.call("GET", "/question")
    items = qs if isinstance(qs, list) else []
    return code, filter_by_session(items, session_id)


def reply_question(
    adapter: Any,
    request_id: str,
    answers: list,
    *,
    session_id: str | None = None,
    pending: dict | None = None,
) -> tuple[int, Any]:
    """Reply with session binding check when pending/session_id provided."""
    if session_id and pending is not None:
        sid = session_id_of(pending)
        if sid and sid != session_id:
            raise ValueError(
                f"question session mismatch: pending={sid!r} expected={session_id!r}"
            )
    if hasattr(adapter, "reply_question"):
        return adapter.reply_question(request_id, answers)
    # normalize [][]string
    norm = []
    for a in answers or []:
        if isinstance(a, (list, tuple)):
            norm.append([str(x) for x in a])
        else:
            norm.append([str(a)])
    return adapter.call("POST", f"/question/{request_id}/reply", body={"answers": norm})


def reject_question(
    adapter: Any,
    request_id: str,
    *,
    session_id: str | None = None,
    pending: dict | None = None,
) -> tuple[int, Any]:
    if session_id and pending is not None:
        sid = session_id_of(pending)
        if sid and sid != session_id:
            raise ValueError(
                f"question session mismatch: pending={sid!r} expected={session_id!r}"
            )
    if hasattr(adapter, "reject_question"):
        return adapter.reject_question(request_id)
    return adapter.call("POST", f"/question/{request_id}/reject", body={})


def handle_question_need_human(
    question: dict,
    *,
    session_id: str,
    auto_answers: list | None = None,
    lead_decide: Callable[[dict], dict | None] | None = None,
) -> dict:
    """Decide what to do with a pending question.

    Default: need_human (do not invent answers). Optional auto_answers / lead_decide
    may supply answers only when explicitly provided by caller.
    """
    qid = str(question.get("id") or question.get("requestID") or "")
    sid = session_id_of(question)
    if sid and sid != session_id:
        return {
            "action": "skip",
            "reason": "foreign_session",
            "question_id": qid,
            "session_id": sid,
        }
    if auto_answers is not None:
        return {
            "action": "reply",
            "question_id": qid,
            "answers": auto_answers,
            "via": "auto_answers",
        }
    if lead_decide is not None:
        decided = lead_decide(question)
        if isinstance(decided, dict) and decided.get("answers") is not None:
            return {
                "action": "reply",
                "question_id": qid,
                "answers": decided["answers"],
                "via": "lead",
                "lead": decided,
            }
        if isinstance(decided, dict) and decided.get("reject"):
            return {"action": "reject", "question_id": qid, "via": "lead", "lead": decided}
    return {
        "action": "need_human",
        "question_id": qid,
        "session_id": session_id,
        "reason": "no auto/lead answers; surface to human (Question API)",
        "question": {
            k: question.get(k)
            for k in ("id", "sessionID", "questions", "question", "header", "options")
            if k in question
        },
    }


__all__ = [
    "QuestionProbe",
    "probe_question_api",
    "list_questions_for_session",
    "reply_question",
    "reject_question",
    "handle_question_need_human",
]
