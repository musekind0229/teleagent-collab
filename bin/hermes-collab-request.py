#!/usr/bin/env python3
"""Minimal Application API client for Hermes (or any terminal agent).

Thin HTTP wrapper over collab-service loopback API. Does NOT own account
pools, agy subprocesses, or Hermes ledger state.

Env:
  COLLAB_API_BASE   default http://127.0.0.1:8765
  COLLAB_API_TOKEN  optional; sent as Authorization: Bearer …

Subcommands: open | status | report | wait
Stdout: one JSON object. Non-zero exit = HTTP/transport failure or wait timeout
(or wait ended in failed/cancelled).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8765"
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
SUCCESS_STATES = frozenset({"completed"})


class ClientError(Exception):
    """Transport or API failure with a JSON-serializable payload."""

    def __init__(self, payload: dict[str, Any], *, exit_code: int = 1) -> None:
        super().__init__(str(payload.get("error") or payload.get("code") or "error"))
        self.payload = payload
        self.exit_code = exit_code


def _base_url() -> str:
    return (os.environ.get("COLLAB_API_BASE") or DEFAULT_BASE).rstrip("/")


def _token() -> str:
    return (os.environ.get("COLLAB_API_TOKEN") or "").strip()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _headers(*, with_body: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if with_body:
        headers["Content-Type"] = "application/json; charset=utf-8"
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """HTTP JSON call. path is absolute under base (e.g. /v1/requests/…)."""
    url = _base_url() + path
    data = None
    headers = _headers(with_body=body is not None)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw or e.reason, "code": "http_error"}
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "non-object error body", "code": "http_error"}
        payload.setdefault("ok", False)
        payload.setdefault("http_status", int(e.code))
        raise ClientError(payload, exit_code=1) from e
    except urllib.error.URLError as e:
        raise ClientError(
            {
                "ok": False,
                "code": "transport_error",
                "error": str(getattr(e, "reason", e)),
            },
            exit_code=1,
        ) from e
    except TimeoutError as e:
        raise ClientError(
            {"ok": False, "code": "timeout", "error": "HTTP request timed out"},
            exit_code=1,
        ) from e

    if not raw.strip():
        return {"ok": True, "http_status": status}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClientError(
            {
                "ok": False,
                "code": "invalid_json",
                "error": f"response is not JSON: {e}",
                "http_status": status,
            },
            exit_code=1,
        ) from e
    if not isinstance(payload, dict):
        raise ClientError(
            {
                "ok": False,
                "code": "invalid_json",
                "error": "response JSON must be an object",
                "http_status": status,
            },
            exit_code=1,
        )
    payload.setdefault("http_status", status)
    return payload


def _encode_id(request_id: str) -> str:
    return urllib.parse.quote(str(request_id), safe="")


def cmd_open(args: argparse.Namespace) -> dict[str, Any]:
    artifacts = list(args.artifact or [])
    if not artifacts:
        artifacts = ["delivery.md"]
    must = list(args.must or [])
    must_not = list(args.must_not or [])
    body: dict[str, Any] = {
        "client_id": args.client_id,
        "title": args.title or (args.goal[:80] if args.goal else "hermes-collab"),
        "goal": args.goal,
        "boundaries": {"must": must, "must_not": must_not},
        "acceptance": {
            "artifacts": artifacts,
            "text": args.acceptance_text
            or (", ".join(artifacts) + " exist"),
        },
        "budget": {
            "wall_sec": int(args.wall_sec),
            "max_reworks": int(args.max_reworks),
        },
    }
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key
    # Backend is selected when collab-service starts. Optional flag is a
    # caller annotation only (server ignores unknown fields today).
    if args.backend:
        body["caller_backend_hint"] = args.backend
    return request_json("POST", "/v1/requests", body=body, timeout=float(args.http_timeout))


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    rid = _encode_id(args.request_id)
    return request_json("GET", f"/v1/requests/{rid}", timeout=float(args.http_timeout))


def cmd_report(args: argparse.Namespace) -> dict[str, Any]:
    rid = _encode_id(args.request_id)
    return request_json("GET", f"/v1/requests/{rid}/report", timeout=float(args.http_timeout))


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    rid = _encode_id(args.request_id)
    deadline = time.monotonic() + float(args.timeout)
    interval = max(0.2, float(args.interval))
    last: dict[str, Any] = {}
    while True:
        last = request_json("GET", f"/v1/requests/{rid}", timeout=float(args.http_timeout))
        state = str(last.get("state") or "")
        if state in TERMINAL_STATES:
            last["wait"] = {"terminal": True, "state": state}
            if state not in SUCCESS_STATES:
                raise ClientError(last, exit_code=2)
            return last
        if time.monotonic() >= deadline:
            last["wait"] = {
                "terminal": False,
                "timed_out": True,
                "state": state,
                "timeout_sec": float(args.timeout),
            }
            raise ClientError(last, exit_code=3)
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-collab-request.py",
        description=(
            "Minimal collab Application API client (Hermes-friendly). "
            "Reads COLLAB_API_BASE / COLLAB_API_TOKEN."
        ),
    )
    p.add_argument(
        "--http-timeout",
        type=float,
        default=30.0,
        help="per-request HTTP timeout seconds (default 30)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="POST /v1/requests")
    p_open.add_argument("--goal", required=True, help="desired outcome text")
    p_open.add_argument("--title", default="", help="short title (default: goal prefix)")
    p_open.add_argument("--client-id", default="hermes", help="client_id (default hermes)")
    p_open.add_argument("--idempotency-key", default="", help="optional idempotency key")
    p_open.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="acceptance artifact relative path (repeatable; default delivery.md)",
    )
    p_open.add_argument("--acceptance-text", default="", help="acceptance.text override")
    p_open.add_argument("--must", action="append", default=[], help="boundaries.must (repeatable)")
    p_open.add_argument(
        "--must-not",
        action="append",
        default=[],
        help="boundaries.must_not (repeatable)",
    )
    p_open.add_argument("--wall-sec", type=int, default=300, help="budget.wall_sec")
    p_open.add_argument("--max-reworks", type=int, default=1, help="budget.max_reworks")
    p_open.add_argument(
        "--backend",
        choices=("teleagent-windows", "antigravity", "agy", "inprocess"),
        default="",
        help=(
            "optional caller annotation only; worker backend is chosen at "
            "collab-service start (teleagent-windows | antigravity | …)"
        ),
    )
    p_open.set_defaults(func=cmd_open)

    p_st = sub.add_parser("status", help="GET /v1/requests/{id}")
    p_st.add_argument("request_id", help="request_id / goal_id")
    p_st.set_defaults(func=cmd_status)

    p_rep = sub.add_parser("report", help="GET /v1/requests/{id}/report")
    p_rep.add_argument("request_id", help="request_id / goal_id")
    p_rep.set_defaults(func=cmd_report)

    p_wait = sub.add_parser("wait", help="poll status until terminal or timeout")
    p_wait.add_argument("request_id", help="request_id / goal_id")
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="wall-clock seconds to wait (default 600)",
    )
    p_wait.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="poll interval seconds (default 2)",
    )
    p_wait.set_defaults(func=cmd_wait)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
    except ClientError as e:
        _emit(e.payload)
        return int(e.exit_code)
    except KeyboardInterrupt:
        _emit({"ok": False, "code": "interrupted", "error": "KeyboardInterrupt"})
        return 130
    _emit(payload)
    if isinstance(payload, dict) and payload.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
