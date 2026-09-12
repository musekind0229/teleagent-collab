"""Task.depends_on refs: task_id, charter/job name, or artifact.

Unsatisfied deps stay queued. Artifact refs check the filesystem;
task/charter refs need a completed-job index (scheduler graph).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

SUCCEEDED = frozenset({"ok", "succeeded", "done", "success"})


class DepRef:
    """Normalized dependency reference."""

    __slots__ = ("kind", "value", "raw")

    def __init__(self, kind: str, value: str, raw: str) -> None:
        self.kind = kind  # task | charter | artifact
        self.value = value
        self.raw = raw

    def __repr__(self) -> str:
        return f"DepRef({self.kind}:{self.value})"

    def as_str(self) -> str:
        if self.kind == "charter":
            return self.value
        return f"{self.kind}:{self.value}"


def _strip(s: Any) -> str:
    return str(s or "").strip()


def parse_one_dep(item: Any) -> DepRef | None:
    """Parse one depends_on entry (string or {artifact|task|charter|job: val})."""
    if item is None:
        return None
    if isinstance(item, DepRef):
        return item
    if isinstance(item, Mapping):
        for key, kind in (
            ("artifact", "artifact"),
            ("task", "task"),
            ("task_id", "task"),
            ("charter", "charter"),
            ("job", "charter"),
            ("name", "charter"),
        ):
            if key in item and _strip(item.get(key)):
                return DepRef(kind, _strip(item.get(key)), f"{kind}:{_strip(item.get(key))}")
        # single-key fallback: {producer: true} is not a dep
        return None
    raw = _strip(item)
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith("artifact:"):
        return DepRef("artifact", raw.split(":", 1)[1].strip(), raw)
    if lower.startswith("task:"):
        return DepRef("task", raw.split(":", 1)[1].strip(), raw)
    if lower.startswith("charter:") or lower.startswith("job:"):
        return DepRef("charter", raw.split(":", 1)[1].strip(), raw)
    if raw.startswith("task_"):
        return DepRef("task", raw, raw)
    # path-like → artifact
    p = Path(raw)
    if "/" in raw or "\\" in raw or p.suffix:
        return DepRef("artifact", raw, raw)
    return DepRef("charter", raw, raw)


def parse_depends_on(raw: Any) -> list[DepRef]:
    if raw is None:
        return []
    if isinstance(raw, (str, Mapping, DepRef)):
        items: Iterable[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        return []
    out: list[DepRef] = []
    seen: set[str] = set()
    for item in items:
        ref = parse_one_dep(item)
        if ref is None:
            continue
        key = ref.as_str()
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def depends_on_strings(raw: Any) -> list[str]:
    """Contract Task.depends_on is an array of strings."""
    return [r.as_str() for r in parse_depends_on(raw)]


def _result_succeeded(result: Any) -> bool:
    if result is True:
        return True
    if not isinstance(result, Mapping):
        return False
    if result.get("ok") is True:
        return True
    st = str(result.get("state") or result.get("task_state") or "").strip().lower()
    return st in SUCCEEDED


def _index_completed(completed: Mapping[str, Any] | None) -> dict[str, Any]:
    if not completed:
        return {}
    idx: dict[str, Any] = {}
    for k, v in completed.items():
        key = _strip(k)
        if key:
            idx[key] = v
            idx[key.lower()] = v
        if isinstance(v, Mapping):
            for alt in (
                v.get("name"),
                v.get("job_id"),
                v.get("task_id"),
                v.get("charter_name"),
            ):
                a = _strip(alt)
                if a:
                    idx[a] = v
                    idx[a.lower()] = v
    return idx


def unsatisfied_deps(
    depends_on: Any,
    *,
    completed: Mapping[str, Any] | None = None,
    workdir: str | Path | None = None,
    extra_artifact_roots: Iterable[str | Path] | None = None,
    enforce_named_deps: bool | None = None,
) -> list[str]:
    """Return raw refs that are not yet satisfied.

    ``completed is None`` (standalone job): only artifact refs are enforced;
    charter/task names are recorded as notes but not blocking. Pass a dict
    (even empty) when a scheduler graph is in play so named deps block.
    """
    refs = parse_depends_on(depends_on)
    if not refs:
        return []
    named = enforce_named_deps
    if named is None:
        named = completed is not None
    idx = _index_completed(completed)
    roots: list[Path] = []
    if workdir is not None:
        roots.append(Path(workdir))
    for r in extra_artifact_roots or []:
        roots.append(Path(r))

    missing: list[str] = []
    for ref in refs:
        if ref.kind == "artifact":
            found = False
            rel = Path(ref.value)
            if rel.is_absolute() and rel.is_file():
                found = True
            else:
                for root in roots:
                    cand = root / ref.value
                    if cand.is_file():
                        found = True
                        break
                    if (root / Path(ref.value).name).is_file():
                        found = True
                        break
            if not found:
                missing.append(ref.as_str())
            continue
        if not named:
            continue
        hit = idx.get(ref.value) or idx.get(ref.value.lower())
        if not _result_succeeded(hit):
            missing.append(ref.as_str())
    return missing


def deps_satisfied(
    depends_on: Any,
    *,
    completed: Mapping[str, Any] | None = None,
    workdir: str | Path | None = None,
    extra_artifact_roots: Iterable[str | Path] | None = None,
    enforce_named_deps: bool | None = None,
) -> bool:
    return not unsatisfied_deps(
        depends_on,
        completed=completed,
        workdir=workdir,
        extra_artifact_roots=extra_artifact_roots,
        enforce_named_deps=enforce_named_deps,
    )


def charter_depends_on(charter: Mapping[str, Any] | None) -> list[DepRef]:
    if not isinstance(charter, Mapping):
        return []
    raw = charter.get("depends_on")
    if raw is None and isinstance(charter.get("inputs"), Mapping):
        raw = charter["inputs"].get("depends_on")
    return parse_depends_on(raw)
