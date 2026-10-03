# 刀P3: GitHub #5 (progress / heartbeat) full version + GitHub #10 (usage budgets & checkpoints) full version

Repo: this directory (main, clean). Python stdlib. Never print tokens. Do not commit. Keep cross-platform
(pathlib; process/signal differences behind os.name branches; tests may mock os.name). Skill 0.2.13 → 0.2.14.

## #5 progress & heartbeat (issue text summarized)
Callers only see `running`; they cannot tell slow-but-healthy, waiting for approval, tool-blocked, no heartbeat,
done. Want: summary status with phase/current_action, last_activity, last_meaningful_progress, recent events,
artifact metadata checkpoint; mark source (worker or runner); allow unknown; one-shot JSON executors must say
progress_available=false and never fake a percentage; distinguish idle/stale, waiting tool, waiting decision,
executing, final delivery; no chat/command/secret leakage; cheap to query. Subagent observability:
`subagent_observability: false/"unknown"` for one-shot backends, never fake 0.
Implement:
1. Backend `observe_run` may return `progress` = {"phase": str, "last_heartbeat_at": iso|None, "last_progress_at":
   iso|None, "events": [<=5 short sanitized strings], "source": "runner"|"worker"|"none"}. Runner-level signals
   that are REAL and cheap:
   - agy (antigravity_cli_v1): process alive (poll) = heartbeat source "runner"; last_progress_at = latest mtime
     among the task workspace files / expected artifacts (artifact checkpoint: names+sizes+mtime, no content);
     stdout/stderr byte counts growing (no content). phase: "starting"|"executing"|"finalizing"(process exited,
     collecting)|"done".
   - inprocess: phase from its state machine.
   - supervised win/linux: whatever the engine/store already exposes (job status, last event time, pending
     action kind) — if nothing reliable, "unknown".
2. Coordinator persists a per-task `progress` snapshot on each tick (cheap; durable layer field; bounded size) and
   derives goal `progress` = {"available": bool, "phase", "state": one of "executing"|"waiting_decision"|
   "waiting_capacity"|"stale"|"idle"|"delivering"|"done"|"unknown", "last_heartbeat_at", "last_progress_at",
   "stale_after_sec", "recent_events", "artifacts_checkpoint", "source", "subagent_observability"}.
   `stale` when heartbeat older than `--stale-after` (default 120 s) while running. Unknown stays unknown.
3. capabilities: `progress: {"available": bool, "heartbeat": "runner_process"|"engine"|false, "artifact_checkpoint":
   bool, "percent": false, "subagent_observability": false|"unknown"}` per backend, honest.
4. Client summary `progress` uses the server block verbatim (still {"available": false, "phase": "unknown"} when
   absent). Add client `progress <id>` subcommand = tiny projection (state, phase, ages in seconds, recent events).

## #10 usage budgets & checkpoints (issue text summarized)
wall_sec + max_reworks are not enough; want capability saying whether live metering exists (unknown not faked);
max duration / usage / tool calls / no-progress policies, stating which are enforceable vs post-hoc only; when the
backend only reports usage at the end, keep a reliable wall clock limit and clear timeout wrap-up; budgets that
cannot be metered must be refused before dispatch or downgraded with notice; long tasks support checkpoints/stop
points, no hidden retry loops; report source and the cache/input/output/total fields as reported, never as a bill.
Implement:
1. Goal `budget` accepts (validated at submit, 400 on bad types): `wall_sec`, `max_reworks` (existing),
   `max_tokens` (total tokens as reported), `max_tool_calls`, `no_progress_sec`, `on_no_progress`:
   "checkpoint"|"fail" (default "checkpoint"), `budget_mode`: "enforce"|"report_only" (default "enforce").
2. capabilities: `metering: {"live_usage": bool, "usage_at_end": bool, "tool_calls": bool, "fields": [...],
   "source": "worker_self_reported"|"none"}` + `budget_enforcement: {"wall_sec":"enforced",
   "max_tokens": "enforced_live"|"post_hoc"|"unsupported", "max_tool_calls": ..., "no_progress_sec":
   "enforced"|"unsupported"}`. agy: usage only at end (from agy JSON), no live usage, no tool-call count →
   max_tokens "post_hoc", max_tool_calls "unsupported", no_progress_sec "enforced" (runner artifact/progress
   signal). inprocess: whatever is true. supervised: check if the engine reports tokens; else unsupported/post_hoc.
3. Submit: a budget field whose enforcement is "unsupported" → 409 `capability_unavailable` (missing
   ["budget:<field>"]) unless `budget_mode="report_only"` (then accepted with a goal warning "budget <field> is
   report-only on this backend"). "post_hoc" fields are accepted with a warning "checked after the run; cannot stop
   mid-run".
4. Enforcement on each tick (works with concurrent runs from #13):
   - live usage over max_tokens (backend reports live usage) → cancel the run(s), task fails `budget_exceeded
     max_tokens`, source as reported; post-hoc: when collected usage total exceeds max_tokens → task marked failed
     `budget_exceeded max_tokens (post_hoc)` even if artifacts exist? NO — keep artifacts, mark the task
     `succeeded_over_budget`? Keep it simple: goal gets `budget_status: {"max_tokens": {"limit","used","source",
     "exceeded": true, "enforced": "post_hoc"}}`, a warning, and the task result `budget_exceeded: true`; dependents
     do NOT start (checkpoint: goal enters `awaiting_decision` with a system decision "budget exceeded: continue or
     stop" for the human — never auto-continue).
   - no progress for `no_progress_sec` (using #5 last_progress_at, falling back to run start) → on_no_progress
     "checkpoint": cancel the run cleanly, keep workspace & partial artifacts, goal → awaiting_decision with a
     system decision `checkpoint: no progress for Ns` (need_human), no automatic retry; "fail": cancel and fail
     task `no_progress_timeout`.
   - wall_sec as today but the failure summary includes elapsed, last_progress_at and partial artifacts checkpoint.
   - Never extend a budget or retry silently. Decision resolution "continue" requires an explicit human verdict
     via the existing decisions API; continuing creates a retry of that task with history op recorded (counts
     toward max_reworks).
   - Usage reported as-is: {"source": "worker_self_reported", "fields": {...as reported...}, "note": "not a bill"}.
5. Client: summary shows `budget_status`; skill text: budgets that are unsupported are refused unless
   --budget-report-only; a checkpoint decision means stop and ask the user; usage numbers are worker self-report,
   not billing. Client flags: `--max-tokens`, `--max-tool-calls`, `--no-progress-sec`, `--on-no-progress`,
   `--budget-report-only`, `--wall-sec` (if not present).
6. Docs: docs/application-api.zh-CN.md, docs/hermes-collab-min-client.zh-CN.md.
Tests (src/test_progress_budget.py; fake backends + fake agy binary from src/test_contract_render.py where needed;
no real model): agy observe shows runner heartbeat + artifact checkpoint while a fake agy sleeps and writes a file;
stale after heartbeat stops (simulated clock); waiting_decision vs executing vs waiting_capacity distinguished;
one-shot backend → progress.available false, no percent; capabilities honest per backend; unsupported budget
field → 409, report_only → warning; post_hoc max_tokens exceeded → budget_status exceeded + checkpoint decision,
dependents not started; live usage fake backend over budget → run cancelled; no_progress checkpoint → run
cancelled, partial artifact kept, decision pending, no retry; on_no_progress fail → no_progress_timeout;
continue verdict → retry counted in max_reworks; works with two concurrent runs.
Run `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` (only allowed error: Windows-only
`test_prepare_injects_configured_proxy_and_userprofile`) and `PYTHONPATH=src:. python3 -X utf8 -m unittest discover -s
tests -t .` (all OK).
