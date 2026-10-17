#!/usr/bin/env python3
"""
chat_local_report.py - Local read-only chat indexing and reporting tool.

Standard library only. Designed for privacy-preserving local extraction from
explicitly manifest-authorized files.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, List, Optional, Set, Tuple


# Regex patterns for credential screening
RE_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)
RE_AUTH_HEADER = re.compile(r"(?:authorization|bearer|token)\s*:\s*(?:bearer\s+)?[a-z0-9_\-\.]{12,}", re.IGNORECASE)
RE_SECRET_ASSIGN = re.compile(
    r"(?:password|passwd|secret|api_key|apikey|access_token|client_secret|auth_token)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-\.]{8,}['\"]?",
    re.IGNORECASE,
)
RE_GENERIC_KEY = re.compile(r"(?:ghp_[a-zA-Z0-9]{36}|xox[baprs]-[0-9a-zA-Z]{10,48}|sk-[a-zA-Z0-9]{20,})")


def is_credential_text(text: str) -> bool:
    """Check if a text contains obvious credential-like signatures."""
    if not text:
        return False
    if RE_PEM_BLOCK.search(text):
        return True
    if RE_AUTH_HEADER.search(text):
        return True
    if RE_SECRET_ASSIGN.search(text):
        return True
    if RE_GENERIC_KEY.search(text):
        return True
    return False


def is_file_credential_mixed(lines: List[str]) -> bool:
    """
    Check if a file appears mixed with credentials (e.g. PEM blocks, secret assignments across lines, etc.).
    If the file has credential mixtures, whole file is excluded from extraction.
    """
    secret_count = 0
    for line in lines:
        if RE_PEM_BLOCK.search(line):
            return True
        if RE_AUTH_HEADER.search(line):
            secret_count += 1
        if RE_SECRET_ASSIGN.search(line):
            secret_count += 1
        if RE_GENERIC_KEY.search(line):
            secret_count += 1
        # If multiple lines or a PEM block contain credentials, treat whole file as credential-mixed
        if secret_count >= 2:
            return True
    return False


# Dialogue analysis patterns (multilingual / Chinese + English)
RE_FIRST_PERSON = re.compile(r"(?:^|[\s,。！？!?，])(?:我|我们|本人|I|me|my|mine|we|us|our|ours)\b", re.IGNORECASE)
RE_THIRD_PERSON = re.compile(
    r"(?:^|[\s,。！？!?，])(?:他|她|它|他们|她们|它们|朋友|同事|别人|有人|用户|he|she|it|they|them|his|her|their|friend|colleague|user)\b",
    re.IGNORECASE,
)
RE_NEGATION = re.compile(r"(?:不|没|无|未|别|非|不再|not|no|never|neither|nor|without|stop|cease)", re.IGNORECASE)
RE_TIME_QUAL = re.compile(
    r"(?:以前|过去|曾经|现在|目前|如今|之后|将来|以后|从前|now|currently|past|previously|used to|formerly|in the past|future)",
    re.IGNORECASE,
)
RE_QUESTION_OR_REQ = re.compile(
    r"(?:请|能否|可以吗|怎么|什么|为什么|如何|？|\?|please|could you|would you|how to|what|why|help me|can you)",
    re.IGNORECASE,
)
RE_GENERATED_PREFIX = re.compile(
    r"^\[(?:CONTEXT COMPACTION|SYSTEM|MEMORY|GENERATED|INSTRUCTION|SUMMARY)\]", re.IGNORECASE
)


def analyze_dialogue_record(
    raw_text: str,
    role: str,
    event_time_raw: str,
    source_locator: str,
    source_line: int,
    known_truncated: bool = False,
    override_expected: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyze dialogue and produce the 14 schema fields.
    """
    is_sensitive = is_credential_text(raw_text)
    if is_sensitive:
        quote_verbatim = "[REDACTED_SENSITIVE]"
        minimal_context = "[REDACTED_SENSITIVE]"
    else:
        quote_verbatim = raw_text
        # minimal context: trimmed version up to 80 chars
        minimal_context = raw_text.strip()
        if len(minimal_context) > 80:
            minimal_context = minimal_context[:77] + "..."

    # Check origin
    role_lower = (role or "").strip().lower()
    is_generated = bool(RE_GENERATED_PREFIX.search(raw_text))
    is_user_role = role_lower == "user" and not is_generated

    # Check truncation
    truncated = known_truncated or raw_text.endswith("...") or raw_text.endswith("…")

    # Subject classification
    has_first = bool(RE_FIRST_PERSON.search(raw_text)) or "我" in raw_text
    has_third = bool(RE_THIRD_PERSON.search(raw_text)) or any(
        w in raw_text for w in ["朋友", "他", "她", "它们", "他们", "别人", "用户"]
    )

    if has_first and has_third:
        subject = "mixed-subject"
    elif has_third and not has_first:
        subject = "third-party"
    elif has_first:
        subject = "first-person"
    else:
        subject = "unspecified"

    # Negation
    negation = bool(RE_NEGATION.search(raw_text))

    # Time qualification
    has_time_qual = bool(RE_TIME_QUAL.search(raw_text))
    if has_time_qual:
        if any(w in raw_text for w in ["以前", "过去", "曾经", "previously", "used to"]):
            time_qualification = "historical"
        elif any(w in raw_text for w in ["现在", "目前", "如今", "now", "currently"]):
            time_qualification = "current"
        else:
            time_qualification = "qualified"
    else:
        time_qualification = "unspecified"

    # Contradiction review flag
    # If the text itself contains conflicting states (e.g. past vs now with negation), or marked
    contradiction_review = False
    if "以前" in raw_text and "现在" in raw_text and negation:
        contradiction_review = True

    # Confidence and Origin
    if not is_user_role:
        if role_lower == "tool":
            confidence = "origin_tool"
            summary = f"Tool output: {minimal_context}"
        elif role_lower == "assistant":
            confidence = "origin_assistant"
            summary = f"Assistant dialogue: {minimal_context}"
        else:
            confidence = "origin_non_user"
            summary = f"Non-user/generated text: {minimal_context}"
    elif truncated:
        confidence = "low_truncated"
        summary = f"Truncated user dialogue fragment: {minimal_context}"
    elif is_sensitive:
        confidence = "sensitive_suppressed"
        summary = "Sensitive content suppressed"
    else:
        if RE_QUESTION_OR_REQ.search(raw_text):
            confidence = "user_request_or_query"
            summary = f"User request/query: {minimal_context}"
        else:
            confidence = "user_dialogue_statement"
            summary = f"User statement: {minimal_context}"

    # Topic derivation (simple local rule inference)
    topic = "general"
    if "颜色" in raw_text or any(c in raw_text for c in ["绿", "红", "蓝", "黑", "白"]):
        topic = "color_preference"
    elif "工具" in raw_text or "tool" in raw_text.lower():
        topic = "tool_usage"
    elif "工作" in raw_text or "回答" in raw_text:
        topic = "workflow_preference"
    elif "住" in raw_text or "住址" in raw_text or "城" in raw_text:
        topic = "residence"
    elif "规则" in raw_text or "上传" in raw_text or "账号" in raw_text:
        topic = "prompt_instruction"
    elif is_sensitive:
        topic = "credential_sensitive"

    record: Dict[str, Any] = {
        "topic": topic,
        "summary_local_rule_inference": summary,
        "quote_verbatim": quote_verbatim,
        "minimal_context": minimal_context,
        "source_locator_LOCAL_ONLY": source_locator,
        "source_line": source_line,
        "event_time_raw": event_time_raw,
        "subject": subject,
        "negation": negation,
        "time_qualification": time_qualification,
        "contradiction_review": contradiction_review,
        "confidence": confidence,
        "truncated": truncated,
        "sensitive": is_sensitive,
    }
    return record


FORBIDDEN_FILE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".wal"}
FORBIDDEN_FILE_SUFFIXES = ("-wal", "-shm")
ALLOWED_FILE_EXTENSIONS = {".json", ".jsonl"}


def _is_symlink_or_reparse(p: Path) -> bool:
    try:
        st = os.lstat(p)
        if os.path.islink(p):
            return True
        attr = getattr(st, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attr & reparse:
            return True
    except (FileNotFoundError, OSError):
        pass
    return False


def _validate_lexical_path_string(s: str) -> None:
    if not isinstance(s, str) or not s.strip():
        raise ValueError("Manifest path entry must be a non-empty string")
    clean = s.strip()
    if clean.startswith("\\\\") or clean.startswith("//"):
        raise ValueError("Network UNC path form is not permitted")
    p = Path(clean)
    if ".." in p.parts:
        raise ValueError("Lexical .. parent traversal is not permitted")


def _inspect_all_ancestors_no_follow(p: Path) -> None:
    ancestors = [p] + list(p.parents)
    for curr in reversed(ancestors):
        if _is_symlink_or_reparse(curr):
            raise ValueError(f"Symlink, junction, or reparse point rejected in path or ancestor: {p.name}")


def parse_manifest(manifest_path: str | Path) -> Tuple[List[Path], List[Path]]:
    """
    Parse and validate the manifest file.
    Manifest JSON shape:
    {"files": ["..."], "allowlisted_roots": ["..."]}
    Returns (authorized_files, allowlisted_roots).
    """
    _validate_lexical_path_string(str(manifest_path))
    manifest_p_orig = Path(manifest_path) if not isinstance(manifest_path, Path) else manifest_path
    _inspect_all_ancestors_no_follow(manifest_p_orig)

    if not manifest_p_orig.is_file():
        raise ValueError(f"Manifest path does not exist or is not a file: {manifest_p_orig}")

    with open(manifest_p_orig, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("Manifest content must be a JSON object")

    file_entries = data.get("files", [])
    if not isinstance(file_entries, list):
        raise ValueError("Manifest 'files' must be a list")

    root_entries = data.get("allowlisted_roots", [])
    if not isinstance(root_entries, list):
        raise ValueError("Manifest 'allowlisted_roots' must be a list")

    if not root_entries:
        raise ValueError("Manifest 'allowlisted_roots' must be non-empty")

    manifest_dir = manifest_p_orig.parent.resolve()

    # Validate and inspect allowlisted roots
    allowlisted_roots: List[Path] = []
    for r in root_entries:
        _validate_lexical_path_string(r)
        rp_orig = Path(r)
        if not rp_orig.is_absolute():
            rp_orig = (manifest_dir / rp_orig)
        _inspect_all_ancestors_no_follow(rp_orig)
        rp = rp_orig.resolve()
        if not rp.exists() or not rp.is_dir():
            raise ValueError(f"Allowlisted root must be an existing directory: {r}")
        allowlisted_roots.append(rp)

    # Validate each listed file BEFORE resolve / open
    authorized_files: List[Path] = []
    for f_str in file_entries:
        _validate_lexical_path_string(f_str)

        f_raw = Path(f_str)
        # Check extensions and suffixes BEFORE opening target data
        f_name_lower = f_raw.name.lower()
        ext_lower = f_raw.suffix.lower()

        if ext_lower in FORBIDDEN_FILE_EXTENSIONS or any(f_name_lower.endswith(sfx) for sfx in FORBIDDEN_FILE_SUFFIXES):
            raise ValueError(f"File suffix is forbidden: {f_raw.name}")

        if ext_lower not in ALLOWED_FILE_EXTENSIONS:
            raise ValueError(f"File suffix must be .json or .jsonl: {f_raw.name}")

        fp_orig = f_raw
        if not fp_orig.is_absolute():
            fp_orig = manifest_dir / fp_orig

        # Inspect every original path ancestor with lstat/no-follow BEFORE resolve/open
        _inspect_all_ancestors_no_follow(fp_orig)

        # Check ordinary file status
        if not fp_orig.exists() or not fp_orig.is_file():
            raise ValueError(f"Manifest listed path is not an ordinary existing file: {f_raw.name}")

        # Canonicalize and perform root containment
        fp = fp_orig.resolve()

        is_under_root = False
        for root in allowlisted_roots:
            try:
                fp.relative_to(root)
                is_under_root = True
                break
            except ValueError:
                continue

        if not is_under_root:
            raise PermissionError(f"Path escapes allowlisted roots: {f_raw.name}")

        authorized_files.append(fp)

    return authorized_files, allowlisted_roots


def extract_records_from_file(file_path: Path) -> Tuple[List[Dict[str, Any]], int, bool]:
    """
    Extract records from an authorized file.
    Returns (records, malformed_skip_count, file_excluded_due_to_credentials).
    """
    # Read file as text only
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return [], 1, False

    # Screen file for credential mixtures
    if is_file_credential_mixed(lines):
        return [], 0, True

    records: List[Dict[str, Any]] = []
    malformed_count = 0
    file_str = str(file_path)

    # First attempt: is it a synthetic normalized JSON list?
    full_content = "".join(lines).strip()
    if full_content.startswith("[") and full_content.endswith("]"):
        try:
            items = json.loads(full_content)
            if isinstance(items, list):
                for idx, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        malformed_count += 1
                        continue
                    role = item.get("role", "unknown")
                    text = item.get("text", "")
                    event_time_raw = item.get("time", "")
                    truncated = bool(item.get("truncated", False))
                    expected = item.get("expected")
                    rec = analyze_dialogue_record(
                        raw_text=text,
                        role=role,
                        event_time_raw=event_time_raw,
                        source_locator=file_str,
                        source_line=idx,
                        known_truncated=truncated,
                        override_expected=expected,
                    )
                    records.append(rec)
                return records, malformed_count, False
        except Exception:
            # Fall through to line-by-line parsing
            pass

    # Line by line parsing (JSONL Claude / Codex / other)
    for line_idx, line in enumerate(lines, start=1):
        line_clean = line.strip()
        if not line_clean:
            continue
        try:
            data = json.loads(line_clean)
        except Exception:
            malformed_count += 1
            continue

        if not isinstance(data, dict):
            malformed_count += 1
            continue

        # Check Claude ordinary JSONL schema:
        # e.g. {"message": {"role": "user", "content": "..."}} or {"role": "user", "content": "..."}
        if "message" in data and isinstance(data["message"], dict):
            msg = data["message"]
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            raw_text = ""
            if isinstance(content, str):
                raw_text = content
            elif isinstance(content, list):
                # list of content blocks: only explicit type text/input_text blocks with string text are human dialogue text
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        b_type = b.get("type")
                        b_text = b.get("text")
                        if b_type in ("text", "input_text") and isinstance(b_text, str):
                            parts.append(b_text)
                raw_text = "\n".join(parts)
            if not raw_text:
                malformed_count += 1
                continue
            event_time_raw = data.get("timestamp") or data.get("created_at") or msg.get("timestamp") or ""
            rec = analyze_dialogue_record(
                raw_text=raw_text,
                role=role,
                event_time_raw=str(event_time_raw),
                source_locator=file_str,
                source_line=line_idx,
            )
            records.append(rec)
            continue

        # Check actual Codex records:
        # e.g. {"type":"event_msg","payload":{"type":"user_message","message":"I prefer synthetic blue."},"timestamp":"2001-01-01T00:00:00Z"}
        # or {"type":"response_item","payload":{"role":"user","content":[{"type":"input_text","text":"I use synthetic tool A."}]}}
        # as well as legacy/synthetic Codex:
        # {"event_msg": "user_message", ...} or {"response_item": ...}
        if (
            data.get("type") in ("event_msg", "response_item")
            or "event_msg" in data
            or "response_item" in data
        ):
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            data_type = data.get("type")

            # Determine role: do not infer human identity from role
            role = data.get("role") or payload.get("role")
            if not role:
                if (
                    data.get("event_msg") == "user_message"
                    or payload.get("type") == "user_message"
                ):
                    role = "user"
                elif "response_item" in data or data_type == "response_item":
                    role = "assistant"
                else:
                    role = "unknown"

            # Determine dialogue text:
            # In content arrays only dictionary blocks with explicit type text or input_text and string text are dialogue
            raw_text = ""
            content_candidates = [
                payload.get("message"),
                payload.get("text"),
                payload.get("content"),
                data.get("text"),
                data.get("content"),
            ]
            for cand in content_candidates:
                if cand is not None:
                    if isinstance(cand, str):
                        raw_text = cand
                        break
                    elif isinstance(cand, list):
                        parts = []
                        for b in cand:
                            if isinstance(b, dict):
                                b_type = b.get("type")
                                b_text = b.get("text")
                                if b_type in ("text", "input_text") and isinstance(b_text, str):
                                    parts.append(b_text)
                        raw_text = "\n".join(parts)
                        if raw_text:
                            break

            if not raw_text:
                malformed_count += 1
                continue

            event_time_raw = (
                data.get("timestamp")
                or data.get("time")
                or payload.get("timestamp")
                or payload.get("time")
                or ""
            )
            rec = analyze_dialogue_record(
                raw_text=str(raw_text),
                role=role,
                event_time_raw=str(event_time_raw),
                source_locator=file_str,
                source_line=line_idx,
            )
            records.append(rec)
            continue

        # Check generic role/text or role/content
        if "role" in data and ("text" in data or "content" in data):
            role = data.get("role", "unknown")
            raw_content = data.get("text", data.get("content", ""))
            raw_text = ""
            if isinstance(raw_content, str):
                raw_text = raw_content
            elif isinstance(raw_content, list):
                parts = []
                for b in raw_content:
                    if isinstance(b, dict):
                        b_type = b.get("type")
                        b_text = b.get("text")
                        if b_type in ("text", "input_text") and isinstance(b_text, str):
                            parts.append(b_text)
                raw_text = "\n".join(parts)
            if not raw_text:
                malformed_count += 1
                continue
            event_time_raw = data.get("time", data.get("timestamp", ""))
            rec = analyze_dialogue_record(
                raw_text=str(raw_text),
                role=str(role),
                event_time_raw=str(event_time_raw),
                source_locator=file_str,
                source_line=line_idx,
                known_truncated=bool(data.get("truncated", False)),
                override_expected=data.get("expected"),
            )
            records.append(rec)
            continue

        # Unrecognized schema line
        malformed_count += 1

    return records, malformed_count, False


def _safe_json_script(data: Any) -> str:
    """
    Safely serialize data to a JSON string that can be embedded directly
    within an HTML <script> tag without risking script injection or syntax breakage.
    Escapes <, >, &, and Unicode line/paragraph separators (U+2028, U+2029).
    When parsed in JavaScript with JSON.parse(...), the exact original string
    values are preserved identically without dropping or censoring source text.
    """
    raw_json = json.dumps(data, ensure_ascii=False)
    # Using raw_json inside a JS double-quoted string literal requires escaping backslashes and double quotes,
    # or alternatively, replace problematic HTML and JS line terminator characters inside the raw json string
    # directly if using `JSON.parse('...')` or embedded literal.
    # A standard secure pattern: json.dumps produces valid JSON. In JSON string literals:
    # We escape HTML-sensitive characters using unicode escapes \u003c, \u003e, \u0026.
    # Also escape U+2028 and U+2029 which are valid in JSON but invalid in JS string/script literals.
    # Note: \u003c etc. are valid JSON escape sequences, so JSON.parse('...') or direct JSON object remains valid JSON!
    cleaned_json = (
        raw_json
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return cleaned_json

def generate_html_report(records: List[Dict[str, Any]], output_path: Path) -> None:
    """
    Generate an offline, zero-network, filterable HTML report.
    """
    # Safe JSON encoding for script embedding
    safe_records_json = _safe_json_script(records)

    script_body = f"""
    const dataElement = document.getElementById("report-data");
    const records = JSON.parse(dataElement ? dataElement.textContent : "[]");

    function populateSelect(id, values) {{
      const sel = document.getElementById(id);
      values.forEach(v => {{
        if (v !== "") {{
          const opt = document.createElement("option");
          opt.value = v;
          opt.textContent = v;
          sel.appendChild(opt);
        }}
      }});
    }}

    const topics = new Set();
    const subjects = new Set();
    const confidences = new Set();
    records.forEach(r => {{
      if (r && r.topic) topics.add(r.topic);
      if (r && r.subject) subjects.add(r.subject);
      if (r && r.confidence) confidences.add(r.confidence);
    }});

    populateSelect("filterTopic", Array.from(topics).sort());
    populateSelect("filterSubject", Array.from(subjects).sort());
    populateSelect("filterConfidence", Array.from(confidences).sort());

    function safeText(val) {{
      if (val === null || val === undefined) return "";
      return String(val);
    }}

    function safeDisplay(val, fallback) {{
      if (val === null || val === undefined || val === "") return fallback;
      return String(val);
    }}

    function applyFilters() {{
      const topic = document.getElementById("filterTopic").value;
      const subject = document.getElementById("filterSubject").value;
      const negation = document.getElementById("filterNegation").value;
      const confidence = document.getElementById("filterConfidence").value;
      const truncated = document.getElementById("filterTruncated").value;
      const sensitive = document.getElementById("filterSensitive").value;

      const tbody = document.getElementById("tableBody");
      tbody.textContent = "";

      let count = 0;
      records.forEach(r => {{
        if (!r) return;
        if (topic && r.topic !== topic) return;
        if (subject && r.subject !== subject) return;
        if (negation && String(r.negation) !== negation) return;
        if (confidence && r.confidence !== confidence) return;
        if (truncated && String(r.truncated) !== truncated) return;
        if (sensitive && String(r.sensitive) !== sensitive) return;

        count++;
        const tr = document.createElement("tr");

        // 1. Line
        const tdLine = document.createElement("td");
        tdLine.className = "col-line";
        tdLine.textContent = safeText(r.source_line);
        tr.appendChild(tdLine);

        // 2. Topic
        const tdTopic = document.createElement("td");
        tdTopic.className = "col-topic";
        const bTopic = document.createElement("b");
        bTopic.textContent = safeText(r.topic);
        tdTopic.appendChild(bTopic);
        tr.appendChild(tdTopic);

        // 3. Subject
        const tdSubject = document.createElement("td");
        tdSubject.className = "col-subject";
        tdSubject.textContent = safeText(r.subject);
        tr.appendChild(tdSubject);

        // 4. Raw Event Time
        const tdTimeRaw = document.createElement("td");
        tdTimeRaw.className = "col-time-raw";
        const timeVal = safeDisplay(r.event_time_raw, "Time not provided");
        tdTimeRaw.textContent = timeVal;
        if (timeVal === "Time not provided") {{
          tdTimeRaw.className += " not-provided";
        }}
        tr.appendChild(tdTimeRaw);

        // 5. Content Hierarchy (Summary, Verbatim Quote, Context, Locator)
        const tdDetails = document.createElement("td");
        tdDetails.className = "col-details";

        // 5a. Summary (Unverified Local Rule Inference)
        const summaryBlock = document.createElement("div");
        summaryBlock.className = "field-block summary-block";
        const summaryLabel = document.createElement("div");
        summaryLabel.className = "field-label label-inference";
        summaryLabel.textContent = "Unverified local inference:";
        summaryBlock.appendChild(summaryLabel);
        const summaryText = document.createElement("div");
        summaryText.className = "field-content summary-content";
        summaryText.textContent = safeDisplay(r.summary_local_rule_inference, "(No inference summary)");
        summaryBlock.appendChild(summaryText);
        tdDetails.appendChild(summaryBlock);

        // 5b. Verbatim Quote
        const quoteBlock = document.createElement("div");
        quoteBlock.className = "field-block quote-block";
        const quoteLabel = document.createElement("div");
        quoteLabel.className = "field-label label-quote";
        quoteLabel.textContent = "Quote verbatim:";
        quoteBlock.appendChild(quoteLabel);
        const quoteCode = document.createElement("code");
        quoteCode.className = "field-content quote-content";
        quoteCode.textContent = safeDisplay(r.quote_verbatim, "(No quote)");
        quoteBlock.appendChild(quoteCode);
        tdDetails.appendChild(quoteBlock);

        // 5c. Minimal Context
        const contextBlock = document.createElement("div");
        contextBlock.className = "field-block context-block";
        const contextLabel = document.createElement("div");
        contextLabel.className = "field-label label-context";
        contextLabel.textContent = "Context:";
        contextBlock.appendChild(contextLabel);
        const contextPre = document.createElement("pre");
        contextPre.className = "field-content context-content";
        const ctxVal = safeDisplay(r.minimal_context, "Context not provided");
        contextPre.textContent = ctxVal;
        if (ctxVal === "Context not provided") {{
          contextPre.className += " not-provided";
        }}
        contextBlock.appendChild(contextPre);
        tdDetails.appendChild(contextBlock);

        // 5d. Inert Source Locator
        const locatorBlock = document.createElement("div");
        locatorBlock.className = "field-block locator-block";
        const locatorLabel = document.createElement("div");
        locatorLabel.className = "field-label label-locator";
        locatorLabel.textContent = "Source locator (inert text):";
        locatorBlock.appendChild(locatorLabel);
        const locatorSpan = document.createElement("span");
        locatorSpan.className = "field-content locator-content";
        const locVal = safeDisplay(r.source_locator_LOCAL_ONLY, "Locator not provided");
        locatorSpan.textContent = locVal;
        if (locVal === "Locator not provided") {{
          locatorSpan.className += " not-provided";
        }}
        locatorBlock.appendChild(locatorSpan);
        tdDetails.appendChild(locatorBlock);

        tr.appendChild(tdDetails);

        // 6. Negation
        const tdNeg = document.createElement("td");
        tdNeg.className = "col-tag";
        const spanNeg = document.createElement("span");
        const negVal = safeText(r.negation);
        spanNeg.className = "tag tag-" + encodeURIComponent(negVal).replace(/[^a-zA-Z0-9_-]/g, "");
        spanNeg.textContent = negVal;
        tdNeg.appendChild(spanNeg);
        tr.appendChild(tdNeg);

        // 7. Time Qual
        const tdTime = document.createElement("td");
        tdTime.className = "col-tag";
        tdTime.textContent = safeText(r.time_qualification);
        tr.appendChild(tdTime);

        // 8. Contradiction
        const tdContra = document.createElement("td");
        tdContra.className = "col-tag";
        const spanContra = document.createElement("span");
        const contraVal = safeText(r.contradiction_review);
        spanContra.className = "tag tag-" + encodeURIComponent(contraVal).replace(/[^a-zA-Z0-9_-]/g, "");
        spanContra.textContent = contraVal;
        tdContra.appendChild(spanContra);
        tr.appendChild(tdContra);

        // 9. Confidence
        const tdConf = document.createElement("td");
        tdConf.className = "col-conf";
        tdConf.textContent = safeText(r.confidence);
        tr.appendChild(tdConf);

        // 10. Truncated
        const tdTrunc = document.createElement("td");
        tdTrunc.className = "col-tag";
        const spanTrunc = document.createElement("span");
        const truncVal = safeText(r.truncated);
        spanTrunc.className = "tag tag-" + encodeURIComponent(truncVal).replace(/[^a-zA-Z0-9_-]/g, "");
        spanTrunc.textContent = truncVal;
        tdTrunc.appendChild(spanTrunc);
        tr.appendChild(tdTrunc);

        // 11. Sensitive
        const tdSens = document.createElement("td");
        tdSens.className = "col-tag";
        const spanSens = document.createElement("span");
        const isSensitive = Boolean(r.sensitive);
        spanSens.className = "tag " + (isSensitive ? "tag-sensitive" : "tag-false");
        spanSens.textContent = safeText(r.sensitive);
        tdSens.appendChild(spanSens);
        tr.appendChild(tdSens);

        tbody.appendChild(tr);
      }});

      document.getElementById("recordCount").textContent = `Showing ${{count}} of ${{records.length}} records`;
    }}

    document.getElementById("filterTopic").addEventListener("change", applyFilters);
    document.getElementById("filterSubject").addEventListener("change", applyFilters);
    document.getElementById("filterNegation").addEventListener("change", applyFilters);
    document.getElementById("filterConfidence").addEventListener("change", applyFilters);
    document.getElementById("filterTruncated").addEventListener("change", applyFilters);
    document.getElementById("filterSensitive").addEventListener("change", applyFilters);

    applyFilters();
  """

    # Compute SHA-256 of final exact UTF-8 script text
    script_bytes = script_body.encode("utf-8")
    script_hash = base64.b64encode(hashlib.sha256(script_bytes).digest()).decode("ascii")

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{script_hash}'; script-src-attr 'none';">
  <title>Chat Local Report (LOCAL-ONLY)</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      margin: 20px;
      background: #fdfdfd;
      color: #333;
    }}
    h1 {{ color: #1a1a1a; }}
    .filters {{
      background: #f0f4f8;
      padding: 12px 16px;
      border-radius: 6px;
      margin-bottom: 16px;
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}
    .filters label {{ font-size: 14px; font-weight: 600; }}
    .filters select, .filters input {{
      padding: 4px 8px;
      border: 1px solid #ccc;
      border-radius: 4px;
      font-size: 14px;
    }}
    table {{
      width: 100%;
      min-width: 1200px;
      border-collapse: collapse;
      font-size: 13px;
      table-layout: fixed;
    }}
    .table-wrapper {{
      width: 100%;
      overflow-x: auto;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      background: #fff;
    }}
    th, td {{
      padding: 8px 10px;
      border: 1px solid #e1e4e8;
      text-align: left;
      vertical-align: top;
      word-break: break-word;
      overflow-wrap: break-word;
    }}
    th {{
      background: #f6f8fa;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    tr:nth-child(even) {{ background: #fafbfc; }}
    tr:hover {{ background: #f1f8ff; }}
    col.col-line {{ width: 50px; }}
    col.col-topic {{ width: 110px; }}
    col.col-subject {{ width: 95px; }}
    col.col-time-raw {{ width: 140px; }}
    col.col-details {{ width: auto; }}
    col.col-tag {{ width: 68px; }}
    col.col-conf {{ width: 110px; }}
    .col-line {{ text-align: center; }}
    .col-topic {{ font-weight: 600; }}
    .col-time-raw {{ font-family: monospace; font-size: 12px; }}
    .col-details {{ min-width: 340px; }}
    .col-tag {{ text-align: center; }}
    .field-block {{
      margin-bottom: 8px;
    }}
    .field-block:last-child {{
      margin-bottom: 0;
    }}
    .field-label {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 3px;
    }}
    .label-inference {{
      color: #9c4221;
    }}
    .summary-content {{
      background: #fffaf0;
      border-left: 3px solid #dd6b20;
      padding: 6px 8px;
      font-size: 13px;
      color: #7b341e;
      border-radius: 2px;
    }}
    .label-quote {{
      color: #2b6cb0;
    }}
    .quote-content {{
      display: block;
      background: #ebf8ff;
      border-left: 3px solid #3182ce;
      padding: 6px 8px;
      font-size: 12px;
      color: #2c5282;
      border-radius: 2px;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
    }}
    .label-context {{
      color: #4a5568;
    }}
    .context-content {{
      display: block;
      background: #f7fafc;
      border: 1px solid #e2e8f0;
      padding: 6px 8px;
      font-size: 12px;
      color: #2d3748;
      border-radius: 3px;
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font-family: inherit;
    }}
    .label-locator {{
      color: #718096;
    }}
    .locator-content {{
      display: block;
      background: #edf2f7;
      border: 1px dashed #cbd5e0;
      padding: 4px 6px;
      font-size: 11px;
      color: #4a5568;
      border-radius: 3px;
      word-break: normal;
      overflow-wrap: anywhere;
      font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
      user-select: text;
    }}
    .not-provided {{
      color: #a0aec0;
      font-style: italic;
    }}
    .tag {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 3px;
      font-size: 11px;
      font-weight: 500;
    }}
    .tag-true {{ background: #ffeef0; color: #b31d28; }}
    .tag-false {{ background: #e6ffed; color: #22863a; }}
    .tag-sensitive {{ background: #ffd33d; color: #735c0f; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>Chat Local Report &mdash; Offline Viewer</h1>
  <div class="filters">
    <label>Topic:
      <select id="filterTopic">
        <option value="">(All)</option>
      </select>
    </label>
    <label>Subject:
      <select id="filterSubject">
        <option value="">(All)</option>
      </select>
    </label>
    <label>Negation:
      <select id="filterNegation">
        <option value="">(All)</option>
        <option value="true">True</option>
        <option value="false">False</option>
      </select>
    </label>
    <label>Confidence:
      <select id="filterConfidence">
        <option value="">(All)</option>
      </select>
    </label>
    <label>Truncated:
      <select id="filterTruncated">
        <option value="">(All)</option>
        <option value="true">True</option>
        <option value="false">False</option>
      </select>
    </label>
    <label>Sensitive:
      <select id="filterSensitive">
        <option value="">(All)</option>
        <option value="true">True</option>
        <option value="false">False</option>
      </select>
    </label>
    <span id="recordCount" style="margin-left: auto; font-weight: bold;"></span>
  </div>

  <div class="table-wrapper">
    <table id="dataTable">
      <colgroup>
        <col class="col-line" style="width: 50px;">
        <col class="col-topic" style="width: 110px;">
        <col class="col-subject" style="width: 95px;">
        <col class="col-time-raw" style="width: 140px;">
        <col class="col-details" style="width: auto;">
        <col class="col-tag" style="width: 68px;">
        <col class="col-tag" style="width: 68px;">
        <col class="col-tag" style="width: 68px;">
        <col class="col-conf" style="width: 110px;">
        <col class="col-tag" style="width: 68px;">
        <col class="col-tag" style="width: 68px;">
      </colgroup>
      <thead>
        <tr>
          <th>Line</th>
          <th>Topic</th>
          <th>Subject</th>
          <th>Event Time (Raw)</th>
          <th>Details (Summary / Quote / Context / Locator)</th>
          <th>Negation</th>
          <th>Time Qual</th>
          <th>Contradiction</th>
          <th>Confidence</th>
          <th>Truncated</th>
          <th>Sensitive</th>
        </tr>
      </thead>
      <tbody id="tableBody"></tbody>
    </table>
  </div>

  <script id="report-data" type="application/json">{safe_records_json}</script>
  <script>{script_body}</script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)


def run_report(manifest_path: str | Path, output_dir: str | Path) -> dict:
    """
    Integration callable: runs report indexing and extraction over authorized manifest files.
    Returns the CONTROL dict ONLY (counts, booleans, hashes, categories).
    No quotations, names, paths, ids, or timestamps taken from sources.
    """
    out_dir_p = Path(output_dir).resolve()
    out_dir_p.mkdir(parents=True, exist_ok=True)

    authorized_files, allowlisted_roots = parse_manifest(manifest_path)

    all_records: List[Dict[str, Any]] = []
    total_files_examined = len(authorized_files)
    total_files_excluded_credential = 0
    total_lines_skipped_malformed = 0
    total_quotes_sensitive_suppressed = 0

    for file_p in authorized_files:
        recs, malformed_cnt, cred_excluded = extract_records_from_file(file_p)
        total_lines_skipped_malformed += malformed_cnt
        if cred_excluded:
            total_files_excluded_credential += 1
            continue

        for r in recs:
            if r.get("sensitive"):
                total_quotes_sensitive_suppressed += 1
            all_records.append(r)

    # Output filenames
    local_json_path = out_dir_p / "report_local_records.json"
    local_csv_path = out_dir_p / "report_local_records.csv"
    local_html_path = out_dir_p / "report_local_viewer.html"
    control_json_path = out_dir_p / "control_summary.json"

    # Write LOCAL-ONLY JSON
    with open(local_json_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    # Write LOCAL-ONLY CSV
    fieldnames = [
        "topic",
        "summary_local_rule_inference",
        "quote_verbatim",
        "minimal_context",
        "source_locator_LOCAL_ONLY",
        "source_line",
        "event_time_raw",
        "subject",
        "negation",
        "time_qualification",
        "contradiction_review",
        "confidence",
        "truncated",
        "sensitive",
    ]
    with open(local_csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_records:
            writer.writerow(r)

    # Write LOCAL-ONLY HTML
    generate_html_report(all_records, local_html_path)

    # Compute sha256 of local artifacts
    def compute_sha256(p: Path) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    json_sha256 = compute_sha256(local_json_path)
    csv_sha256 = compute_sha256(local_csv_path)
    html_sha256 = compute_sha256(local_html_path)

    # Categories collected
    topics_found = sorted(list({r["topic"] for r in all_records if "topic" in r}))
    subjects_found = sorted(list({r["subject"] for r in all_records if "subject" in r}))
    confidences_found = sorted(list({r["confidence"] for r in all_records if "confidence" in r}))

    # Minted record ids hash (hash of deterministic sequence index + topic)
    id_hasher = hashlib.sha256()
    for idx, r in enumerate(all_records):
        id_hasher.update(f"{idx}:{r['topic']}:{r['subject']}:{r['confidence']}".encode("utf-8"))
    sorted_record_ids_hash = id_hasher.hexdigest()

    # Build CONTROL dict
    control_data: Dict[str, Any] = {
        "is_success": True,
        "files_manifest_count": total_files_examined,
        "files_credential_excluded_count": total_files_excluded_credential,
        "records_extracted_count": len(all_records),
        "lines_malformed_skipped_count": total_lines_skipped_malformed,
        "quotes_sensitive_suppressed_count": total_quotes_sensitive_suppressed,
        "has_contradictions_flagged": any(r["contradiction_review"] for r in all_records),
        "has_truncation_uncertainty": any(r["truncated"] for r in all_records),
        "artifact_hashes": {
            "local_records_json_sha256": json_sha256,
            "local_records_csv_sha256": csv_sha256,
            "local_viewer_html_sha256": html_sha256,
            "deterministic_record_ids_sha256": sorted_record_ids_hash,
        },
        "categories": {
            "topics": topics_found,
            "subjects": subjects_found,
            "confidence_classes": confidences_found,
        },
    }

    # Write CONTROL JSON file
    with open(control_json_path, "w", encoding="utf-8") as f:
        json.dump(control_data, f, ensure_ascii=False, indent=2)

    return control_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Privacy-preserving local chat indexer and report tool.")
    parser.add_argument("--manifest", required=True, help="Path to manifest JSON file")
    parser.add_argument("--output", required=True, help="Path to output directory")
    args = parser.parse_args()

    control = run_report(manifest_path=args.manifest, output_dir=args.output)
    # Print only CONTROL JSON to stdout (fixed counts, booleans, hashes, categories)
    print(json.dumps(control, indent=2))


if __name__ == "__main__":
    main()
