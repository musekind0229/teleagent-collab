# Knife 11 — Goal-level budget + Task dependencies

## Scope
- A **Goal** owns a total budget (wall time, attempt counts, optional usage). Child Task + rework + approval costs **roll up** to that parent. **Minting a new task_id / run_id must not reset** the account.
- Dispatch **reserves** capacity; finish **reconciles** actual cost and releases unused reservation.
- **Over-budget must not report success.**
- Unsatisfied `depends_on` stay `queued`. Before `queued → running`: check deps, then budget, then workdir claim (knife 10).
- Two inprocess jobs prove the graph: consumer waits for producer artifact/completion.
- Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No Hermes ledger. No deploy. No Cloud Agent.

## Budget account
Persisted under `<persist_dir>/.collab-goal-budget/<goal_id>.json` (workdir by default). Keyed by **goal_id**, never by task/run id.

| op | effect |
| --- | --- |
| `reserve` | hold attempts / wall / reworks / approvals / usage before a Run starts |
| `reconcile` | commit actual child cost; unused reservation returns to the pool |
| `release` | drop a held reservation without consuming (did not start) |
| open existing goal_id | restore consumed + wall_deadline; **not** a fresh zeroed account |

Limits come from Goal.budget / charter (`wall_sec` / `timeout_sec`, `max_attempts`, `max_reworks`, `max_lead_calls` / `max_approvals`, optional `max_usage`).

## Dependencies
Charter `depends_on` maps onto `Task.depends_on` (array of strings):

- charter / job name: `producer`
- task id: `task_…` or `task:…`
- artifact: `artifact:producer.txt` (YAML `- artifact: producer.txt` is accepted)

Standalone inprocess run (no scheduler graph) enforces **artifact** refs against the workdir. A scheduler tick passes a `completed` index so named deps block too.

## Code
- Account: `src/execution_backend/goal_budget.py` — `GoalBudget.reserve` / `reconcile`, `can_enter_running`, `simulate_scheduler_goal_deps`, `run_dependent_inprocess_jobs`
- Refs: `src/framework/task_deps.py`
- Wire: closed loop reserves per attempt and rolls rework + approval up; `run_inprocess_charter` queues on unsatisfied deps **before** claiming the workdir
- Examples: `jobs/examples/producer.charter.yaml`, `jobs/examples/consumer.charter.yaml` (shared `goal_id: goal_dep_demo`)
- Tests: `src/test_framework_b11.py`

`bin/run-job.py` default remains TeleAgent. `--backend inprocess` inherits the gate. `--dry-run` still does not touch a backend.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 -m test_framework_b11
python3 -m test_framework_b10
python3 -m test_framework_b9
# consumer stays queued until producer.txt exists / producer completed
# Goal max_attempts=1 → consumer is budget_exhausted, ok is not true
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy / Cloud Agent
- changing default TeleAgent `bin/run-job.py` behavior
