#!/usr/bin/env python3
"""Strict completion criteria (条2): all required artifacts, no timeout/cancel success,
rework budget that cannot be reset, acceptance packet + artifact fingerprint gate.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MAX_REWORKS = 1
DEFAULT_MAX_WALL_SEC = 600


@dataclass
class ArtifactRecord:
    path: str
    exists: bool
    size: int | None = None
    mtime_ns: int | None = None
    sha256: str | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "exists": self.exists,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "error": self.error,
        }


def file_sha256(path: Path, *, max_bytes: int = 8_000_000) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        remaining = max_bytes
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def snapshot_artifact(path: str | Path, *, hash_content: bool = True) -> ArtifactRecord:
    p = Path(path)
    rec = ArtifactRecord(path=str(p), exists=False)
    try:
        if not p.exists() or not p.is_file():
            return rec
        st = p.stat()
        rec.exists = True
        rec.size = int(st.st_size)
        rec.mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        if hash_content:
            rec.sha256 = file_sha256(p)
    except OSError as e:
        rec.error = str(e)
    return rec


def snapshot_artifacts(paths: Iterable[str | Path], *, hash_content: bool = True) -> list[ArtifactRecord]:
    return [snapshot_artifact(p, hash_content=hash_content) for p in paths]


def artifacts_all_present(records: list[ArtifactRecord]) -> bool:
    """ALL required artifacts must exist — presence of any subset is not success."""
    if not records:
        return False
    return all(r.exists and not r.error for r in records)


def artifacts_unchanged(before: list[ArtifactRecord], after: list[ArtifactRecord]) -> tuple[bool, list[str]]:
    """Confirm hash/mtime unchanged between submit and lead approve."""
    by_path = {r.path: r for r in after}
    diffs: list[str] = []
    for b in before:
        a = by_path.get(b.path)
        if a is None:
            diffs.append(f"missing_in_after:{b.path}")
            continue
        if b.exists != a.exists:
            diffs.append(f"exists_changed:{b.path}")
            continue
        if not b.exists:
            continue
        if b.sha256 and a.sha256 and b.sha256 != a.sha256:
            diffs.append(f"sha256_changed:{b.path}")
        elif b.mtime_ns is not None and a.mtime_ns is not None and b.mtime_ns != a.mtime_ns:
            diffs.append(f"mtime_changed:{b.path}")
        elif b.size is not None and a.size is not None and b.size != a.size:
            diffs.append(f"size_changed:{b.path}")
    return (len(diffs) == 0), diffs


def expected_exists(paths: list[str]) -> list[str]:
    """Return existing paths only (inventory helper). Does NOT imply job success."""
    return [p for p in paths if Path(p).exists()]


def missing_artifacts(paths: list[str]) -> list[str]:
    return [p for p in paths if not Path(p).exists()]


@dataclass
class ReworkBudget:
    """Wall clock + rework count; restart/redo must NOT extend or reset these."""

    wall_deadline: float
    max_reworks: int = DEFAULT_MAX_REWORKS
    reworks_used: int = 0
    started_at: float = field(default_factory=time.time)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, timeout_sec: int | float, *, max_reworks: int = DEFAULT_MAX_REWORKS) -> "ReworkBudget":
        now = time.time()
        return cls(
            wall_deadline=now + float(timeout_sec),
            max_reworks=max(0, int(max_reworks)),
            reworks_used=0,
            started_at=now,
        )

    def remaining_sec(self) -> float:
        return max(0.0, self.wall_deadline - time.time())

    def exhausted_wall(self) -> bool:
        return time.time() >= self.wall_deadline

    def can_rework(self) -> bool:
        return self.reworks_used < self.max_reworks and not self.exhausted_wall()

    def consume_rework(self) -> bool:
        """Spend one rework. Never extends wall_deadline."""
        if not self.can_rework():
            self.notes.append("rework denied: budget exhausted")
            return False
        self.reworks_used += 1
        self.notes.append(f"rework consumed ({self.reworks_used}/{self.max_reworks}); wall unchanged")
        return True

    def clamp_subdeadline(self, desired_extra: float) -> float:
        """Sub-poll deadline capped by remaining wall — never past wall_deadline."""
        return min(time.time() + max(0.0, desired_extra), self.wall_deadline)

    def to_dict(self) -> dict:
        return {
            "wall_deadline": self.wall_deadline,
            "max_reworks": self.max_reworks,
            "reworks_used": self.reworks_used,
            "started_at": self.started_at,
            "remaining_sec": self.remaining_sec(),
            "notes": list(self.notes),
        }


TERMINAL_FAIL_STATES = frozenset({"fail", "timeout", "cancelled", "cancel", "error"})


def is_success_allowed(
    *,
    state: str,
    artifacts_ok: bool,
    lead_verdict: str | None = None,
    force_lead_review: bool = False,
    finish: str | None = None,
    error: str | None = None,
) -> tuple[bool, str]:
    """Strict success gate. Missing/error/timeout/cancel never succeed.

    When force_lead_review, lead_verdict must be 'pass' and artifacts_ok.
    Partial artifacts (any-of) are rejected by artifacts_ok=False.
    """
    st = (state or "").lower().strip()
    if error:
        return False, f"error:{error[:200]}"
    if st in TERMINAL_FAIL_STATES:
        return False, f"terminal_state:{st}"
    if finish in ("error", "cancelled", "cancel"):
        return False, f"finish:{finish}"
    if not artifacts_ok:
        return False, "artifacts_incomplete"
    if force_lead_review:
        if (lead_verdict or "").lower() != "pass":
            return False, f"lead_verdict_required_pass got={lead_verdict!r}"
    return True, "ok"


def build_acceptance_packet(
    *,
    job_name: str,
    goal: str,
    acceptance_criteria: Any,
    expected_artifacts: list[str],
    execution_result: dict | None = None,
    error: str = "",
    tool_records: list | None = None,
    api_replies: list | None = None,
    notes: list | None = None,
    state: str = "",
    session_id: str = "",
    hash_content: bool = True,
) -> dict:
    """Packet submitted to lead for acceptance: results, errors, tools, artifact inventory."""
    arts = snapshot_artifacts(expected_artifacts, hash_content=hash_content)
    return {
        "kind": "acceptance_submit",
        "job_name": job_name,
        "goal": goal,
        "acceptance_criteria": acceptance_criteria,
        "execution_result": execution_result or {},
        "error": error or "",
        "state": state,
        "session_id": session_id,
        "tool_records": list(tool_records or []),
        "api_replies": list(api_replies or []),
        "notes": list(notes or []),
        "artifacts": [r.to_dict() for r in arts],
        "artifacts_complete": artifacts_all_present(arts),
        "missing": [r.path for r in arts if not r.exists],
        "submitted_at": time.time(),
    }


def confirm_artifacts_for_lead_approve(packet: dict, *, hash_content: bool = True) -> tuple[bool, dict]:
    """Before lead approval is finalized: re-snapshot and compare to packet inventory."""
    before = [
        ArtifactRecord(
            path=a["path"],
            exists=bool(a.get("exists")),
            size=a.get("size"),
            mtime_ns=a.get("mtime_ns"),
            sha256=a.get("sha256"),
            error=a.get("error") or "",
        )
        for a in (packet.get("artifacts") or [])
        if isinstance(a, dict)
    ]
    paths = [r.path for r in before] or []
    after = snapshot_artifacts(paths, hash_content=hash_content)
    ok, diffs = artifacts_unchanged(before, after)
    return ok, {
        "unchanged": ok,
        "diffs": diffs,
        "before": [r.to_dict() for r in before],
        "after": [r.to_dict() for r in after],
    }


def format_acceptance_prompt(packet: dict, *, charter: dict | None = None) -> str:
    """Full-context acceptance prompt (stateless lead)."""
    charter = charter or {}
    blob = json.dumps(packet, ensure_ascii=False, indent=2, default=str)
    return (
        "You are the team lead accepting a TeleAgent worker delivery. "
        "Judge ONLY against the acceptance criteria and artifact inventory. "
        "Output strict JSON: {\"verdict\":\"pass\"|\"fail\",\"reason\":string,\"application_id\":string}. "
        "If any required artifact is missing, content fails criteria, or fingerprints look wrong → fail.\n"
        f"Task goal: {packet.get('goal') or charter.get('goal') or ''}\n"
        f"Authorized scope (must): {json.dumps(charter.get('must') or [], ensure_ascii=False)}\n"
        f"Prohibitions (must_not): {json.dumps(charter.get('must_not') or [], ensure_ascii=False)}\n"
        f"Acceptance criteria: {json.dumps(packet.get('acceptance_criteria') or charter.get('acceptance') or charter.get('done_when') or {}, ensure_ascii=False)}\n"
        f"Acceptance packet:\n{blob}\n"
    )


__all__ = [
    "ArtifactRecord",
    "ReworkBudget",
    "DEFAULT_MAX_REWORKS",
    "snapshot_artifact",
    "snapshot_artifacts",
    "artifacts_all_present",
    "artifacts_unchanged",
    "expected_exists",
    "missing_artifacts",
    "is_success_allowed",
    "build_acceptance_packet",
    "confirm_artifacts_for_lead_approve",
    "format_acceptance_prompt",
]
