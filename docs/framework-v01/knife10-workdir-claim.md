# Knife 10 — workdir/path resource claim (queue or block)

## Scope
- Two jobs targeting the **same resolved workdir** (or the same write path under it) must **not** run in parallel merely because they use different `InProcessExecutionBackend` instances.
- The second job is **blocked** (default) or **queued**, with a **resource occupancy record** naming the holder.
- Conflicts must **not** silently overwrite artifacts.
- Jobs with **distinct workdirs** still run in parallel (knife 9 isolation still holds).
- Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No Hermes ledger. No deploy. No Cloud Agent.

## Claim rule

| situation | result |
| --- | --- |
| same resolved workdir, concurrent start | second **blocked** or **queued**; occupancy names the holder |
| same workdir, sequential (first released) | second may run |
| distinct workdirs | both claimed independently; parallel OK |
| distinct backend/process ids, shared workdir | **not** sufficient — still a conflict |

Knife 9's `prove_shared_workspace_collides` remains the unclaimed counterexample (two `start_run`s overwrite). Knife 10 wraps the write with a claim so the second start is refused and the first body stays.

## Occupancy record
Written to `<workdir>/.collab-workdir-claim.json` while held (plus an adjacent flock file). Fields:

- `holder_id`, `job_name`, `backend_id`, `backend_instance_id`
- `pid`, `thread_id`, `claimed_at` / `claimed_at_iso`
- `write_paths`, `status=held`, `waiters[]`

A refused job's result includes `state=blocked|queued`, `occupancy` (the holder), and `workdir_claim`.

## Code
- Registry: `src/execution_backend/workdir_claim.py`
  - `WorkdirClaimRegistry.claim` / `release` / `occupancy`
  - in-memory occupancy for threads (Linux `flock` is per-process)
  - `fcntl.flock` + JSON for cross-process
  - `run_two_jobs_with_claims`, `start_run_with_workdir_claim`
  - `prove_same_workdir_no_silent_overwrite`
  - `simulate_scheduler_workdir_claims` (one tick: free workdirs start, occupied stay queued)
- Wire: `run_inprocess_charter` claims the workdir for the whole closed loop
- Isolation helper: `run_two_jobs_claimed` (shared workdir → claim, not `IsolationError`)
- Tests: `src/test_framework_b10.py`

`bin/run-job.py` default remains TeleAgent. `--backend inprocess` inherits the claim. `--dry-run` still does not touch a backend.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 -m test_framework_b10
python3 -m test_framework_b9
# same workdir: parent holds claim → child inprocess run is blocked, occupancy.holder_id set
# distinct workdirs: iso-a / iso-b still finish in parallel
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy / Cloud Agent
- changing default TeleAgent `bin/run-job.py` behavior
