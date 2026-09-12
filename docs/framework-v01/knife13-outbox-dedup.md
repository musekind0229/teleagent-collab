# Knife 13 — transactional state + durable outbox + receiver dedup

## Scope
- A **state change and its outbound event commit in the same transaction** (atomic persist of new entity state + outbox row).
- Outbound notifications use a **durable outbox** (`pending` → `claimed` → `sent`).
- The **receiver deduplicates** by `event_id` / delivery key (`dedupe_key`).
- Covered transitions: **task `queued` → `running`**; **blocked/queued** due to deps or resources; **goal completed**.
- **At-least-once delivery is not the same as an external side effect happening only once.** Crash-replay must not lose a pending outbox row; a second identical delivery is ignored on receive.
- Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No Hermes ledger. No deploy. No Cloud Agent.

## Transaction
Persisted under `<persist_dir>/.collab-outbox/`. File journal is the atomic unit:

| file | role |
| --- | --- |
| `journal.json` | full snapshot of entities + outbox (intent). Present only while a txn is in flight |
| `entities.json` | live Goal/Task state |
| `outbox.json` | live outbox rows |
| `inbox.json` | receiver EventLog / DedupStore |

Write journal (durable) → apply to live files → unlink journal. If the process dies after the journal is on disk, `OutboxStore.open` replays it. Apply is idempotent.

`commit_transition(goal_or_task, new_state, events[])` validates the public lifecycle, then writes **new state + outbox row(s)** in that one journal. Illegal transitions raise and write nothing.

## Outbox
| op | effect |
| --- | --- |
| `append_in_txn` | add pending rows (used by `commit_transition`) |
| `claim_pending` | pending (and unacked claimed) rows become `claimed` |
| `mark_sent` | claimed → sent after a delivery attempt |

Crash **before** `mark_sent` leaves the row pending/claimed. Replay `claim_pending` still returns it. That is at-least-once delivery: the notify sink may run again.

## Receiver
`DedupStore.receive` (EventLog) keys on `event_id` **or** `dedupe_key` / delivery key. A second identical delivery returns `duplicate=True` and is not appended. The inbox is durable so a crash after receive still dedups the replay.

```
commit_transition          →  state + pending outbox   (atomic)
claim_pending + notify     →  external side effect     (at-least-once)
receive                    →  processed once           (dedup)
mark_sent                  →  outbox row retired
```

## Code
- Kernel: `src/framework/outbox.py` — `OutboxStore`, `commit_transition`, `DedupStore` / `EventLog`, `pump_outbox`
- Goal states: `src/framework/lifecycle.py` — `GOAL_STATES`; execute-only Task may `running → succeeded`
- Light wire (inprocess only): closed loop records queued→running and goal completed; `run_inprocess_charter` records dep-queued and workdir blocked/queued; scheduler simulate records the same on its persist dir
- Tests: `src/test_framework_b13.py`

`bin/run-job.py` default remains TeleAgent. `--dry-run` still does not touch a backend.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 -m test_framework_b13
python3 -m test_framework_b12
# crash after journal / before mark_sent → pending outbox still delivered
# second identical delivery → receiver duplicate, EventLog length 1
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy / Cloud Agent
- changing default TeleAgent `bin/run-job.py` behavior
- exactly-once external side effects (notify may run twice; receive does not)
