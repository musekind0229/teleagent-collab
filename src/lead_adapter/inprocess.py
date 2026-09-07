"""In-process / file-protocol LeadAdapter — current Codex (or human) conversation as lead.

Does NOT spawn a new Grok/Codex process. Pending asks land on disk (or queue);
decisions are submitted via the same structured JSON protocol.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from lead_adapter.base import LeadAdapterABC, safe_failure
from lead_adapter.schema import format_lead_request_prompt


class InProcessLeadAdapter(LeadAdapterABC):
    """File drop + optional callback for the current conversation acting as lead.

    Layout under exchange_dir (default COLLAB_LEAD_EXCHANGE or ./lead_exchange):
      pending/<application_id>.json   — full LeadRequest (+ prompt text)
      decisions/<application_id>.json — lead JSON decision
      archive/                        — moved after consume

    Env:
      COLLAB_LEAD_EXCHANGE — directory
      COLLAB_LEAD_INPROCESS_TIMEOUT — default wait seconds (also per-call timeout_sec)
    """

    name = "inprocess"

    def __init__(
        self,
        *,
        exchange_dir: str | Path | None = None,
        decision_fn: Callable[[dict, dict], dict] | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        root = Path(
            exchange_dir
            or os.environ.get("COLLAB_LEAD_EXCHANGE")
            or "/workspace/teleagent-collab/jobs/lead_exchange"
        )
        self.exchange_dir = root
        self.pending_dir = root / "pending"
        self.decisions_dir = root / "decisions"
        self.archive_dir = root / "archive"
        for d in (self.pending_dir, self.decisions_dir, self.archive_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.decision_fn = decision_fn  # sync hook for unit tests / embedded lead
        self.poll_interval = poll_interval
        self._lock = threading.Lock()

    def decide(
        self,
        request: dict,
        *,
        schema: dict,
        cwd: str,
        timeout_sec: float = 180,
    ) -> tuple[str, dict | None]:
        app_id = str(request.get("application_id") or "")
        if not app_id:
            return safe_failure("error", "missing application_id")

        envelope = {
            "request": request,
            "schema": schema,
            "cwd": cwd,
            "prompt": format_lead_request_prompt(request),
            "written_at": time.time(),
        }
        pending_path = self.pending_dir / f"{app_id}.json"
        with self._lock:
            pending_path.write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )

        # Embedded/sync decision (Codex-in-process or test fake)
        if self.decision_fn is not None:
            try:
                decision = self.decision_fn(request, schema)
            except Exception as e:
                return safe_failure("call_failed", f"decision_fn error: {e}")
            if not isinstance(decision, dict):
                return safe_failure("error", "decision_fn did not return dict")
            self._write_decision(app_id, decision)
            raw = json.dumps(decision, ensure_ascii=False)
            self._archive_pending(app_id)
            return raw, decision

        # Wait for external writer (current conversation / human / sidecar)
        deadline = time.time() + float(timeout_sec)
        decision_path = self.decisions_dir / f"{app_id}.json"
        while time.time() < deadline:
            if decision_path.exists():
                try:
                    text = decision_path.read_text(encoding="utf-8")
                    obj = json.loads(text)
                except Exception as e:
                    return safe_failure("error", f"invalid decision file: {e}")
                if not isinstance(obj, dict):
                    return safe_failure("error", "decision file not a JSON object")
                self._archive_pending(app_id)
                return text[:3000], obj
            time.sleep(self.poll_interval)

        return safe_failure("timeout", f"inprocess wait exceeded for {app_id}")

    def submit_decision(self, application_id: str, decision: dict) -> Path:
        """Helper for the current conversation to submit a structured decision."""
        return self._write_decision(application_id, decision)

    def list_pending(self) -> list[dict]:
        out: list[dict] = []
        for p in sorted(self.pending_dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def _write_decision(self, application_id: str, decision: dict) -> Path:
        path = self.decisions_dir / f"{application_id}.json"
        path.write_text(
            json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def _archive_pending(self, application_id: str) -> None:
        src = self.pending_dir / f"{application_id}.json"
        if src.exists():
            dest = self.archive_dir / f"{application_id}.json"
            try:
                src.replace(dest)
            except OSError:
                pass


__all__ = ["InProcessLeadAdapter"]
