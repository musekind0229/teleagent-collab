# Knife 12 — Goal coordinator ownership + versioned plan revisions

## Scope
- A Goal has **one effective coordinator** at a time, plus a monotonic **ownership version**.
- Plan revisions go through a **structured proposal**. The kernel **validates legality**, then **commits**.
- Submissions from a **stale ownership instance are rejected**.
- **Handoff / succession MUST bump** the ownership version (same-coordinator succession included).
- Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No Hermes ledger. No deploy. No Cloud Agent.

## Ownership record
Persisted under `<persist_dir>/.collab-goal-ownership/<goal_id>.json`. Keyed by **goal_id**.

| field | meaning |
| --- | --- |
| `coordinator_id` | the single live coordinator |
| `version` | ownership instance; starts at 1 on first claim |
| `updated_at` | unix seconds of last claim / handoff |

| op | effect |
| --- | --- |
| `claim` | first coordinator at version 1; second claimant is refused; same coordinator is idempotent (no bump) |
| `handoff` / `succeed` | replace (or refresh) coordinator; **always** `version += 1`; previous instance is stale |
| `propose_plan_revision` | structured proposal; stale / wrong coordinator / illegal plan → `rejected` |
| `commit_revision` | re-check live ownership + plan legality, then replace the committed plan |

Plan revision number (`plan_revision`) is independent of ownership version. Several legal revisions may land under the same live instance. Handoff is what expires the instance.

## Plan legality
The kernel, not the coordinator, decides whether a proposal may commit:

- Plan must be an object with a `tasks` list; `task_id` unique and required
- `goal_id` on the plan / tasks must match the Goal (when present)
- Plan **cannot rewrite** `coordinator_id` / `ownership_version` / `ownership` / `version`
- Task status changes follow the public Task lifecycle (`assert_transition`)
- New tasks start `queued` or `blocked`; active tasks (`running` / `awaiting_decision` / `review` / `cancel_requested`) cannot be dropped

## Stale instance
After handoff, the previous `(coordinator_id, version)` pair is expired.

| submitter | version presented | result |
| --- | --- | --- |
| live coordinator | current | accepted (then legality) |
| anyone | not current | `stale_ownership` — **rejected** |
| other id | current | `not_coordinator` — rejected |
| proposal minted before handoff | (commit later) | `stale_ownership` — commit refused |

## Code
- Kernel: `src/framework/goal_ownership.py` — `GoalOwnership`, `GoalOwnershipStore.claim` / `handoff` / `propose_plan_revision` / `commit_revision`
- Light projection: charter `coordinator_id` → `Goal.role_hints.coordinator` (Goal schema has no coordinator field). Report projection stays readonly and does not flip `ok`.
- Tests: `src/test_framework_b12.py`

`bin/run-job.py` default remains TeleAgent. `--dry-run` still does not touch a backend.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 -m test_framework_b12
python3 -m test_framework_b11
# successful propose+commit lands plan_revision=1 under coordinator v1
# after handoff, the old coordinator's submit is stale_ownership
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy / Cloud Agent
- changing default TeleAgent `bin/run-job.py` behavior
- adding `coordinator` to the Goal JSON Schema (hint stays on `role_hints`)
