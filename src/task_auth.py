#!/usr/bin/env python3
"""Task authorization scope (条5).

Separates:
  - *mechanical isolation* (path/network/install-root checks we actually enforce)
  - *prompt constraints* (must/must_not text fed to worker/lead — soft)

Lead may approve inside user-authorized charter scope without re-pinging the user
each time. Lead must NOT treat "install" as unbounded system privilege.
Permissions listed in user_gate_permissions always bounce back to the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from pathutil import canonicalize, is_path_within

TASK_KINDS = frozenset({"file_task", "system_install"})

# Steps that are always lead-review candidates unless charter narrows them.
DEFAULT_LEAD_REVIEW_STEPS = (
    "plan_commit",
    "permission_grey",
    "outbound_auth",
    "job_end",
    "install_execute",
    "service_change",
)

# Permission categories that never auto-expand to "whole system".
USER_GATE_DEFAULTS_SYSTEM_INSTALL = (
    "sudo",
    "systemd_unit_install",
    "firewall_change",
    "kernel_module",
    "ssh_listen_bind",
    "credential_store_write",
)


@dataclass
class AuthDecision:
    allowed: bool
    via: str  # mechanical | prompt_only | user_gate | lead_scope | deny
    reason: str
    needs_lead: bool = False
    needs_user: bool = False
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "via": self.via,
            "reason": self.reason,
            "needs_lead": self.needs_lead,
            "needs_user": self.needs_user,
            "extras": self.extras,
        }


def normalize_task_kind(raw: Any) -> str:
    if raw is None or raw == "":
        return "file_task"
    s = str(raw).strip().lower()
    if s in ("file", "files", "workspace", "file_task"):
        return "file_task"
    if s in ("install", "system", "system_install", "sys_install"):
        return "system_install"
    if s not in TASK_KINDS:
        raise ValueError(f"task_kind must be one of {sorted(TASK_KINDS)}, got {raw!r}")
    return s


def _as_str_list(val: Any, *, field_name: str) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if not isinstance(val, list):
        raise ValueError(f"{field_name} must be a list of strings")
    out: list[str] = []
    for item in val:
        if item is None:
            continue
        out.append(str(item))
    return out


def extract_auth_fields(charter: dict | None) -> dict:
    """Normalize authorization-related charter fields (defaults for missing)."""
    c = charter or {}
    kind = normalize_task_kind(c.get("task_kind"))
    network_allow = _as_str_list(c.get("network_allow"), field_name="network_allow")
    install_roots = _as_str_list(c.get("install_roots"), field_name="install_roots")
    lead_review_steps = _as_str_list(c.get("lead_review_steps"), field_name="lead_review_steps")
    if not lead_review_steps:
        lead_review_steps = list(DEFAULT_LEAD_REVIEW_STEPS)
        if kind == "file_task":
            lead_review_steps = [s for s in lead_review_steps if s not in ("install_execute", "service_change")]
    user_gate = _as_str_list(c.get("user_gate_permissions"), field_name="user_gate_permissions")
    if kind == "system_install" and not user_gate:
        user_gate = list(USER_GATE_DEFAULTS_SYSTEM_INSTALL)
    acceptance = c.get("acceptance")
    if acceptance is None:
        acceptance = c.get("done_when")
    rollback = c.get("rollback")
    if rollback is None:
        rollback = ""
    elif isinstance(rollback, dict):
        rollback = rollback
    else:
        rollback = str(rollback)
    return {
        "task_kind": kind,
        "network_allow": network_allow,
        "install_roots": install_roots,
        "lead_review_steps": lead_review_steps,
        "user_gate_permissions": user_gate,
        "acceptance": acceptance,
        "rollback": rollback,
    }


def validate_auth_fields(charter: dict) -> None:
    """Raise ValueError if authorization fields are malformed or unsafe combo."""
    auth = extract_auth_fields(charter)
    kind = auth["task_kind"]
    if kind == "system_install":
        if not auth["install_roots"]:
            raise ValueError(
                "system_install charter requires non-empty install_roots "
                "(install ≠ unbounded system privilege)"
            )
        if not auth["rollback"]:
            raise ValueError("system_install charter requires rollback plan (string or mapping)")
        if not auth["network_allow"]:
            # Allow empty only if explicit key present as []
            if "network_allow" not in charter:
                raise ValueError(
                    "system_install charter must declare network_allow "
                    "(use [] if no outbound downloads)"
                )
    # file_task: install_roots should be empty or omitted
    if kind == "file_task" and auth["install_roots"]:
        raise ValueError(
            "file_task must not set install_roots; use task_kind=system_install for installs"
        )


def isolation_capabilities_doc() -> dict:
    """What we *actually* enforce vs prompt-only constraints."""
    return {
        "mechanical_isolation": [
            "Per-job workdir (scheduler) — writes land under jobs/workspaces/<job_id>/",
            "Path canonicalize + is_path_within for workspace R/W checks",
            "Hard rules eternal-reject for ~/.ssh, cookies, gh hosts, .netrc",
            "Charter allow_secret_globs / allow_paths / allow_keys gate secret paths",
            "install_roots containment for system_install path targets (task_auth)",
            "network_allow host/prefix match for declared download URLs (task_auth)",
            "user_gate_permissions never auto-approved by lead (task_auth)",
            "State store: decisions recorded; restart does not re-send or re-claim sessions",
            "Cancel/timeout scoped to a single job_id",
        ],
        "prompt_constraints_only": [
            "must / must_not free text in worker instruction",
            "acceptance prose / rollback prose fed to lead",
            "lead_review_steps labels (orchestration hint; not a kernel sandbox)",
            "allowed_surfaces strings without OS-level enforcement",
            "TeleAgent session directory hint (x-opencode-directory) — soft boundary",
        ],
        "explicit_non_isolation": [
            "No container/VM sandbox around TeleAgent worker process",
            "No seccomp / Landlock / AppArmor profile applied by this repo",
            "Lead approval inside charter scope ≠ OS capability bounding",
            "Installing software is never interpreted as root-anywhere privilege",
        ],
    }


def _host_allowed(url_or_host: str, allow: Iterable[str]) -> bool:
    allow_l = [a.strip().lower() for a in allow if str(a).strip()]
    if not allow_l:
        return False
    raw = (url_or_host or "").strip()
    if not raw:
        return False
    host = raw
    if "://" in raw:
        host = urlparse(raw).hostname or ""
    host = host.lower().rstrip(".")
    for a in allow_l:
        if a.startswith("*."):
            suf = a[1:]  # .example.com
            if host.endswith(suf) or host == a[2:]:
                return True
        elif a.startswith("http://") or a.startswith("https://"):
            ah = urlparse(a).hostname or ""
            if host == ah.lower():
                return True
        else:
            if host == a or host.endswith("." + a):
                return True
    return False


def path_in_install_roots(path: str | Path, roots: Iterable[str], *, base: str | None = None) -> bool:
    if not roots:
        return False
    target = canonicalize(str(path), base=base)
    for root in roots:
        # Relative roots resolve against job workdir when base is provided (P3).
        r = canonicalize(str(root), base=base)
        if is_path_within(target, r) or target == r:
            return True
    return False


def classify_permission_category(perm: dict) -> str:
    """Rough category for user_gate matching."""
    tool = str(perm.get("tool") or perm.get("permission") or "").lower()
    cmd = str(perm.get("command") or perm.get("bash") or "").lower()
    path = str(perm.get("path") or "").lower()
    blob = f"{tool} {cmd} {path}"
    if "sudo" in blob or tool == "sudo":
        return "sudo"
    if "systemctl" in blob or "systemd" in blob or blob.endswith(".service"):
        return "systemd_unit_install"
    if "ufw" in blob or "iptables" in blob or "firewall" in blob:
        return "firewall_change"
    if "insmod" in blob or "modprobe" in blob:
        return "kernel_module"
    if "sshd" in blob or ":22" in blob or "ssh -R" in blob:
        return "ssh_listen_bind"
    if any(x in path for x in ("/.ssh", "cookie", "keyring", ".netrc", "credentials")):
        return "credential_store_write"
    if tool in ("bash", "shell", "exec") and ("apt" in cmd or "dnf" in cmd or "yum" in cmd or "pacman" in cmd):
        return "package_manager"
    if tool in ("bash", "shell", "exec") and ("curl" in cmd or "wget" in cmd):
        return "network_fetch"
    return tool or "unknown"


def authorize_action(
    *,
    charter: dict | None,
    path: str | None = None,
    url: str | None = None,
    permission: dict | None = None,
    step: str | None = None,
    workspace: str | Path | None = None,
) -> AuthDecision:
    """Decide if an action stays inside charter-authorized scope.

    Mechanical checks first; prompt-only fields never alone grant privilege.
    """
    auth = extract_auth_fields(charter)
    kind = auth["task_kind"]
    perm = permission or {}
    category = classify_permission_category(perm) if perm else ""

    # User gate always wins — lead cannot silently expand.
    gates = {g.lower() for g in auth["user_gate_permissions"]}
    if category and category.lower() in gates:
        return AuthDecision(
            allowed=False,
            via="user_gate",
            reason=f"category {category!r} requires user re-authorization (user_gate_permissions)",
            needs_user=True,
            extras={"category": category, "task_kind": kind},
        )

    if step and step in auth["lead_review_steps"]:
        # Still may be mechanically ok, but orchestration should ping lead.
        needs_lead = True
    else:
        needs_lead = False

    ws = canonicalize(str(workspace)) if workspace else None

    if url:
        if not _host_allowed(url, auth["network_allow"]):
            return AuthDecision(
                allowed=False,
                via="mechanical",
                reason=f"url/host not in network_allow: {url!r}",
                needs_lead=False,
                extras={"network_allow": auth["network_allow"]},
            )

    if kind == "file_task":
        if path and ws:
            if not is_path_within(canonicalize(path, base=ws), ws):
                return AuthDecision(
                    allowed=False,
                    via="mechanical",
                    reason=f"file_task path outside workspace: {path}",
                    extras={"workspace": ws},
                )
        if category in ("package_manager", "systemd_unit_install", "sudo"):
            return AuthDecision(
                allowed=False,
                via="mechanical",
                reason=f"file_task forbids system category {category!r}",
                extras={"hint": "use task_kind=system_install with install_roots"},
            )
        return AuthDecision(
            allowed=True,
            via="lead_scope" if needs_lead else "mechanical",
            reason="file_task within workspace / charter scope",
            needs_lead=needs_lead,
            extras={"task_kind": kind},
        )

    # system_install
    if path:
        if not path_in_install_roots(path, auth["install_roots"], base=ws):
            # Workspace-relative docs/scripts may still be ok
            if ws and is_path_within(canonicalize(path, base=ws), ws):
                return AuthDecision(
                    allowed=True,
                    via="mechanical",
                    reason="path inside job workspace (not an install root write)",
                    needs_lead=needs_lead or ("install_execute" in auth["lead_review_steps"]),
                    extras={"workspace": ws},
                )
            return AuthDecision(
                allowed=False,
                via="mechanical",
                reason=f"path not under install_roots: {path}",
                extras={"install_roots": auth["install_roots"]},
            )
    return AuthDecision(
        allowed=True,
        via="lead_scope" if needs_lead else "mechanical",
        reason="system_install within declared install_roots / network_allow",
        needs_lead=needs_lead or True,  # installs always prefer lead eyes
        extras={
            "task_kind": kind,
            "install_roots": auth["install_roots"],
            "rollback_declared": bool(auth["rollback"]),
        },
    )


def lead_may_approve_without_user(charter: dict | None, permission: dict | None = None) -> tuple[bool, str]:
    """Lead can batch-approve inside user-authorized scope; not outside."""
    d = authorize_action(charter=charter, permission=permission or {})
    if d.needs_user:
        return False, d.reason
    if not d.allowed:
        return False, d.reason
    return True, "within charter-authorized scope; lead may approve without re-pinging user"


def auth_summary_for_lead(charter: dict | None) -> dict:
    """Compact block attached to lead requests."""
    auth = extract_auth_fields(charter)
    return {
        **auth,
        "isolation": isolation_capabilities_doc(),
        "note": (
            "Lead may approve operations already inside this charter scope. "
            "Do not interpret install as unbounded system rights. "
            "user_gate_permissions require bouncing to the human user."
        ),
    }


__all__ = [
    "TASK_KINDS",
    "AuthDecision",
    "normalize_task_kind",
    "extract_auth_fields",
    "validate_auth_fields",
    "isolation_capabilities_doc",
    "authorize_action",
    "lead_may_approve_without_user",
    "auth_summary_for_lead",
    "path_in_install_roots",
    "classify_permission_category",
]
