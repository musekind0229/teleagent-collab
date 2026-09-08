"""Linux TeleAgent adapter: HTTP Basic + local-v1 HMAC on :4399."""
from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

from teleagent_adapter.base import (
    AdapterError,
    AdapterStatus,
    TeleAgentAdapterABC,
    filter_by_session,
)

FindCredsFn = Callable[[], tuple[str, str, str]]


def default_find_creds() -> tuple[str, str, str]:
    keys = (b"OPENCODE_SERVER_PASSWORD", b"SUPER_AGENT_LOCAL_SESSION_KEY", b"OPENCODE_SERVER_USERNAME")
    for path in glob.glob("/proc/[0-9]*/environ"):
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        if not all(k in data for k in keys):
            continue
        env: dict[bytes, bytes] = {}
        for item in data.split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                env[k] = v
        return (
            env[b"OPENCODE_SERVER_USERNAME"].decode(),
            env[b"OPENCODE_SERVER_PASSWORD"].decode(),
            env[b"SUPER_AGENT_LOCAL_SESSION_KEY"].decode(),
        )
    raise AdapterError(AdapterStatus.MISSING_CREDS, "TeleAgent local API creds not found in process environ")


def prompt_body(text: str, model: dict | None = None, agent: str | None = None) -> dict:
    return {
        "parts": [{"type": "text", "text": text}],
        "model": model
        or {
            "providerID": os.environ.get("TELEAGENT_PROVIDER_ID", "NewApi"),
            "modelID": os.environ.get("TELEAGENT_MODEL_ID", "chat-lite"),
        },
        "agent": agent or os.environ.get("TELEAGENT_AGENT", "opencowork-default"),
        "queryID": f"q_{uuid.uuid4()}",
    }


class LinuxLocalV1Adapter(TeleAgentAdapterABC):
    """Basic + X-SA-* local-v1 HMAC against TeleAgent SAC HTTP (default :4399)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4399",
        *,
        find_creds_fn: FindCredsFn | None = None,
        lazy_creds: bool = True,
        allow_non_loopback: bool = False,
    ) -> None:
        self.base_url = (base_url or "http://127.0.0.1:4399").rstrip("/")
        self._allow_non_loopback = bool(allow_non_loopback)
        self._assert_loopback_base()
        self._find_creds = find_creds_fn or default_find_creds
        self._creds: tuple[str, str, str] | None = None
        if not lazy_creds:
            self.refresh_creds()

    def _assert_loopback_base(self) -> None:
        """Refuse non-loopback base_url unless explicitly allowed (auth headers stay local)."""
        if self._allow_non_loopback:
            return
        n = urlparse(self.base_url)
        host = (n.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise AdapterError(
                AdapterStatus.BLOCKED,
                f"linux adapter base_url must be loopback (got host={host!r}); "
                "pass allow_non_loopback=True only for deliberate exceptions",
            )

    def refresh_creds(self) -> None:
        self._creds = self._find_creds()

    def _ensure_creds(self) -> tuple[str, str, str]:
        if self._creds is None:
            self.refresh_creds()
        assert self._creds is not None
        return self._creds

    def reconnect(self) -> None:
        """Re-resolve default base + refresh creds once (no IPv6 / no auth disable)."""
        if "://" not in self.base_url:
            self.base_url = "http://127.0.0.1:4399"
        # Prefer IPv4 loopback explicitly
        if "localhost" in self.base_url:
            self.base_url = self.base_url.replace("localhost", "127.0.0.1")
        self._assert_loopback_base()
        self.refresh_creds()

    def sign_headers(self, method: str, url: str) -> dict[str, str]:
        user, pw, key = self._ensure_creds()
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

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: dict | None = None,
        timeout: float = 120,
        *,
        _retried: bool = False,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        try:
            h = self.sign_headers(method, url)
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(AdapterStatus.MISSING_CREDS, str(e)) from e
        if extra_headers:
            h.update(extra_headers)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, headers=h, method=method, data=data)
        # Do not honor HTTP(S)_PROXY for local TeleAgent — auth headers must not leave the box.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                # Reject redirects that leave loopback when hardening is on
                final = resp.geturl() if hasattr(resp, "geturl") else url
                final_host = (urlparse(final).hostname or "").lower()
                if (
                    not self._allow_non_loopback
                    and final_host
                    and final_host not in ("127.0.0.1", "localhost", "::1")
                ):
                    raise AdapterError(
                        AdapterStatus.BLOCKED,
                        f"refusing non-loopback redirect host={final_host!r}",
                    )
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
            if e.code in (401, 403) and not _retried:
                # auth_failed → refresh once per reconnect_policy
                try:
                    self.refresh_creds()
                    return self.call(method, path, body=body, extra_headers=extra_headers, timeout=timeout, _retried=True)
                except AdapterError:
                    raise AdapterError(AdapterStatus.AUTH_FAILED, f"HTTP {e.code}")
            return e.code, j
        except urllib.error.URLError as e:
            if not _retried:
                self.reconnect()
                return self.call(method, path, body=body, extra_headers=extra_headers, timeout=timeout, _retried=True)
            raise AdapterError(AdapterStatus.NOT_RUNNING, str(e.reason if hasattr(e, "reason") else e)) from e

    def create_session(self, *, title: str, directory: str) -> tuple[int, Any]:
        return self.call(
            "POST",
            "/session",
            body={"title": title, "directory": directory},
            extra_headers={"x-opencode-directory": directory},
        )

    def prompt(self, session_id: str, text: str, *, directory: str | None = None) -> tuple[int, Any]:
        headers = {"x-opencode-directory": directory} if directory else None
        return self.call(
            "POST",
            f"/session/{session_id}/prompt_async",
            body=prompt_body(text),
            extra_headers=headers,
        )

    def list_permissions(self, *, session_id: str | None = None) -> tuple[int, list]:
        code, pending = self.call("GET", "/permission")
        items = pending if isinstance(pending, list) else []
        return code, filter_by_session(items, session_id)

    def list_questions(self, *, session_id: str | None = None) -> tuple[int, list]:
        code, qs = self.call("GET", "/question")
        items = qs if isinstance(qs, list) else []
        return code, filter_by_session(items, session_id)

    def reply_question(self, request_id: str, answers: list) -> tuple[int, Any]:
        """POST /question/:id/reply — body.answers is [][]string (SAC 1.2.x)."""
        norm: list = []
        for a in answers or []:
            if isinstance(a, (list, tuple)):
                norm.append([str(x) for x in a])
            else:
                norm.append([str(a)])
        return self.call("POST", f"/question/{request_id}/reply", body={"answers": norm})

    def reject_question(self, request_id: str) -> tuple[int, Any]:
        return self.call("POST", f"/question/{request_id}/reject", body={})

    def reply_permission(self, request_id: str, reply: str) -> tuple[int, Any]:
        # Default once; never auto-always
        r = (reply or "reject").strip().lower()
        if r == "always":
            r = "once"
        if r not in ("once", "reject"):
            r = "reject"
        return self.call("POST", f"/permission/{request_id}/reply", body={"reply": r})

    def session_status(self, session_id: str | None = None) -> tuple[int, Any]:
        code, status = self.call("GET", "/session/status")
        if session_id and isinstance(status, dict) and session_id in status:
            return code, {session_id: status[session_id]}
        if session_id and isinstance(status, list):
            return code, [x for x in status if isinstance(x, dict) and (
                x.get("sessionID") == session_id or x.get("id") == session_id
            )]
        return code, status

    def cancel(self, session_id: str) -> tuple[int, Any]:
        return self.call("POST", f"/session/{session_id}/abort", body={})

    def resume(self, session_id: str) -> tuple[int, Any]:
        """Verify session exists; return (code, session_obj_or_error)."""
        code, data = self.call("GET", f"/session/{session_id}")
        if code < 300 and data:
            return code, data
        # fallback: list sessions
        code2, listing = self.call("GET", "/session")
        if isinstance(listing, list):
            for item in listing:
                if isinstance(item, dict) and item.get("id") == session_id:
                    return code2, item
        if isinstance(listing, dict) and session_id in listing:
            return code2, listing[session_id]
        raise AdapterError(AdapterStatus.API_INCOMPATIBLE, f"session {session_id} not found")


__all__ = ["LinuxLocalV1Adapter", "default_find_creds", "prompt_body"]
