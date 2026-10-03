# 刀P2: GitHub issue #13 — independent tasks of one Goal are serialized; add bounded concurrent dispatch

Repo: this directory (main, clean). Python stdlib. Never print tokens. Do not commit.

Issue #13 repro (fake backend): one Goal, two queued tasks A/B without dependencies; FakeBackend.start_run records,
observe_run returns busy=true, list_pending_actions empty; three process_goal ticks → started == ['A'], B stays
queued. Cause: AppCoordinator (src/framework/app_service.py) running-task loop returns as soon as one observe_run is
busy, and the queued-task dispatch returns after starting one busy task.
FIRST reproduce this as a failing test (RED), keep a note of it.

Requirements (from the issue):
- Expose actual limits: `max_parallel_per_goal` and `max_parallel_global` (service config: collab-service flags
  `--max-parallel-per-goal` (default 1 → current behaviour preserved unless raised? NO: default 2 per goal,
  4 global) — choose defaults 2/4 and document), plus backend capacity: a backend may declare
  `capabilities()["concurrency"] = {"max_runs": int|"unknown", "limited_by": [...]}`; agy: limited by account pool
  leases (if pool size known use it, else 1); inprocess: unlimited-ish (e.g. 8); windows/linux supervised: 1 per
  desktop (desktop lock) → effectively serial, with reason "desktop_session_lock". Effective capacity = min(of all).
  Report in /v1/capabilities `concurrency: {max_parallel_per_goal, max_parallel_global, backend_max_runs,
  effective, limited_by}` and in goal status `scheduler: {running, queued_ready, capacity, waiting_reason}`.
- Bounded concurrency: A/B independent and capacity>=2 → B starts while A is still busy. C depends on A and B →
  starts only after both succeeded (and handoff from both, using the trusted workspace binding from #14).
  Capacity=1 → still serial, with an explicit waiting_reason ("capacity" ), not a failure.
- Each tick: observe ALL running tasks (one busy must not block others), collect finished ones, then start ready
  tasks up to capacity. Selection + bind_task_run must be atomic (durable layer lock / compare-and-set on task
  status) so concurrent ticks (HTTP thread + background loop) never double-dispatch — test with two threads calling
  process_goal concurrently on a backend whose start_run sleeps.
- Keep existing rules: same working directory queueing (workdir_claim: two tasks with the same workdir never run at
  once; second waits, not fails), dependency order, failed dependency → dependents not started (cancelled/blocked
  as today), budgets (goal wall_sec / max_reworks) still enforced across parallel runs, pending decisions of one task
  do not block unrelated ready tasks but global approvals still gate, cancellation cancels all running runs of the
  goal, persistence/restart recovery keeps already-bound runs (no duplicate start after restart).
- Capacity shortage is never recorded as a permanent business failure.
- Tests (new src/test_goal_concurrency.py, fake backends, no real model): prove two runs OVERLAP (record start/end
  timestamps or a shared "currently running" counter that reaches 2), not just two start records; C after A,B;
  capacity 1 serial; same-workdir serialization; double-tick race; failure of A blocks C but B continues; cancel
  stops both; restart recovery; budget wall_sec exceeded while two runs active → both stopped, goal failed with
  worker_timeout/budget reason.
- Client summary (bin/hermes-collab-request.py) shows `scheduler` block when present. Docs:
  docs/application-api.zh-CN.md. Skill: bump version 0.2.11 → 0.2.12 with one line: same-goal independent tasks may
  run in parallel up to `capabilities.concurrency.effective`; supervised TeleAgent desktops stay serial.
Run `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` (only allowed error: Windows-only
`test_prepare_injects_configured_proxy_and_userprofile`) and `PYTHONPATH=src:. python3 -X utf8 -m unittest discover -s
tests -t .` (all OK). Keep code cross-platform (pathlib; no POSIX-only calls without os.name branches).
