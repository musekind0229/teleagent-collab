# Knife 7 — run-job selects inprocess ExecutionBackend

## Scope
- Old entrypoint `bin/run-job.py` can select a second backend without rewriting glue.
- **Default remains TeleAgent** (`glue.run_job`).
- `--backend inprocess` / `--backend teleagent`, and/or env `COLLAB_EXECUTION_BACKEND=inprocess.local_v1`.
- CLI flag overrides env. Empty / unset → teleagent.
- `--dry-run` still validates the charter and writes reports **without** calling any backend (glue or inprocess).

## Inprocess path
- Thin helper: `src/execution_backend/run_job_wire.py` → `run_file_job_via_public_api`.
- Public API only: `start_run` / `observe_run` / `collect_result`.
- `list_pending_actions` is empty; `reply_permission` stays **unsupported** (never invent once/approve).
- Does **not** parse TeleAgent HTTP (`/session/status`, `/message`, …).
- Unknown names (including Hermes) → clear unsupported error, exit 2.

## Prove
```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 bin/run-job.py --backend inprocess jobs/examples/hello.charter.yaml
# writes hello-from-worker.txt under the run workdir; no :4399 required
```

## Not in this knife
- glue.py rewrite
- Hermes as ledger / execution backend
- deploy
