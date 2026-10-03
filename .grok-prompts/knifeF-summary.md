Task: improve `_decision_summary` in `bin/hermes-collab-request.py` so native TeleAgent decisions get a meaningful one-line summary.

Observed live (teleagent-windows backend): a pending decision row looks like
  kind="artifact_review", title="TeleAgent review", reason="TeleAgent worker requires a bounded decision",
  details={"backend_kind":"review","backend_request_id":"...","context_hash":"...",
           "payload":{"artifacts":{"hello.txt":{"sha256":"...","bytes":10777,"preview":"...","truncated":false}},
                      "tools":[{"tool":"write","status":"completed","input":{...}},{"tool":"read",...},{"tool":"report_final_files",...}],
                      "policy_violations":[],"approved_permissions":0,"finish":"stop"}}
Today summary == "TeleAgent review" (just the generic title) which tells a human nothing.

Requirements (minimal, stdlib only, keep existing behaviour for non-native rows):
1. When `details.backend_kind` is one of permission/question/review/system_action AND `details.payload` is a dict, build the summary from the payload, and prefer it over the generic title `TeleAgent <kind>` and the generic reason. Keep the existing candidate order otherwise (details.summary/message/reason/question still win if present and non-empty).
2. Formats (one line, via existing `_one_line`, limit 200):
   - review: `review: artifacts <name>(<bytes>B)[, ...]; tools <tool1>,<tool2>,...; finish=<finish>; violations=<n>` (skip parts that are missing). Do NOT include artifact preview text (may contain watermark/zero-width chars) or tool inputs/outputs.
   - permission: `permission: <payload.permission> <comma-joined payload.patterns>` (patterns may be list or missing; skip metadata/tool args).
   - question: `question: <first question text>` from payload.questions[0].question (fallback header), else payload.question.
   - system_action: `system_action: <type> <package.filename>` when present.
   - Fallback if nothing extractable: current behaviour.
3. Never raise on odd shapes (non-dict/non-list values) — fall back.
4. Output stays ASCII-escaped by default (don't change json emission).
5. Add unit tests in `src/test_hermes_collab_request.py` for review (using the shape above), permission, question, and a malformed payload fallback. Existing tests must keep passing.
6. Bump the skill version in `integrations/hermes/skills/teleagent-collab/SKILL.md` from 0.2.3 to 0.2.4 and add one short line in its wait exit-4 bullet noting summaries for TeleAgent-native decisions come from the worker payload (review artifacts/tools, permission pattern, question text). Do not change anything else in the skill.
Run: `python3 -m pytest -q src/test_hermes_collab_request.py` (or unittest if pytest missing) and make sure it passes. Do not commit.
