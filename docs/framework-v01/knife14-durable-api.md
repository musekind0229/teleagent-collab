# Knife 14 — durable / perpetual layer (minimal API)

## Scope
File-backed **durable/perpetual Goal layer**. Not a transport (no HTTP/RPC/socket). Coordinator calls these ops; the kernel accepts or refuses.

| op | contract |
| --- | --- |
| `submit_goal` | idempotent via **submit key**; duplicate submit must **not** open a second Goal |
| `get_goal` | read current Goal snapshot |
| `resolve_decision` | resolve **exactly one** pending decision; fuzzy batch of unrelated actions is refused |
| `cancel_goal` | stop accepting new child tasks + request terminate in-flight; **`cancel_requested` ≠ `cancelled`** |
| `get_report` | report snapshot (readonly) |

Kernel must **not** promote identity/memory (no 晋升身份记忆). Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No Hermes ledger. No deploy. No Cloud Agent.

## Persist
Under `<persist_dir>/.collab-durable/`:

| file | role |
| --- | --- |
| `store.json` | canonical snapshot: submit_key → goal_id, Goal records |
| `goals/<goal_id>.json` | per-Goal copy for inspection |

Atomic write is tmp + `os.replace`. Re-open loads `store.json`.

## submit_goal
First call with a submit key opens one Goal (`created=true`). A second call with the **same key** returns that Goal (`created=false`, `duplicate=true`) and does not mint a second `goal_id`. A different key opens a different Goal.

`Goal.idempotency_key` records the submit key. Payload differences on a duplicate are ignored (idempotent replay, not a merge).

## resolve_decision
One pending decision per call. Refused (`reason=unrelated_batch` / `fuzzy_batch`) when:

- `decisions` / `batch` / `items` lists more than one target
- `actions` rows name more than one `decision_id` / `request_id`
- mixed ops (`cancel_goal`, `submit_goal`, identity/memory promotion) ride along

Related action rows that share the **same** decision id are allowed. Several pending decisions may exist; the caller must name exactly one when more than one is open. Already-resolved same id is idempotent.

## cancel_goal
`cancel_goal` **requests** stop. It does not claim execution has stopped.

| field | after `cancel_goal` | after `effect_cancel` |
| --- | --- | --- |
| `state` | `cancel_requested` | `cancelled` |
| `cancel_requested` | true | true (the request still happened) |
| `cancelled` | **false** | true |
| `accepting_child_tasks` | false | false |
| in-flight tasks | `cancel_requested` + `terminate_requested` | `cancelled` |

`effect_cancel` is refused unless state is already `cancel_requested`. Request received ≠ stopped.

New `add_child_task` after cancel is `admission_closed`.

## get_goal / get_report
`get_goal` returns the live snapshot (`state`, cancel flags, tasks, pending/resolved decisions). `get_report` is a readonly projection (`readonly: true`) and does not flip job `ok`.

## Identity / memory
`kernel_promotes_identity_memory()` is **false**. Submit/resolve payloads that carry identity/memory promotion keys are refused (`identity_memory_forbidden`). The durable layer stores Goal / Task / Decision / Report only.

## Code
- Kernel: `src/framework/durable_api.py` — `DurableLayer`, `submit_goal` / `get_goal` / `resolve_decision` / `cancel_goal` / `get_report`
- Goal lifecycle: `src/framework/lifecycle.py` — `GOAL_STATES` includes `cancel_requested` (distinct from `cancelled`)
- Thin CLI (optional, still no transport): `bin/durable-cli.py`
- Tests: `src/test_framework_b14.py`

`bin/run-job.py` default remains TeleAgent. `--dry-run` still does not touch a backend.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 -m test_framework_b14
python3 -m test_framework_b13
# duplicate submit_key → same goal_id, goal_count=1
# resolve_decision with two unrelated actions → unrelated_batch
# cancel_goal → state=cancel_requested, cancelled=false; effect_cancel → cancelled
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy / Cloud Agent
- changing default TeleAgent `bin/run-job.py` behavior
- binding this API to HTTP/RPC/websocket
- promoting identity/memory into the kernel
