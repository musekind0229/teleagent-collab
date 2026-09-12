# Framework v0.1 (path B)

Boss locked **B: evolve collab kernel** (2026-09-12).

- Contracts (versioned schemas): [`../../contracts/`](../../contracts/)
- Phase 0/1 comparison notes (frozen decision aid): this folder
- Hermes is **not** the dispatch ledger and not welded to the worker layer.

Old entrypoints `bin/run-job.py` / `bin/run-scheduler.py` stay valid.
`bin/run-job.py --backend inprocess` (or `COLLAB_EXECUTION_BACKEND=inprocess.local_v1`) selects the knife-6 inprocess backend; default remains TeleAgent.
