# Path-B knife 7 — run-job selects inprocess ExecutionBackend

Repo: /workspace/teleagent-collab (already on main tip ~9b21c19). Do NOT clone. Do NOT use Cloud Agent / Hermes ledger.

## Goal
Make old entrypoint `bin/run-job.py` able to select second backend `inprocess.local_v1` via **CLI flag and/or env**, while **default remains TeleAgent**.

When backend is inprocess:
- Main path must only use ExecutionBackend public API: `start_run` / `observe_run` / `collect_result` (and empty `list_pending_actions`; never invent permission approvals — `reply_permission` stays unsupported).
- Do **not** parse TeleAgent HTTP (/session/status, /message, etc.) on that path.

When default / TeleAgent / `--dry-run`:
- Preserve existing behavior (glue path). Dry-run and TA regressions must stay green.

## Constraints
- Surgical changes — do NOT rewrite glue.py wholesale.
- No deploy. No Hermes as task source of truth.
- Missing capability → unsupported (501 / clear error), never pretend once/approve.
- Prefer extending `bin/run-job.py` + thin helper under `src/` (e.g. wire to `execution_backend.run_file_job_via_public_api` or equivalent).
- Add a short doc `docs/framework-v01/knife7-run-job-backend-flag.md`.
- Add/adjust unittest covering flag/env selection + hello inprocess via run-job.

## Suggested UX (pick one coherent design)
- Flag: `--backend inprocess` / `--backend teleagent` (default teleagent)
- and/or env: `COLLAB_EXECUTION_BACKEND=inprocess.local_v1`

## Verify before finish
1. `python3 -m unittest src.test_framework_b6 src.test_framework_b5 src.test_p0_security -v` (and any new tests) green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` → ok (TA dry path)
3. `python3 bin/run-job.py --backend inprocess jobs/examples/hello.charter.yaml` (or env equivalent) → ok, writes hello-from-worker.txt under run workdir, **without** needing :4399
4. Commit on main with message like `feat(run-job): path-B knife7 select inprocess ExecutionBackend`
5. Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
6. Print final: session summary, short SHA, full SHA, test results, hello inprocess result JSON snippet

## Success criteria
- Default unchanged; inprocess selectable; public API only on inprocess path; pushed to origin/main.
