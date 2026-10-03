# 刀N2: backend capabilities + caller-required capabilities + agy acceptance gate in Application API
# (issues #1, #2, #8 server part, #5/#10 honesty). Skill 0.2.10 -> 0.2.11.

Repo: this directory (main, clean). Python stdlib. Never print tokens. Do not touch `build_agy_prompt`/contract_render
semantics (just landed for #3/#9/#11) except to call them.

## #1 Capabilities (src/framework/app_service.py + backends)
1. Each execution backend exposes `capabilities() -> dict` (default in `execution_backend/base.py`: everything
   "unknown"/False). Implement honestly for: inprocess (`InProcessExecutionBackend`), agy
   (`antigravity_cli_v1`: permission/question/review channel False — list_pending_actions is always empty and
   reply_permission is 501; external_input_enforcement "prompt_only"; os_sandbox False; access_audit False;
   skip_permissions = whether AGY auto-approve is configured in this backend's env (use the same helper
   agy_auto_approve_enabled with charter=None); resume False unless the code supports it), windows supervised and
   linux supervised (`windows_supervised_v1`, `linux_supervised_v1`: permission/question/review True via TeleAgent
   native decisions; external_input_enforcement "permission_gate" (hard_reject + lead/human decision);
   os_sandbox False; access_audit "decision_log"; skip_permissions False; resume True if the engine resumes after
   restart — check, else "unknown"). Schema (stable keys, document in docs/application-api.zh-CN.md):
   ```
   {"api_version", "backend": {"id", "kind"}, "planner": {"name", "decomposes": bool, "lead_review": bool},
    "channels": {"permission": bool, "question": bool, "review": bool},
    "external_inputs": {"max": 8, "enforcement": "permission_gate"|"prompt_only"|"none"},
    "isolation": {"os_sandbox": false, "access_audit": "decision_log"|false|"unknown", "prompt_constraints": true},
    "skip_permissions": bool|"unknown", "resume": bool|"unknown",
    "acceptance": {"artifact_presence": true, "exact_content": bool, "lead_review": bool, "executable_checks": false},
    "progress": {"available": false}, "usage": {"source": "worker_self_reported"|"unknown"},
    "warnings": [human-readable strings, e.g. agy + skip_permissions => "backend runs with skip-permissions: no
                 permission gate; pinned external inputs are prompt-only"]}
   ```
   Planner: deterministic.single_task → decomposes False, lead_review False; lead_adapter planner → True/True.
2. `GET /v1/capabilities` (authorized like other /v1 routes) returns it; `GET /health` additionally returns
   `backend` id and `planner` name (no secrets) — keep `ok`/`api_version`.
3. `POST /v1/requests` accepts optional `required_capabilities` (list of strings from a fixed vocabulary:
   `permission_gate`, `question_channel`, `review_channel`, `external_input_enforcement`, `os_sandbox`,
   `access_audit`, `no_skip_permissions`, `lead_review`, `decomposition`). Unknown names → 400 `invalid_request`.
   Unmet → 409 `code=capability_unavailable` with `missing:[...]` and the capabilities snapshot, BEFORE any goal is
   persisted or dispatched. Additionally, implicit requirement: a Goal with `external_inputs` on a backend whose
   external_inputs.enforcement is "prompt_only" AND skip_permissions is True is refused with 409
   `capability_unavailable` (missing ["external_input_enforcement"]) unless the request sets
   `acknowledge_prompt_only_inputs: true` (operator-acknowledged downgrade); when acknowledged, record a warning on
   the goal and surface it in status `warnings`. Check how idempotency works so a 409 does not create a goal.
4. Status payload (`GET /v1/requests/{id}`) gets `warnings: [...]` (capability warnings relevant to the goal) and
   `capabilities_ref: {"backend","planner"}`.

## #2 agy acceptance gate in the Application API
- In AppCoordinator, where a backend result is collected and gated (`_gate_backend_result` → finish_task), when the
  backend is agy (`backend_id` startswith "antigravity"), call `apply_agy_acceptance_gate(charter=<worker charter
  for that task (worker_charter_for_task)>, workdir=<task workspace>, result=result)` before deciding success, so both
  entries share the mechanism. Keep contamination scanning.
- Results carry `review: {"status": "not_requested"|"passed"|"failed"|"unsupported", "source":
  "agy_exact_content"|"artifact_review"|"lead"|"none", "evidence": short str}`. If acceptance text was requested
  but no reviewer can actually check it (agy, deterministic planner, acceptance text not exact-content form) do NOT
  mark it passed: set `review.status="unsupported"` and add a goal/task warning "acceptance text was not
  independently verified"; the goal may still complete (artifact presence) but status must say so explicitly.
  Exact-content acceptance that fails → task fails with acceptance_failed. force_lead_review on agy without a
  checkable criterion → fail closed as the gate already does.
- Regression: files all present but exact-content acceptance wrong → not succeeded, same result through
  Application API and run_job_with_agy_backend.

## Skill (0.2.11) & docs
- SKILL.md: step 1 ping now shows `capabilities`; add a short "后端能力边界" section: read `capabilities` before
  sensitive work; if `channels.permission` is false or `skip_permissions` is true or `external_inputs.enforcement`
  is `prompt_only`, the backend has NO permission gate — "no permission prompt appeared" does NOT mean access was
  safe; prompt constraints are not an OS sandbox; for private/sensitive data stop and tell the user, only continue
  if the user/operator explicitly accepts (`--ack-prompt-only-inputs`); never claim review passed when
  `review.status` is `unsupported`. Also mention `--require-capability NAME` (repeatable).
- bin/hermes-collab-request.py: `open --require-capability NAME` (repeatable) → `required_capabilities`;
  `--ack-prompt-only-inputs` → `acknowledge_prompt_only_inputs: true`; 409 capability_unavailable printed with
  `missing` and exit 1 (code passthrough). Summary output already passes `warnings`; also include `review` per task
  in the summary.
- docs/application-api.zh-CN.md + docs/hermes-collab-min-client.zh-CN.md: capabilities schema, required caps,
  review status meanings; planner doc (#8): deterministic = one task no decomposition, lead = decomposes/reviews,
  plus a staged-delivery example (sample → implementation+test → dry-run → operator approval).

## Tests
New `src/test_capabilities.py`: capabilities for inprocess/agy(with and without skip)/windows/linux backends;
/v1/capabilities and /health over the real HTTP handler (pattern used by existing app_service HTTP tests); required
caps unmet → 409 and no goal persisted; unknown cap → 400; agy + external_inputs + skip → 409 unless acknowledged
(then warning visible in status); agy gate via Application API with fake agy binary (see
src/test_contract_render.py `_write_fake_agy`) — wrong exact content → failed acceptance_failed; correct → passed;
non-exact acceptance text → review.status unsupported + warning, never "passed". Client tests for new flags.
Run both suites: `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` (only allowed error: Windows-only
`test_prepare_injects_configured_proxy_and_userprofile`), `PYTHONPATH=src:. python3 -X utf8 -m unittest discover -s
tests -t .` (all OK). Do not commit.
