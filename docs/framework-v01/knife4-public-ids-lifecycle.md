# Knife 4 — public ids on records + readonly Task/Run projection

## Scope
- `DecisionRecord` / `JobRecord` carry public `request_id` (decision) and optional `goal_id`/`task_id`.
- `goal_id`/`task_id` come from stable charter→Goal projection (`id_projection.stable_goal_task_ids`); does **not** change hard-rule / lead judgment.
- Scheduler `refresh_job_status` attaches `result.framework_lifecycle` (Task/Run vocabulary) as **readonly**.
- No glue main-loop rewrite. No Hermes. No deploy.

## Not in this knife
- TeleAgent status/message observation sink (explicitly deferred).
