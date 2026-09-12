"""Knife 9: two independent inprocess jobs isolated by workdir (not process).

Each job gets its own workdir. Distinct artifact filenames (iso-a.txt / iso-b.txt)
must not land in the peer workdir. Sharing a workspace path is not isolation even
if backend instances differ.

Knife 10 (workdir_claim): a second job on the same workdir is queued/blocked with
an occupancy record instead of racing writes. Isolation here still *requires*
distinct workdirs; claims are the conflict path, not a substitute for isolation.

Public ExecutionBackend API only. No TeleAgent HTTP. No Hermes ledger.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from charter import expected_artifacts, job_name, load_charter
from execution_backend.inprocess_v1 import InProcessExecutionBackend, run_file_job_via_public_api
from execution_backend.run_job_wire import run_inprocess_charter

REPO = Path(__file__).resolve().parents[2]
ISO_A_CHARTER = REPO / "jobs" / "examples" / "iso-a.charter.yaml"
ISO_B_CHARTER = REPO / "jobs" / "examples" / "iso-b.charter.yaml"
ARTIFACT_A = "iso-a.txt"
ARTIFACT_B = "iso-b.txt"


class IsolationError(ValueError):
    """Shared workspace path is not isolation."""


def artifact_basenames(charter: dict) -> list[str]:
    """Relative artifact names from done_when (ignore caller workspace)."""
    names: list[str] = []
    for raw in expected_artifacts(charter, workspace=None):
        p = Path(str(raw))
        names.append(p.name if p.is_absolute() else str(p))
    return names


def charter_artifact_rel(charter: dict) -> str:
    names = artifact_basenames(charter)
    if not names:
        raise ValueError(f"charter {job_name(charter)!r} has no artifacts")
    return names[0]


def require_distinct_workdirs(
    workdir_a: str | Path, workdir_b: str | Path
) -> tuple[Path, Path]:
    """Resolve and reject a shared workspace path.

    Different backend/process instances still collide if they write the same
    relative artifact under one root — that is not isolation.
    """
    a = Path(workdir_a).resolve()
    b = Path(workdir_b).resolve()
    if a == b:
        raise IsolationError(
            "shared workdir is not isolation; two jobs must not share a workspace path "
            f"(both={a})"
        )
    return a, b


def allocate_isolated_workdirs(
    root: str | Path,
    *,
    name_a: str = "iso-a",
    name_b: str = "iso-b",
) -> tuple[Path, Path]:
    """Two sibling workdirs under root. Refuses equal paths."""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    wa, wb = require_distinct_workdirs(base / name_a, base / name_b)
    wa.mkdir(parents=True, exist_ok=True)
    wb.mkdir(parents=True, exist_ok=True)
    return wa, wb


def check_workdir_isolation(
    *,
    workdir_a: str | Path,
    workdir_b: str | Path,
    artifact_a: str = ARTIFACT_A,
    artifact_b: str = ARTIFACT_B,
) -> dict[str, Any]:
    """Filesystem isolation: distinct workdirs, each unique artifact only in its own root."""
    wa = Path(workdir_a).resolve()
    wb = Path(workdir_b).resolve()
    rel_a = Path(artifact_a).name
    rel_b = Path(artifact_b).name
    path_a = wa / rel_a
    path_b = wb / rel_b
    a_in_b = (wb / rel_a).is_file() if rel_a != rel_b else False
    b_in_a = (wa / rel_b).is_file() if rel_a != rel_b else False
    distinct = wa != wb
    own_ok = path_a.is_file() and path_b.is_file()
    no_cross = (not a_in_b) and (not b_in_a)
    paths_distinct = (path_a.resolve() != path_b.resolve()) if (own_ok and distinct) else False
    ok = bool(distinct and own_ok and no_cross and paths_distinct)
    return {
        "isolation_ok": ok,
        "workdir_a": str(wa),
        "workdir_b": str(wb),
        "workdirs_distinct": distinct,
        "artifact_a": str(path_a),
        "artifact_b": str(path_b),
        "artifact_a_present": path_a.is_file(),
        "artifact_b_present": path_b.is_file(),
        "artifact_a_in_b": a_in_b,
        "artifact_b_in_a": b_in_a,
        "artifact_paths_distinct": paths_distinct,
    }


def run_one_inprocess_job(
    *,
    charter: dict,
    workdir: str | Path,
    name: str = "",
    via: str = "charter",
    backend: InProcessExecutionBackend | None = None,
) -> dict[str, Any]:
    """Run one charter via inprocess public API into ``workdir`` only."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    job = name or job_name(charter)
    be = backend or InProcessExecutionBackend()
    if via == "public_api":
        result = run_file_job_via_public_api(workdir=wd, charter=charter, backend=be)
    else:
        result = run_inprocess_charter(charter=charter, workdir=wd, name=job)
    names = artifact_basenames(charter)
    present = [n for n in names if (wd / n).is_file()]
    return {
        "ok": bool(result.get("ok")),
        "name": job,
        "workdir": str(wd.resolve()),
        "artifact_names": names,
        "artifacts_present": present,
        "used_public_api_only": bool(result.get("used_public_api_only")),
        "backend": result.get("backend") or "inprocess.local_v1",
        "result": result,
    }


def isolation_report(job_a: dict[str, Any], job_b: dict[str, Any]) -> dict[str, Any]:
    """Assert workdir_a != workdir_b and neither job's unique artifacts appear in the other."""
    wa = Path(job_a["workdir"]).resolve()
    wb = Path(job_b["workdir"]).resolve()
    a_names = set(job_a.get("artifact_names") or [])
    b_names = set(job_b.get("artifact_names") or [])
    # Shared filenames in *distinct* workdirs are fine; a name unique to A must
    # not exist in B (and vice versa). That is the collision we forbid.
    a_in_b = [n for n in sorted(a_names - b_names) if (wb / n).is_file()]
    b_in_a = [n for n in sorted(b_names - a_names) if (wa / n).is_file()]
    a_missing = [n for n in sorted(a_names) if n not in set(job_a.get("artifacts_present") or [])]
    b_missing = [n for n in sorted(b_names) if n not in set(job_b.get("artifacts_present") or [])]
    workdirs_distinct = wa != wb
    isolated = (
        bool(job_a.get("ok"))
        and bool(job_b.get("ok"))
        and workdirs_distinct
        and not a_in_b
        and not b_in_a
        and not a_missing
        and not b_missing
    )
    return {
        "ok": isolated,
        "isolation_ok": isolated,
        "workdirs_distinct": workdirs_distinct,
        "workdir_a": str(wa),
        "workdir_b": str(wb),
        "artifact_of_a_in_b": a_in_b,
        "artifact_of_b_in_a": b_in_a,
        "missing_in_a": a_missing,
        "missing_in_b": b_missing,
        "job_a": {
            "ok": job_a.get("ok"),
            "name": job_a.get("name"),
            "workdir": str(wa),
            "artifact_names": list(job_a.get("artifact_names") or []),
            "artifacts_present": list(job_a.get("artifacts_present") or []),
            "used_public_api_only": job_a.get("used_public_api_only"),
        },
        "job_b": {
            "ok": job_b.get("ok"),
            "name": job_b.get("name"),
            "workdir": str(wb),
            "artifact_names": list(job_b.get("artifact_names") or []),
            "artifacts_present": list(job_b.get("artifacts_present") or []),
            "used_public_api_only": job_b.get("used_public_api_only"),
        },
    }


def run_two_jobs_isolated(
    *,
    charter_a: dict | None = None,
    charter_b: dict | None = None,
    workdir_a: str | Path | None = None,
    workdir_b: str | Path | None = None,
    concurrent: bool = False,
    workspaces_root: str | Path | None = None,
    via: str = "charter",
) -> dict[str, Any]:
    """Run A and B with required-distinct workdirs (serial or threads).

    Isolation is the workdir pair — not the backend instance. A shared
    ``workdir_a == workdir_b`` raises ``IsolationError`` before any write.
    """
    ca = charter_a if charter_a is not None else load_charter(ISO_A_CHARTER)
    cb = charter_b if charter_b is not None else load_charter(ISO_B_CHARTER)

    if workdir_a is None or workdir_b is None:
        if workspaces_root is None:
            raise ValueError("workspaces_root or both workdir_a/workdir_b required")
        wa, wb = allocate_isolated_workdirs(
            workspaces_root,
            name_a=job_name(ca),
            name_b=job_name(cb),
        )
    else:
        wa, wb = require_distinct_workdirs(workdir_a, workdir_b)
        wa.mkdir(parents=True, exist_ok=True)
        wb.mkdir(parents=True, exist_ok=True)

    def _run_a() -> dict[str, Any]:
        return run_one_inprocess_job(charter=ca, workdir=wa, via=via)

    def _run_b() -> dict[str, Any]:
        return run_one_inprocess_job(charter=cb, workdir=wb, via=via)

    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(_run_a)
            fb = pool.submit(_run_b)
            errors: list[BaseException] = []
            for fut in as_completed([fa, fb]):
                try:
                    fut.result()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
            if errors:
                raise errors[0]
            job_a, job_b = fa.result(), fb.result()
    else:
        job_a = _run_a()
        job_b = _run_b()

    report = isolation_report(job_a, job_b)
    report["concurrent"] = bool(concurrent)
    report["serial"] = not concurrent
    report["isolation_by"] = "workdir"
    report["via"] = via
    report["used_public_api_only"] = bool(
        job_a.get("used_public_api_only") and job_b.get("used_public_api_only")
    )
    report["artifact_a"] = charter_artifact_rel(ca) if artifact_basenames(ca) else ""
    report["artifact_b"] = charter_artifact_rel(cb) if artifact_basenames(cb) else ""
    report["result_a"] = job_a
    report["result_b"] = job_b
    report["mode"] = "concurrent" if concurrent else "serial"
    report["jobs_ok"] = bool(job_a.get("ok") and job_b.get("ok"))
    report["check"] = check_workdir_isolation(
        workdir_a=wa,
        workdir_b=wb,
        artifact_a=report["artifact_a"] or ARTIFACT_A,
        artifact_b=report["artifact_b"] or ARTIFACT_B,
    )
    return report


def prove_shared_workspace_collides(
    *,
    workdir: str | Path,
    relative: str = "shared.txt",
) -> dict[str, Any]:
    """Two backend *instances*, one workspace: same relative path is a collision.

    This is the counterexample: process/instance isolation without workdir
    isolation is not isolation.
    """
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / relative
    if target.exists():
        target.unlink()
    be_a = InProcessExecutionBackend()
    be_b = InProcessExecutionBackend()
    started_a = be_a.start_run(
        title="instance-a",
        directory=str(root),
        artifacts=[relative],
    )
    body_after_a = target.read_text(encoding="utf-8") if target.is_file() else ""
    started_b = be_b.start_run(
        title="instance-b",
        directory=str(root),
        artifacts=[relative],
    )
    body_after_b = target.read_text(encoding="utf-8") if target.is_file() else ""
    collided = target.is_file() and body_after_a != body_after_b
    return {
        "ok": collided,
        "isolation_ok": False,
        "shared_workdir": str(root.resolve()),
        "relative": relative,
        "same_relative_path": relative,
        "instance_a_id": id(be_a),
        "instance_b_id": id(be_b),
        "instances_distinct": be_a is not be_b,
        "backend_instances_distinct": be_a is not be_b,
        "run_id_a": started_a.get("run_id"),
        "run_id_b": started_b.get("run_id"),
        "run_ids_distinct": started_a.get("run_id") != started_b.get("run_id"),
        "body_after_a": body_after_a,
        "body_after_b": body_after_b,
        "overwritten": collided,
        "collided": collided,
        "single_file_remains": target.is_file(),
        "started_a": started_a,
        "started_b": started_b,
        "note": "distinct backend instances still share the file; isolation requires distinct workdirs",
        "reason": "distinct backend instances still share the workspace path",
    }


def prove_shared_workdir_is_not_isolation(
    shared_root: str | Path,
    relative: str = "shared.txt",
) -> dict[str, Any]:
    """Positional alias used by convenience tests."""
    return prove_shared_workspace_collides(workdir=shared_root, relative=relative)


def run_two_jobs_claimed(
    *,
    charter_a: dict | None = None,
    charter_b: dict | None = None,
    workdir_a: str | Path | None = None,
    workdir_b: str | Path | None = None,
    concurrent: bool = True,
    on_conflict: str = "block",
    **kwargs: Any,
) -> dict[str, Any]:
    """Knife 10: same workdir → second queued/blocked; distinct → parallel.

    Unlike ``run_two_jobs_isolated``, a shared workdir does not raise
    IsolationError — the claim registry refuses the second writer.
    """
    from execution_backend.workdir_claim import run_two_jobs_with_claims

    ca = charter_a if charter_a is not None else load_charter(ISO_A_CHARTER)
    cb = charter_b if charter_b is not None else load_charter(ISO_B_CHARTER)
    if workdir_a is None or workdir_b is None:
        raise ValueError("workdir_a and workdir_b are required")
    return run_two_jobs_with_claims(
        charter_a=ca,
        charter_b=cb,
        workdir_a=workdir_a,
        workdir_b=workdir_b,
        concurrent=concurrent,
        on_conflict=on_conflict,
        **kwargs,
    )
