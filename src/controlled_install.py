"""Controlled system_install helper (P3) — fake/rollbackable artifacts only.

Rules:
  - Requires task_kind=system_install + validated auth fields
  - Writes only under charter install_roots that sit inside the job workdir
  - Never runs apt/dnf/yum/sudo/systemd (dangerous real install → BLOCKED)
  - Proves authorization field gating via authorize_action
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pathutil import canonicalize, is_path_within
from task_auth import (
    authorize_action,
    extract_auth_fields,
    path_in_install_roots,
    validate_auth_fields,
)


DANGEROUS_REAL_INSTALL = frozenset(
    {
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "pacman",
        "zypper",
        "brew",
        "sudo",
        "systemctl",
        "dpkg",
        "rpm",
    }
)


@dataclass
class InstallResult:
    ok: bool
    status: str  # installed | denied | blocked | error
    reason: str
    artifact_path: str | None = None
    rollback_path: str | None = None
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "artifact_path": self.artifact_path,
            "rollback_path": self.rollback_path,
            "extras": self.extras,
        }


def _workdir_scoped_roots(charter: dict, workdir: Path) -> list[str]:
    auth = extract_auth_fields(charter)
    ws = canonicalize(str(workdir))
    scoped: list[str] = []
    for root in auth["install_roots"]:
        r = canonicalize(str(root), base=ws)
        if is_path_within(r, ws) or r == ws:
            scoped.append(r)
    return scoped


def assert_auth_gate(charter: dict, *, workdir: Path, target_path: str) -> None:
    """Raise ValueError if auth fields invalid or target outside install_roots."""
    validate_auth_fields(charter)
    auth = extract_auth_fields(charter)
    if auth["task_kind"] != "system_install":
        raise ValueError("controlled_install requires task_kind=system_install")
    d = authorize_action(
        charter=charter,
        path=target_path,
        permission={"tool": "write", "path": target_path},
        workspace=workdir,
    )
    if not d.allowed:
        raise ValueError(f"authorize_action deny: {d.reason}")


def skip_dangerous_real_install(command: str | None) -> InstallResult | None:
    """If command looks like a real package-manager install, return BLOCKED result."""
    if not command:
        return None
    low = command.strip().lower()
    first = low.split()[0] if low.split() else ""
    # also catch sudo apt ...
    tokens = set(low.replace(";", " ").replace("|", " ").split())
    if first in DANGEROUS_REAL_INSTALL or tokens & DANGEROUS_REAL_INSTALL:
        return InstallResult(
            ok=False,
            status="blocked",
            reason=(
                f"dangerous real install skipped (P3): {command!r}. "
                "Only fake/rollbackable artifacts under job workdir install_roots are allowed."
            ),
            extras={"blocked_tokens": sorted(tokens & DANGEROUS_REAL_INSTALL or {first})},
        )
    return None


def install_fake_package(
    *,
    charter: dict,
    workdir: str | Path,
    package_name: str = "collab-fake-pkg",
    version: str = "0.0.1-p3",
    real_install_command: str | None = None,
) -> InstallResult:
    """Install a fake package directory under the first workdir-scoped install_root."""
    blocked = skip_dangerous_real_install(real_install_command)
    if blocked is not None:
        return blocked

    ws = Path(workdir).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    try:
        validate_auth_fields(charter)
    except ValueError as e:
        return InstallResult(False, "denied", f"auth field validation: {e}")

    scoped = _workdir_scoped_roots(charter, ws)
    if not scoped:
        return InstallResult(
            False,
            "denied",
            "no install_roots contained inside job workdir "
            "(refuse absolute system roots outside workdir for P3 fake install)",
            extras={"install_roots": extract_auth_fields(charter)["install_roots"], "workdir": str(ws)},
        )

    root = Path(scoped[0])
    target = root / package_name
    try:
        assert_auth_gate(charter, workdir=ws, target_path=str(target))
    except ValueError as e:
        return InstallResult(False, "denied", str(e))

    # mechanical double-check
    if not path_in_install_roots(str(target), scoped, base=str(ws)):
        return InstallResult(False, "denied", f"target not in scoped install_roots: {target}")

    target.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": package_name,
        "version": version,
        "installed_at": time.time(),
        "fake": True,
        "rollback": f"rm -rf {target}",
        "note": "P3 controlled fake package — not a real system install",
    }
    (target / "FAKE_PACKAGE.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (target / "bin").mkdir(exist_ok=True)
    stub = target / "bin" / package_name
    stub.write_text(
        f"#!/bin/sh\necho '{package_name} {version} (fake)'\n", encoding="utf-8"
    )
    stub.chmod(0o755)

    # workspace acceptance docs
    plan = ws / "install-plan.md"
    rollback = ws / "rollback-notes.md"
    plan.write_text(
        f"# Install plan (fake)\n\n"
        f"- package: `{package_name}` `{version}`\n"
        f"- install_root: `{root}`\n"
        f"- artifact: `{target}`\n"
        f"- network_allow: {extract_auth_fields(charter)['network_allow']}\n"
        f"- real system package managers: **BLOCKED**\n",
        encoding="utf-8",
    )
    rollback.write_text(
        f"# Rollback\n\n1. `rm -rf {target}`\n"
        f"2. Remove `{plan.name}` / `{rollback.name}` if desired.\n"
        f"3. No systemd/firewall/sudo changes were made.\n",
        encoding="utf-8",
    )

    return InstallResult(
        ok=True,
        status="installed",
        reason="fake package written under workdir-scoped install_roots",
        artifact_path=str(target),
        rollback_path=str(rollback),
        extras={"install_root": str(root), "meta": meta},
    )


def rollback_fake_package(artifact_path: str | Path, *, workdir: str | Path | None = None) -> InstallResult:
    path = Path(artifact_path)
    if workdir is not None:
        ws = Path(workdir).resolve()
        if not is_path_within(canonicalize(str(path)), canonicalize(str(ws))):
            return InstallResult(False, "denied", "rollback path outside workdir")
    if path.exists():
        shutil.rmtree(path)
    return InstallResult(True, "installed", f"removed {path}", artifact_path=str(path))


__all__ = [
    "DANGEROUS_REAL_INSTALL",
    "InstallResult",
    "install_fake_package",
    "rollback_fake_package",
    "skip_dangerous_real_install",
    "assert_auth_gate",
]
