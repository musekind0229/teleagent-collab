# Path-B knife 10 — workdir/path resource claim (queue or blocked)

Repo tip ~e63ca89 on main. Do NOT clone. No Cloud Agent. No Hermes. No deploy. Do not rewrite glue wholesale. Default TeleAgent entry must stay intact.

## Goal
1. Two jobs targeting the **same workdir / same write path** must **not** run in parallel merely because they are different backend instances.
2. The second job must be **queued** or **blocked**, with a **resource occupancy record** (who holds the claim).
3. Conflicts must **not** silently overwrite artifacts.
4. Jobs that are properly **isolated** (distinct workdirs) must **still** be able to run in parallel.
5. Cover via inprocess path / helper / scheduler-sim as appropriate; keep `bin/run-job.py` default TA behavior.

## Suggested shape (non-binding — verify)
- A small claim registry under `execution_backend/` (e.g. file lock or in-memory+persist for tests): claim(workdir), release, holder metadata.
- Integrate with `two_job_isolation` / `run_inprocess_charter` / optional concurrent helper so same-workdir concurrent start returns blocked/queued instead of racing writes.
- Tests: same workdir → second blocked/queued + occupancy recorded; distinct workdirs → parallel ok; no silent overwrite proof.

## Deliverables
- Code + `src/test_framework_b10.py`
- Doc `docs/framework-v01/knife10-workdir-claim.md`
- Commit: `feat(framework): path-B knife10 workdir claim queue-or-block`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`

## Verify
1. unittest b9/b10 (+ b7/b8 if quick) green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
3. Demo/test: same workdir second job queued/blocked with occupancy; isolated pair still parallel
4. Print short+full SHA, whether same-dir queue/block works

## Success
Same-dir conflict cannot silent-overwrite; claim recorded; isolated parallel still works; pushed.
