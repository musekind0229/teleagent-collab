"""Public Run observation — hide TeleAgent status/message shapes from scheduler/glue.

Missing or unusable fields must NOT invent idle / successful finish (缺字段不补授权).
"""
from __future__ import annotations

from typing import Any, Callable

CallFn = Callable[..., tuple[Any, Any]]


def build_run_observation(
    *,
    session_id: str,
    status_http: int | None,
    status_body: Any,
    message_http: int | None = None,
    messages: Any = None,
    dispatch_user_message_id: str | None = None,
    fetch_messages: bool = True,
) -> dict[str, Any]:
    """Build public observation from already-fetched TA payloads.

    Imports glue parsers (single source of truth for finish/activity).
    """
    import glue as g

    sid = (session_id or "").strip()
    status_ok = isinstance(status_http, int) and 200 <= int(status_http) < 300
    if not status_ok or not sid:
        activity = "unknown"
    else:
        activity = g.parse_session_activity(status_body, sid)

    messages_ok = False
    fin = None
    err = ""
    this_round_found = False
    out_dispatch = str(dispatch_user_message_id or "")

    if fetch_messages:
        messages_ok = (
            isinstance(message_http, int)
            and 200 <= int(message_http) < 300
            and messages is not None
        )
        if messages_ok:
            asst = g.this_round_assistant(
                messages,
                dispatch_user_message_id=out_dispatch or None,
            )
            if not out_dispatch:
                uid = g.latest_user_message_id(messages)
                if uid:
                    out_dispatch = uid
            if asst is not None:
                this_round_found = True
                fin = g.assistant_finish(asst)
                err = g.assistant_error(asst)
    else:
        message_http = None
        messages = None

    finish_successful = bool(fin) and g.assistant_finish_successful(fin)
    # Never invent success when messages were requested but unusable / no this-round
    if fetch_messages and (not messages_ok or not this_round_found):
        finish_successful = False

    cancelled = fin in ("cancelled", "canceled", "cancel")
    errored = bool(err) or fin == "error"

    return {
        "session_id": sid,
        "native_handle": sid or None,
        "activity": activity,  # busy | idle | unknown
        "busy": activity != "idle",
        "status_ok": status_ok,
        "status_http": status_http,
        "messages_ok": messages_ok if fetch_messages else None,
        "message_http": message_http if fetch_messages else None,
        "finish": fin,
        "assistant_error": err or "",
        "this_round_found": this_round_found if fetch_messages else None,
        "dispatch_user_message_id": out_dispatch,
        "finish_successful": finish_successful,
        "cancelled": cancelled,
        "errored": errored,
        "readonly": True,
    }


def fetch_run_observation(
    call: CallFn,
    session_id: str,
    *,
    dispatch_user_message_id: str | None = None,
    fetch_messages: bool = True,
) -> dict[str, Any]:
    """GET /session/status (+ optional /message) via call(method, path), return public obs."""
    sid = (session_id or "").strip()
    status_http: int | None
    status_body: Any
    try:
        status_http, status_body = call("GET", "/session/status")
    except Exception:
        status_http, status_body = None, None

    message_http: int | None = None
    messages: Any = None
    if fetch_messages and sid:
        try:
            message_http, messages = call("GET", f"/session/{sid}/message")
        except Exception:
            message_http, messages = None, None

    return build_run_observation(
        session_id=sid,
        status_http=status_http,
        status_body=status_body,
        message_http=message_http,
        messages=messages,
        dispatch_user_message_id=dispatch_user_message_id,
        fetch_messages=fetch_messages,
    )
