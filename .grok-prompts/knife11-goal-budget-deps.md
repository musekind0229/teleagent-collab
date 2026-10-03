# Path-B knife 11 — Goal-level budget + Task dependencies

Repo tip ~b1a9b7d on main. Do NOT clone. No Cloud Agent. No Hermes ledger. No deploy. No glue rewrite. Default TeleAgent entry intact. You implement; coordinator only accepts.

## Goals
1. **Goal-level total budget** (at least one of: wall time / attempt counts / usage). Child Task + rework + approval costs **roll up** to the parent Goal. **Minting a new id must not reset** the Goal budget. Dispatch **reserves**, finish **reconciles**.
2. **Task dependencies**: unsatisfied deps stay `queued`; before entering `running`, check budget + resources (reuse workdir claim where relevant).
3. **Two inprocess jobs** prove: one depends on the other’s artifact or completion; **over-budget must not report success**.
4. Regressions green; TA default path unchanged.

## Suggested shape (non-binding)
- `execution_backend/goal_budget.py` or `framework/` helpers: GoalBudget account with reserve/reconcile; persist under workdir or jobs/state.
- Task graph: `depends_on: [task_id|charter_name|artifact]` on charter or Goal/Task projection; scheduler-sim or inprocess runner respects queue.
- Example charters under `jobs/examples/` (producer + consumer).
- Tests `src/test_framework_b11.py`; doc `docs/framework-v01/knife11-goal-budget-deps.md`.

## Deliverables
- Commit: `feat(framework): path-B knife11 goal budget + task deps`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print: short+full SHA; whether deps block unready tasks; whether budget rolls up / over-budget ≠ success

## Verify
1. unittest b10/b11 (+ b9 if quick) green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
3. Demo/test: consumer stays queued until producer done; over-budget path fails (not ok)
