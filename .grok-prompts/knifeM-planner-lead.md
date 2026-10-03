# 刀M fix: let collab-service use any LeadAdapter (codex_cli) as the lead

Problem: `bin/collab-service.py --planner` only accepts `deterministic|grok`; `grok` hard-codes
`GrokCliLeadAdapter()`. Env `COLLAB_LEAD_ADAPTER=codex_cli` is ignored by the service, so the Codex CLI lead
adapter can never plan or auto-decide permission/review gates there.

Minimal change (do not alter `deterministic` or `grok` behavior):
1. Add `--planner lead`: `LeadAdapterPlanner(get_lead_adapter(), cwd=persist/"lead-work", timeout_sec=...)`, i.e.
   the adapter kind comes from `COLLAB_LEAD_ADAPTER` (factory default grok_cli). Refuse (argparse error / exit 2
   with a clear message) when the resolved adapter name is `inprocess` — the HTTP service has no dialogue to answer
   file-protocol asks, and the `codex` alias means inprocess; message must say "use COLLAB_LEAD_ADAPTER=codex_cli
   for the Codex CLI".
2. Add `--lead-timeout <sec>` (float, default 180, must be >0) used by both `grok` and `lead` planners.
3. At startup print one line to stderr: `lead planner: adapter=<name> timeout=<sec>s` (no paths that could contain
   secrets beyond the adapter name; for codex_cli you may add `bin=<basename only>`).
4. Help text for `--planner` explains: lead decides plan + routine permission/review gates; anything it cannot
   decide (lead error, illegal JSON, timeout, non-auto kinds like question/system_action) stays a pending human
   decision.
5. Tests (add to `src/test_app_service.py` or a new `src/test_collab_service_planner.py`): import the module from
   `bin/collab-service.py` via importlib (see how other tests load bin scripts, e.g. `rg -n "collab-service.py" src tests`);
   `_planner("lead", tmp)` with env `COLLAB_LEAD_ADAPTER=codex_cli` → LeadAdapterPlanner whose adapter is
   CodexCliLeadAdapter and timeout honored; `COLLAB_LEAD_ADAPTER=codex` / `inprocess` → refused; `grok` unchanged;
   argparse accepts `--planner lead --lead-timeout 240`.
6. Docs: `docs/lead-adapter.md` add a short "在 collab-service 里用 Codex 组长" section with the Windows example
   (env only in the service process):
   `set COLLAB_LEAD_ADAPTER=codex_cli` / `set COLLAB_CODEX_LEAD_BIN=C:\Users\Admin\.local\share\TeleAgent\runtimes\node\codex.cmd`
   / `python bin/collab-service.py --planner lead --backend teleagent-windows ...`, and when the lead decides vs
   when it escalates (MAX_LEAD_ATTEMPTS_PER_DECISION=2 for retryable errors; non-retryable → human at once).
Do not touch skills/, .env, credentials. Run the relevant tests from `src` and make them pass. Do not commit.
