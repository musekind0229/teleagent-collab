# Path-B knife 8 — inprocess stage-2 minimal closed loop

Repo tip should be ~d43f2a8 on main. Do NOT clone. No Cloud Agent. No Hermes ledger/source of truth.

## Goal (stage-2 minimal closed loop on inprocess)
1. One small file job via `bin/run-job.py --backend inprocess`, using only public `start_run` / `observe_run` / `collect_result`.
2. Independent acceptance: `force_lead_review` or equivalent **public** `artifact_review` gate. May use inprocess lead / local rules. NOT Hermes as task source.
3. Force one acceptance **failure** then **rework**: same Task, **new Run**; wall-clock budget must **not** reset. Decision-channel failure must **not** count as business rework.
4. Default TeleAgent entry (`bin/run-job.py` without inprocess) unchanged. Regressions green. No deploy.

## Design hints (non-binding — verify yourself)
- Extend `execution_backend` / `run_job_wire` rather than rewriting glue.
- Charter example: hello + `force_lead_review: true`, or a dedicated mini charter under `jobs/examples/`.
- For deterministic test: inject a lead/review stub that fails once then passes (inprocess lead / decision_fn), proving new run_id / attempt++ and wall deadline unchanged.
- Map error classes: acceptance_failed → rework new Run; decision_channel_failed → stop/fail without consuming rework budget as business rework.

## Deliverables
- Code + tests (`src/test_framework_b8.py` or similar)
- Short doc `docs/framework-v01/knife8-inprocess-closed-loop.md`
- Commit on main: `feat(framework): path-B knife8 inprocess closed-loop accept+rework`
- Push with `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`

## Verify
1. unittest b6/b7/b8 + relevant p0 green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
3. Demo/test proving: first review fail → rework → new Run id / attempt; wall not reset; final ok
4. Print: short+full SHA, session notes, whether closed loop ok, whether rework used new Run

## Success
Closed loop works on inprocess public API; TA default intact; pushed.
