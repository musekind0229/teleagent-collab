# Knife 9 — two-job isolation (inprocess, by workdir)

## Scope
- Two **independent** charters run with `--backend inprocess`.
- Each job gets its **own workdir**. Isolation is the workdir pair, not “two process/backend instances.”
- Jobs must **not** write the same relative artifact path in a shared root in a way that collides.
- Serial and concurrent (threads) both finish without stomping each other.
- Default TeleAgent entry (`bin/run-job.py` without `--backend inprocess`) unchanged. No glue rewrite. No Hermes ledger. No deploy. No system-install work.

## Isolation rule
A second `InProcessExecutionBackend()` (or a second OS process) that still uses the same workspace path is **not** isolation: both write `workdir / relative` and the later write wins.

Required:

| check | pass |
| --- | --- |
| `workdir_a != workdir_b` (resolved) | required |
| artifact unique to A not present in B | required |
| artifact unique to B not present in A | required |
| distinct backend/process ids alone | **not** sufficient |

The helper `run_two_jobs_isolated` raises `IsolationError` if the two workdirs resolve to the same path, before any write.

## Code
- Examples: `jobs/examples/iso-a.charter.yaml` → `iso-a.txt`; `jobs/examples/iso-b.charter.yaml` → `iso-b.txt`
- Helper: `src/execution_backend/two_job_isolation.py`
  - `run_two_jobs_isolated` (serial or `concurrent=True` threads)
  - `prove_shared_workspace_collides` (two instances, one workdir, same relative path)
- Tests: `src/test_framework_b9.py`
- Wire: existing `run_inprocess_charter` / `bin/run-job.py --backend inprocess --workspace <dir>`

Scheduler dry/sim isolation is unchanged (`docs/parallel-scheduler.md`); this knife does not expand it or system-install.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 -m test_framework_b9
# or explicit workdirs:
python3 bin/run-job.py --backend inprocess --workspace /tmp/iso-a \
  jobs/examples/iso-a.charter.yaml
python3 bin/run-job.py --backend inprocess --workspace /tmp/iso-b \
  jobs/examples/iso-b.charter.yaml
# expect: /tmp/iso-a/iso-a.txt exists; /tmp/iso-b/iso-b.txt exists;
#         neither file appears in the other workdir
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend / task source
- deploy
- system-install / expanding scheduler live path
