# Path B knife 1 — what was extracted

Date: 2026-09-12

## Landed

- Versioned schemas under repo `contracts/`
- Decision notes under `docs/framework-v01/`
- `src/framework/`: charter→Goal/Task map, Task transition skeleton, Run helper
- `teleagent_adapter.native_handle`: session id as opaque `native_handle` only

## Not done (next knives)

- glue/scheduler still owns the live loop; not rewritten
- No second execution backend
- Hermes not wired as ledger
- Win migration branch still absent on this box

## Old entrypoints

`bin/run-job.py` / `bin/run-scheduler.py` unchanged in behavior.
