Task (刀H): artifact acceptance must check CONTENT for AIGC watermark / invisible-character contamination, not just file presence.

Background (observed live on Windows TeleAgent desktop): TeleAgent's `write` tool wrote "hello decision" (14 bytes) but the file on disk was 10777 bytes: the text, then "\n\nAI生成\n" and ~3584 zero-width chars (U+200B x1926, U+200D x1658) used as steganographic watermark. Presence-only acceptance passed it.

Implement, minimal and stdlib-only:

1. New module `src/framework/artifact_contamination.py`:
   - `scan_bytes(raw: bytes) -> dict` returning e.g. `{"contaminated": bool, "encoding": "utf-8"|"utf-8-bom"|"utf-16-le"|"utf-16-be"|"binary", "invisible": {"U+200B": n, ...}, "aigc_marks": {"AI生成": n, ...}, "first_offset": <char index or None>}`.
   - Invisible set: U+200B, U+200C, U+200D, U+2060, U+FEFF, U+180E, U+2061..U+2064. 
   - BOM rule: a single U+FEFF as the FIRST character of the decoded text (UTF-8 BOM EF BB BF at byte 0, or a UTF-16 BOM FF FE / FE FF at byte 0) is a legitimate encoding marker and is NOT contamination; any other U+FEFF (not at position 0, or a second one) IS contamination. Document this in the docstring.
   - Decoding: UTF-16 only if a UTF-16 BOM is at byte 0; else strict UTF-8 (with optional BOM). If it does not decode, encoding="binary" and contaminated=False (do not scan binary; report it as not scanned).
   - AIGC marks: regex for the label actually seen, allowing optional whitespace and a few close variants: `AI\s*生成`, `人工智能生成`, `AI\s*generated` is NOT included (too many false positives in English prose). Count matches.
   - `scan_file(path, max_bytes=2*1024*1024)`; files larger than max are scanned on the first max_bytes only (note `truncated: True`).
   - `summarize(findings_by_name: dict) -> str` one-line like `CONTAMINATED hello.txt: AI生成x1, U+200Bx1926, U+200Dx1658` (empty string when clean).
   - Opt-out: content check can be disabled per task with `allow_aigc_marks: true` (see 3/4). Keep the helper pure.

2. Windows controller `win_collab/core.py`:
   - In `snapshot()`, add `"contamination": scan_bytes(raw)` per artifact (import must work both when run as `python -m win_collab` from repo root and when `src` is on sys.path — check how win_collab currently imports things / how execution_backend imports win_collab; if importing from src/framework is awkward, put the scanner in a place both can import, e.g. keep the canonical module in `src/framework/` and do a guarded import, or vendor a tiny wrapper. Do not duplicate logic in two places).
   - In the review `pass` branch (where it already refuses on policy_violations), refuse with ValueError('Artifact content contaminated (AIGC mark / invisible chars): ...') when any artifact is contaminated, unless charter `allow_aigc_marks` is true. `fail` must still work (redo path) so the worker can fix it.
   - Make sure charter validation accepts the optional boolean key `allow_aigc_marks`, and `WindowsSupervisedExecutionBackend._charter` passes it through (like forbidden_tools).

3. Service-side gate `src/framework/app_service.py` (backend-independent; covers antigravity too): right after `result = self.backend.collect_result(run_id)` and before `finish_task` (and in any other place where a task is finished successfully from a backend result — check), if `result.ok` then scan every artifact path in `result["artifacts"]` (absolute paths or relative to `result["workspace"]` — handle both; only files that exist). If any contaminated and the task/goal does not opt out, set `result["ok"]=False`, `result["error"]="artifact_contaminated: <summarize()>"`, `result["artifact_contamination"]=<findings>`, so the task fails. Opt-out: `allow_aigc_marks: true` in the goal acceptance (look at `task_acceptance_criteria` and how goal.acceptance is stored) or task inputs; pick the simplest place and document it.
   - When projecting a TeleAgent review decision (open_decision around line 706), if the payload's artifacts carry contamination, set `title` to `TeleAgent review (CONTAMINATED)` and put `summary` in details = summarize(...) so clients see it.

4. Client `bin/hermes-collab-request.py`: in `_review_payload_summary`, if any artifact has `contamination.contaminated`, prefix the summary with `CONTAMINATED <name>: <marks/invisible counts>; ` (keep 200 char limit, the prefix must come first). Also, if a failed task result has `artifact_contamination`, nothing else needed.

5. Tests (unittest, must run on Linux box):
   - `src/test_artifact_contamination.py`: clean ASCII; clean Chinese UTF-8; the observed pattern ("hello decision\n\nAI生成\n" + "\u200b\u200d"*n); leading UTF-8 BOM only → clean; BOM + later U+FEFF → contaminated; UTF-16-LE with BOM clean; binary → not contaminated, encoding binary; "AI 生成" variant; summarize output.
   - core: snapshot includes contamination; review pass refused on contaminated artifact, allowed with allow_aigc_marks; fail/redo still OK (follow existing test style in tests/test_win_collab_budget.py or similar).
   - app_service gate: a fake backend whose collect_result returns ok with a contaminated artifact → task fails with error starting `artifact_contaminated`; clean → succeeds; opt-out → succeeds (follow existing fakes in src/test_app_service.py).
   - client summary prefix test in src/test_hermes_collab_request.py.
6. Docs: short section in `docs/application-api.zh-CN.md` (or the most relevant existing doc) + `integrations/hermes/README.zh-CN.md` explaining the content check, BOM rule, opt-out. Skill `integrations/hermes/skills/teleagent-collab/SKILL.md`: bump 0.2.4 → 0.2.5 and add one line: if summary starts with `CONTAMINATED` or error starts with `artifact_contaminated`, report it to the user verbatim (it is a failed/unsafe artifact); do not approve.
Run: `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` and `PYTHONPATH=src:. python3 -X utf8 -m unittest discover -s tests -t .`. Baseline before your change: src 670 tests with exactly 1 error (test_agy_account_pool ... test_prepare_injects_configured_proxy_and_userprofile, Windows-only, ignore it); tests 127 OK. Nothing new may fail. Do not commit.
