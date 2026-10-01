"""Render a worker contract into a prompt section.

Prompt text is not an OS sandbox. The rendered section states what the worker
was authorized to do; it does not confine the process, filesystem, or network.
Enforcement stays in the execution backend and host policy. A missing or
invalid contract field is an error, not a cue to start a degraded run.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

# Fields that belong in the prompt contract (not task-auth or runtime extras).
CONTRACT_FIELDS = (
    "must",
    "must_not",
    "forbidden_tools",
    "input_files",
    "external_inputs",
    "done_when",
    "acceptance",
)
_LIST_FIELDS = ("must", "must_not", "forbidden_tools", "input_files")
_TYPED_FIELDS = frozenset(
    {"goal", "instruction", "acceptance", "done_when", "external_inputs", *_LIST_FIELDS}
)
# Ignored on an acceptance object. Unknown keys are rejected, not dropped.
_ACCEPTANCE_SAFE_KEYS = frozenset({"text", "artifacts", "allow_aigc_marks"})
_DONE_WHEN_KEYS = frozenset({"artifacts", "text"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ContractRenderError(ValueError):
    """The contract could not be rendered without dropping a constraint."""

    code = "contract_render_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "contract_render_error"


def safe_relative_artifact(raw: str) -> str:
    """Lexical relative path, or raise.

    ``..``, absolute paths, drive letters (``C:``), and backslash traversal
    (``..\\\\x``) are rejected on every platform, including Linux where
    ``pathlib`` does not treat ``\\\\`` as a separator. Empty names are
    rejected. This check is not an OS sandbox and does not touch the filesystem.
    """
    if not isinstance(raw, str):
        raise ContractRenderError(f"artifact path must be a string, got {type(raw).__name__}")
    text = raw.strip()
    if not text or "\x00" in text:
        raise ContractRenderError(f"artifact path is empty: {raw!r}")
    unified = text.replace("\\", "/")
    if unified.startswith("/") or unified.startswith("//"):
        raise ContractRenderError(f"artifact path must stay inside the workspace: {raw!r}")
    if _DRIVE_RE.match(unified):
        raise ContractRenderError(f"artifact path must not use a drive letter: {raw!r}")
    parts: list[str] = []
    for seg in unified.split("/"):
        if seg == "":
            raise ContractRenderError(f"artifact path has an empty name: {raw!r}")
        if seg == ".":
            continue
        if seg == "..":
            raise ContractRenderError(f"artifact path must not escape the workspace: {raw!r}")
        if _DRIVE_RE.match(seg):
            raise ContractRenderError(f"artifact path must not use a drive letter: {raw!r}")
        parts.append(seg)
    if not parts or not parts[-1]:
        raise ContractRenderError(f"artifact path has an empty name: {raw!r}")
    return "/".join(parts)


def _as_str_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        if not value.strip():
            raise ContractRenderError(f"{field} string is empty")
        return [value.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ContractRenderError(f"{field}[{index}] must be a string")
            if not item.strip():
                raise ContractRenderError(f"{field}[{index}] is empty")
            out.append(item.strip())
        return out
    raise ContractRenderError(f"{field} must be a list of strings")


def _normalize_acceptance(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Mapping):
        unknown = sorted(str(key) for key in value.keys() if key not in _ACCEPTANCE_SAFE_KEYS)
        if unknown:
            raise ContractRenderError(
                "acceptance has unknown keys: " + ", ".join(unknown)
            )
        if "text" not in value:
            return None
        text = value.get("text")
        if not isinstance(text, str):
            raise ContractRenderError("acceptance.text must be a string")
        stripped = text.strip()
        return stripped or None
    raise ContractRenderError("acceptance must be a string or an object with text")


def _normalize_done_when(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ContractRenderError("done_when string is empty")
        return {"text": text}
    if isinstance(value, list):
        return {"artifacts": [safe_relative_artifact(item) for item in _as_str_list(value, "done_when")]}
    if isinstance(value, Mapping):
        unknown = sorted(str(key) for key in value.keys() if key not in _DONE_WHEN_KEYS)
        if unknown:
            raise ContractRenderError("done_when has unknown keys: " + ", ".join(unknown))
        out: dict[str, Any] = {}
        if "artifacts" in value:
            arts = value.get("artifacts")
            if isinstance(arts, str):
                arts = [arts]
            if not isinstance(arts, list):
                raise ContractRenderError("done_when.artifacts must be a list of strings")
            out["artifacts"] = [
                safe_relative_artifact(item) for item in _as_str_list(arts, "done_when.artifacts")
            ]
        if "text" in value:
            text = value.get("text")
            if not isinstance(text, str):
                raise ContractRenderError("done_when.text must be a string")
            out["text"] = text.strip()
        return out
    raise ContractRenderError("done_when must be a mapping, string, or list of strings")


def _normalize_external_inputs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ContractRenderError("external_inputs must be a list")
    out: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractRenderError(f"external_inputs[{index}] must be an object")
        extra = sorted(str(key) for key in item.keys() if key not in {"path", "sha256"})
        if extra:
            raise ContractRenderError(
                f"external_inputs[{index}] has unknown keys: " + ", ".join(extra)
            )
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not path.strip():
            raise ContractRenderError(f"external_inputs[{index}].path must be a string")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest.strip()) is None:
            raise ContractRenderError(
                f"external_inputs[{index}].sha256 must be 64 hex characters"
            )
        out.append({"path": path.strip(), "sha256": digest.strip().lower()})
    return out


def normalize_worker_contract(charter: Any) -> dict[str, Any]:
    """Strict contract copy. Unknown keys are kept unchanged.

    ``goal`` / ``instruction`` must be strings. ``must``, ``must_not``,
    ``forbidden_tools``, and ``input_files`` are string lists (a single string
    becomes a one-item list). ``acceptance`` is a string or a mapping whose
    ``text`` is a string. ``done_when`` becomes a mapping. ``external_inputs``
    is a list of ``{path, sha256}``. Any other type raises
    ``ContractRenderError`` instead of being omitted.
    """
    if charter is None:
        return {}
    if not isinstance(charter, Mapping):
        raise ContractRenderError("worker contract must be an object")
    out: dict[str, Any] = {}
    for key, value in charter.items():
        if not isinstance(key, str):
            raise ContractRenderError("contract keys must be strings")
        if key not in _TYPED_FIELDS:
            out[key] = value
            continue
        if key in {"goal", "instruction"}:
            if value is None:
                continue
            if not isinstance(value, str):
                raise ContractRenderError(f"{key} must be a string")
            out[key] = value.strip()
            continue
        if key in _LIST_FIELDS:
            if value is None:
                out[key] = []
                continue
            out[key] = _as_str_list(value, key)
            continue
        if key == "acceptance":
            if value is None:
                continue
            text = _normalize_acceptance(value)
            if text:
                out["acceptance"] = text
            continue
        if key == "done_when":
            if value is None:
                continue
            out["done_when"] = _normalize_done_when(value)
            continue
        if key == "external_inputs":
            if value is None:
                out["external_inputs"] = []
                continue
            out["external_inputs"] = _normalize_external_inputs(value)
            continue
    return out


def _reject_corrupt(normalized: Mapping[str, Any]) -> None:
    """Raise if a contract field is present but not in normalized form.

    Treating a bad value as absent would drop ``must_not`` and start a
    degraded run. That is the failure this module exists to prevent.
    """
    for field in _LIST_FIELDS:
        if field not in normalized:
            continue
        value = normalized[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ContractRenderError(f"{field} must be a list of strings")
    if "acceptance" in normalized and not isinstance(normalized.get("acceptance"), str):
        raise ContractRenderError("acceptance must be a string after normalize")
    if "done_when" in normalized and not isinstance(normalized.get("done_when"), Mapping):
        raise ContractRenderError("done_when must be a mapping after normalize")
    if "external_inputs" in normalized:
        value = normalized.get("external_inputs")
        if not isinstance(value, list):
            raise ContractRenderError("external_inputs must be a list")
        for item in value:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                raise ContractRenderError("external_inputs entries must carry a path")
            if not isinstance(item.get("sha256"), str):
                raise ContractRenderError("external_inputs entries must carry sha256")


def _done_when_active(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    arts = value.get("artifacts") or []
    text = value.get("text") or ""
    has_arts = isinstance(arts, list) and any(str(item).strip() for item in arts)
    has_text = isinstance(text, str) and bool(text.strip())
    return has_arts or has_text


def contract_fields_present(normalized: Mapping[str, Any] | None) -> list[str]:
    """Contract field names that will actually be rendered, in stable order."""
    if not isinstance(normalized, Mapping):
        raise ContractRenderError("normalized contract must be an object")
    _reject_corrupt(normalized)
    present: list[str] = []
    for field in CONTRACT_FIELDS:
        value = normalized.get(field)
        if field in _LIST_FIELDS or field == "external_inputs":
            if isinstance(value, list) and value:
                present.append(field)
        elif field == "acceptance":
            if isinstance(value, str) and value.strip():
                present.append(field)
        elif field == "done_when":
            if _done_when_active(value):
                present.append(field)
    return present


def contract_fingerprint(normalized: Mapping[str, Any] | None) -> str:
    """Full SHA-256 hex of the canonical JSON of the rendered contract fields."""
    fields = contract_fields_present(normalized)
    body = {field: normalized[field] for field in fields}  # type: ignore[index]
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _bullets(heading: str, items: list[str]) -> str:
    lines = [heading]
    for item in items:
        lines.append(f"- {item}")
    return "\n".join(lines)


def render_contract_section(normalized: Mapping[str, Any] | None) -> str:
    """Render contract fields under stable headings.

    Returns an empty string only when no contract field is present. Prompt
    text is not an OS sandbox: headings do not grant or revoke OS rights.
    A corrupt field raises ``ContractRenderError`` and must not be skipped.
    """
    if normalized is None:
        return ""
    fields = contract_fields_present(normalized)
    if not fields:
        return ""
    blocks: list[str] = []
    if "must" in fields:
        blocks.append(_bullets("Must:", list(normalized["must"])))
    if "must_not" in fields:
        blocks.append(_bullets("Must not:", list(normalized["must_not"])))
    if "forbidden_tools" in fields:
        blocks.append(_bullets("Forbidden tools:", list(normalized["forbidden_tools"])))
    if "input_files" in fields:
        blocks.append(_bullets("Input files (in workspace):", list(normalized["input_files"])))
    if "external_inputs" in fields:
        lines = [
            "Pinned external inputs (read-only; verify SHA-256; no other outside paths):"
        ]
        for item in normalized["external_inputs"]:
            lines.append(f"- {item['path']}")
            lines.append(f"  sha256: {item['sha256']}")
        blocks.append("\n".join(lines))
    if "done_when" in fields:
        done = normalized["done_when"]
        lines = ["Done when:"]
        arts = done.get("artifacts") or []
        if isinstance(arts, list) and arts:
            lines.append("artifacts:")
            for art in arts:
                lines.append(f"- {art}")
        text = done.get("text")
        if isinstance(text, str) and text.strip():
            lines.append(f"text: {text.strip()}")
        blocks.append("\n".join(lines))
    if "acceptance" in fields:
        blocks.append("Acceptance:\n" + str(normalized["acceptance"]).strip())
    return "\n\n".join(blocks)
