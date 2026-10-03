# 刀N4: project task failures to the Goal top level (GitHub issue #12) + keep dispatch error reason

Repo: this directory (main, clean). Python stdlib. Never print tokens.

Live repro on this box: `collab-service --backend teleagent-linux` without a running TeleAgent → task result
`{"ok": false, "error": "backend dispatch failed: RuntimeError"}` and goal status `failure: null, failure_reason: ""`.

1. src/framework/app_service.py AppCoordinator dispatch (`except Exception as e: launched = {... "backend dispatch
   failed: {type(e).__name__}"}`): append a sanitized short reason: `f"backend dispatch failed: {type(e).__name__}:
   {sanitize_reason(str(e))[:300]}"` (sanitize_reason from framework.need_human; check it redacts secrets; if the
   message is empty keep the old form). Set `error_source: "spawn"` in that result.
2. Goal status (GET /v1/requests/{id}, see how `failure` / `failure_reason` are built today) for state failed:
   - `failure_reason`: non-empty one-line summary from the primary failed task (first failed task in plan order).
   - `primary_failure`: {task_id, run_id, title, error (<=300 chars, sanitized), source, missing_artifacts (list),
     retryable (bool, conservative), next_step (short safe text: e.g. timeout → "raise budget.wall_sec or split the
     task; open a NEW request citing this request_id"; spawn → "check collab-service --ready")}.
     `source` ∈ "worker_timeout" (error == "timeout" or budget/wall markers), "spawn", "contract_render",
     "acceptance", "contamination", "task_failed", "cancelled". Do not reuse/alter need_human semantics.
   - `failures`: list of the same brief for EVERY failed task (multi-task aggregation, never invent a single cause).
   - keep `failure` as today when it is set; do not dump stdout or the goal contract.
   - pending-decision goals and observation-wait timeouts are not failures (unchanged).
3. bin/hermes-collab-request.py summary: include `primary_failure` and `failure_count`; `failure_reason` falls back to
   primary_failure.error. wait kind `task_failed` sets `wait.task_timeout=true` also when primary_failure.source ==
   "worker_timeout".
4. Docs: docs/application-api.zh-CN.md status fields; docs/hermes-collab-min-client.zh-CN.md summary fields.
5. Tests (new src/test_failure_projection.py): worker timeout result {"ok":false,"error":"timeout","missing":[...]}
   → failure_reason non-empty, source worker_timeout, missing listed; spawn RuntimeError("no TeleAgent process
   (verified=0)") → source spawn and the message survives; two failed tasks → failures has 2; awaiting decision →
   no primary_failure; secret-looking text in the exception (e.g. "token=sk-ABCDEF...") is redacted. Client summary
   test.
Run `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` (only allowed error: Windows-only
`test_prepare_injects_configured_proxy_and_userprofile`) and `PYTHONPATH=src:. python3 -X utf8 -m unittest discover -s
tests -t .` (all OK). No skill change needed. Do not commit.
