# Knife 6 — second ExecutionBackend (inprocess.local_v1)

## Scope
- New package `src/execution_backend/`: Protocol + `InProcessExecutionBackend`
- Public surface: `start_run` / `observe_run` / `collect_result` / `list_pending_actions` / `reply_permission` / `cancel`
- `observe_run` keys align with `teleagent_adapter.run_observe`
- Permissions: empty list; `reply_permission` → **501 unsupported** (never invent once/reject)
- Prove mini file job via `run_file_job_via_public_api` / `bin/run-inprocess-job.py`
- Ledger remains collab. Hermes is **not** offered as this backend.
- Old `bin/run-job.py` TeleAgent path unchanged. No glue rewrite. No deploy.

## Prove
`python3 bin/run-inprocess-job.py jobs/examples/hello.charter.yaml` → ok using only public API.
