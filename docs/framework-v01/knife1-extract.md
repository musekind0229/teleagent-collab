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

## Knife 2 (2026-09-12)

- `attach_framework_projection` into glue/run-job report (`framework_projection`, readonly)
- `teleagent_adapter.permission_view`: public PendingAction; scheduler scan returns public view; rules use `to_native_for_rules`
- `list_pending_actions` on Linux/Windows adapters
