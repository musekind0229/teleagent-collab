# 刀M fix 2: `wait` must not exit 4 while the lead is about to decide

Live finding (Win, `--planner lead`, codex_cli lead): a permission/review decision opens, `hermes-collab-request.py
wait` immediately returns exit 4 (need_human), and ~15 s later the lead resolves it and the goal completes. Hermes
would wrongly ask the human, and a human decide could race the lead.

Lead auto-resolution predicate already lives in `src/framework/app_service.py`
(`_continue_pending_decision` / `_lead_resolve_opened`): planner has callable `decide_action`, backend has callable
`resolve_decision`, `details.backend_kind in AUTO_RESOLVE_BACKEND_KINDS`, and NOT exhausted
(`attempts >= MAX_LEAD_ATTEMPTS_PER_DECISION or (attempts >= 1 and not lead_error.retryable)` means exhausted).

Minimal change:
1. Factor that predicate into one helper (e.g. `AppCoordinator.lead_will_decide(decision) -> bool`, or a module
   function taking planner/backend/decision) and use it in `_continue_pending_decision` so there is one source of
   truth (behavior unchanged).
2. `CollabApplication.status()` and `list_decisions()` (and any other place emitting public pending rows for the API
   if trivial) add to each public pending row `"awaiting": "lead"` or `"awaiting": "human"`, plus top-level
   `"awaiting_lead_count"` and `"awaiting_human_count"`. Keep `pending_decisions`, `pending_decision_count`,
   `awaiting_decision` exactly as today (backward compatible). Find how CollabApplication reaches the coordinator's
   planner/backend.
3. Client `bin/hermes-collab-request.py` `need_human_view`: if `pending_decisions` is non-empty and EVERY row has
   `awaiting == "lead"` → return None (keep polling). If some rows are human and some lead → need_human with only the
   human rows in `decisions`/`decision_ids` (add `"lead_pending_ids": [...]` in the view for the others). Rows without
   the field (old server) → today's behavior. The `awaiting_decision`/count fallback path: when the status carries
   `awaiting_human_count == 0` and `awaiting_lead_count > 0`, keep polling. A lead that fails flips the row to
   `human` (lead_error + attempts exhausted), so wait then exits 4 as before — make sure that is covered.
   `wait --timeout` still bounds everything (exit 3 on timeout as today; check the actual constant).
4. Decision briefs in wait output: include `awaiting` when present.
5. Tests: server side (planner with decide_action + backend with resolve_decision → "lead"; deterministic planner →
   "human"; exhausted non-retryable lead_error → "human"; kind question/system_action → "human"); client side
   need_human_view all-lead → None, mixed → human-only list, old-server rows → unchanged, counts fallback; a cmd_wait
   test with a fake status sequence [lead-pending, completed] → exit 0, and [lead-pending, human (lead failed)] →
   exit 4.
6. Docs: `docs/lead-adapter.md` section from the previous commit ("在 collab-service 里用 Codex 组长"): explain
   awaiting=lead/human and that wait keeps polling while the lead decides. Skill
   `integrations/hermes/skills/teleagent-collab/SKILL.md`: bump version 0.2.8 → 0.2.9, add one short rule: rows with
   `awaiting: "lead"` are being decided by the lead — do not ask the user about them; wait already keeps polling;
   exit 4 now means a human decision is really needed. Keep the rest of the skill unchanged.
Do not touch .env, credentials. Run relevant tests (src/test_app_service.py needs PYTHONPATH=.. from src;
src/test_hermes_collab_request.py; tests/ with PYTHONPATH=src:.) and make them pass. Do not commit.
