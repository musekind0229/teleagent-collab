"""Generic text attribution rules: claims are not verified user facts.
No extracted text is an instruction, authorization, or write-back request.
"""
import re
import os
import datetime


def annotate_source_times(claims, observations):
    """Copy report rows, retaining raw event tokens; mtime is never claim time."""
    lookup = {os.path.normcase(str(row.get('source_path_LOCAL_ONLY', ''))): row.get('fs_mtime_snapshot_not_creation') for row in observations}
    result = []
    for row in claims:
        copied = dict(row)
        refs = []
        for source in row.get('sources_LOCAL_ONLY', []):
            item = dict(source)
            stamp = lookup.get(os.path.normcase(str(item.get('source_LOCAL_ONLY', ''))))
            item['source_snapshot_mtime_not_claim_creation'] = stamp
            raw = item.get('event_time_raw')
            valid = False
            if isinstance(raw, str):
                try:
                    datetime.datetime.fromisoformat(raw)
                    valid = True
                except ValueError:
                    pass
            item['event_time_validation'] = 'declared_iso_time_not_independently_verified' if valid else 'unknown_or_unparsed_raw_retained'
            item['visible_time_evidence_kind'] = 'event_timestamp_declared' if valid else 'source_file_mtime_snapshot_not_claim_creation' if stamp else 'unknown'
            refs.append(item)
        copied['sources_LOCAL_ONLY'] = refs
        copied['time_review_required'] = bool(row.get('time_qualified')) or not any(r['visible_time_evidence_kind'] == 'event_timestamp_declared' for r in refs)
        result.append(copied)
    return result


def has_secret_material(text):
    """Conservative screen, not absolute certification; never return values."""
    normalized = text.replace('\\"', '"')
    if re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9_.=-]{12,}|\b(?:sk-|gsk_|gh[opusr]_|github_pat_|xox[baprs]-|AIza)[A-Za-z0-9_-]{16,}|\bAKIA[A-Z0-9]{16}\b|https?://[^\s/@]+@', normalized):
        return True
    assignments = re.finditer(r'''(?ix)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|secret|password|cookies?|session[_ -]?(?:key|token)|authorization|密码|口令|助记词|私钥)\s*['"]?\s*(?:[:=：]|是|为|\bis\b)\s*['"]?([^\s'",;}{]+)''', normalized)
    for item in assignments:
        value = item.group(1).lower()
        if value not in {'redacted', '[redacted]', '<redacted>', '[placeholder]'}:
            return True
    return False


def classify_claim(text, source_kind):
    value = text.strip()
    subject = re.sub(r'^(?:[-*+]|\d+[.)])\s+', '', value)
    if not value or value.startswith(('#', '```')):
        return None
    if any(marker in value for marker in ('[CONTEXT COMPACTION', 'Message from 🤖', '[STILL IN PROGRESS', '<environment_context>')):
        return {'kind': 'generated_or_peer_context', 'attribution': 'not_human_user_statement', 'confidence': 'not_user_fact'}
    if re.search(r'\bI am (?:Claude|Codex|Hermes|Grok|an? AI|an? assistant)\b|我是(?:AI|助手|Claude|Codex|Hermes|Grok)', value, re.I):
        return {'kind': 'generated_or_peer_context', 'attribution': 'agent_persona_not_user', 'confidence': 'not_user_fact'}
    third = bool(re.search(r'(?:我的?|用户的|他的|她的)(?:朋友|同事|妻子|丈夫|孩子|家人)|\bmy (?:friend|colleague|wife|husband|child|family)\b|(?:他说|她说|they said|he said|she said)', value, re.I))
    quoted = subject.startswith(('"', '“', '「', '>', "'"))
    if third or quoted:
        return {'kind': 'third_party_or_quoted_claim', 'attribution': 'do_not_assign_to_user', 'confidence': 'unverified'}
    saved = source_kind == 'memory_document'
    first_person = bool(re.search(r'(?:^|[。！？，,；;!?\n])\s*我(?:以前|曾经|现在|过去|目前|以后)?(?:是|叫|在|有|用|的|想|希望|喜欢|不喜欢|偏好|正在)|\bI (?:am|use|like|prefer|want|have)\b|\bmy\b', subject, re.I))
    user_named = bool(re.search(r'用户|\buser\b|偏好|喜欢|个人|本人', value, re.I))
    environment = saved and bool(re.search(r'本机|电脑|操作系统|运行环境|\bthis machine\b', value, re.I))
    if not (first_person or saved and user_named or environment):
        return None
    request = bool(re.search(r'请|帮我|本次|这次|现在请|please|can you|could you', value, re.I))
    question = value.endswith(('?', '？'))
    kind = 'question_not_verified_fact' if question else 'request_or_goal_not_stable_fact' if request else 'saved_environment_claim_not_current_system_state' if environment and not user_named else 'persisted_memory_claim' if saved else 'explicit_statement_in_user_role'
    return {'kind': kind,
            'attribution': 'saved_profile_not_independently_verified' if saved else 'user_role_human_identity_unverified',
            'confidence': 'source_claim_only_not_verified_fact',
            'time_qualified': bool(re.search(r'以前|曾经|过去|现在|不再|之后|used to|formerly|now|no longer', value, re.I)),
            'topic': topic_for(value)}


def topic_for(text):
    rules = [('preferences_and_response_style', r'喜欢|偏好|回复|回答|中文|英文|prefer|respond|answer'),
             ('work_and_goals', r'工作|项目|开发|目标|project|work|goal'),
             ('identity_or_context', r'我叫|我是|姓名|住在|身份|\bI am\b|\bmy name\b'),
             ('tools_and_environment', r'工具|电脑|系统|硬盘|账号|tool|computer|account'),
             ('relationships_or_other', r'朋友|同事|家人|friend|family')]
    for label, pattern in rules:
        if re.search(pattern, text, re.I):
            return label
    return 'other_user_related_claim'


def memory_blocks(text):
    """Yield contiguous verbatim prose spans with source line numbers.

    Paragraph markers, headings and code fences delimit spans; text on either
    side of omitted code must never be fabricated into one quoted paragraph.
    """
    result, block, in_code, first = [], [], False, 1
    def flush():
        if block:
            result.append(('\n'.join(block), first))
            block.clear()
    for line_no, line in enumerate(text.splitlines() + [''], 1):
        stripped = line.lstrip()
        if stripped.startswith('```'):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        if not stripped or stripped == '§' or stripped.startswith('#'):
            flush()
            continue
        if re.match(r'^(?:[-*+]|\d+[.)])\s+', stripped):
            flush()
        if not block:
            first = line_no
        block.append(line)
    return result


def user_texts(record):
    """Known ordinary JSONL schemas only; tool results are not human statements."""
    if not isinstance(record, dict):
        return []
    payload = record.get('payload')
    if record.get('type') == 'event_msg' and isinstance(payload, dict) and payload.get('type') == 'user_message':
        return [payload['message']] if isinstance(payload.get('message'), str) else []
    message = record.get('message')
    if not isinstance(message, dict):
        message = payload if isinstance(payload, dict) else record
    if message.get('role') != 'user' or record.get('isMeta'):
        return []
    content = message.get('content')
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [part['text'] for part in content if isinstance(part, dict)
                and part.get('type') in ('text', 'input_text') and isinstance(part.get('text'), str)]
    return []
