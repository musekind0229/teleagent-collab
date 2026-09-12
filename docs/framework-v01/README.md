# Framework v0.1 (path B)

Boss locked **B: evolve collab kernel** (2026-09-12).

- Contracts (versioned schemas): [`../../contracts/`](../../contracts/)
- Phase 0/1 comparison notes (frozen decision aid): this folder
- Hermes is **not** the dispatch ledger and not welded to the worker layer.

Old entrypoints `bin/run-job.py` / `bin/run-scheduler.py` stay valid.
`bin/run-job.py --backend inprocess` (or `COLLAB_EXECUTION_BACKEND=inprocess.local_v1`) selects the knife-6 inprocess backend; default remains TeleAgent.
Knife 8 adds an inprocess stage-2 closed loop: independent `artifact_review` + same-Task new-Run rework (`docs/framework-v01/knife8-inprocess-closed-loop.md`).
Knife 9: two independent inprocess jobs isolated **by workdir** (not by process instance) — `docs/framework-v01/knife9-two-job-isolation.md`.
Knife 10: same workdir / write path is **claimed** (queue or block) with occupancy; isolated workdirs still run in parallel — `docs/framework-v01/knife10-workdir-claim.md`.
Knife 11: Goal-level total budget (reserve / reconcile, child costs roll up, new ids do not reset) and Task `depends_on` (unready stay queued) — `docs/framework-v01/knife11-goal-budget-deps.md`.
Knife 12: one effective coordinator per Goal + ownership version; plan revisions via structured proposal (kernel legality then commit); stale instance submits rejected; handoff bumps version — `docs/framework-v01/knife12-goal-ownership.md`.
Knife 13: state change + outbox row in one journal txn; durable outbox pending→sent; receiver dedup by event id / delivery key; crash-replay does not lose pending; at-least-once ≠ exactly-once side effect — `docs/framework-v01/knife13-outbox-dedup.md`.
Knife 14: durable/perpetual Goal layer (`submit_goal` / `get_goal` / `resolve_decision` / `cancel_goal` / `get_report`); submit-key idempotency; one decision per resolve; `cancel_requested` ≠ `cancelled`; kernel does not promote identity/memory — `docs/framework-v01/knife14-durable-api.md`.
v0.2 P1: minimal delegation on those five ops (autonomy scope, submitter identity, return-to-upper pending, ownership on get) plus a file-backed caller — [`v0.2-p1-delegation.md`](v0.2-p1-delegation.md).
