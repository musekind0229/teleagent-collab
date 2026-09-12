# Knife 8 — inprocess stage-2 minimal closed loop (accept + rework)

## Scope
- One small file job via `bin/run-job.py --backend inprocess` using **only** public `start_run` / `observe_run` / `collect_result`.
- Independent acceptance: `force_lead_review` triggers a public `artifact_review` gate (inprocess lead / local rules). Hermes is **not** the task source.
- Acceptance fail → **same Task, new Run**; `ReworkBudget.wall_deadline` is never reset.
- `decision_channel_failed` (timeout / binding / illegal JSON) → stop/fail **without** consuming business rework.
- Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No deploy.

## Code
- `src/execution_backend/closed_loop.py` — `run_inprocess_closed_loop` / `artifact_review`
- Wire: `run_inprocess_charter` now calls the closed loop (execute-only when `force_lead_review` is false)
- Error mapping: `framework.lifecycle.map_error_class` (`acceptance_failed` vs `decision_channel_failed`)
- Example charter: `jobs/examples/hello-inprocess-closed-loop.charter.yaml`

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 bin/run-job.py --backend inprocess jobs/examples/hello.charter.yaml
python3 bin/run-job.py --backend inprocess jobs/examples/hello-inprocess-closed-loop.charter.yaml
# fail once then rework (test/demo stub; local rules pass on first try without it):
COLLAB_INPROCESS_REVIEW_STUB=fail_once python3 bin/run-job.py --backend inprocess \
  jobs/examples/hello-inprocess-closed-loop.charter.yaml
python3 -m test_framework_b8
```

First review fail → new `run_id` / `attempt++` / same `task_id`; `wall_deadline_unchanged=true`; final ok.

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy
