# Path-B knife 9 — stage-3 thin: two-job isolation (inprocess)

Repo tip ~12b0f92 on main. Do NOT clone. No Cloud Agent. No Hermes ledger. No deploy. No system-install work.

## Goal
Two **independent** charters run with `--backend inprocess` (and/or scheduler dry/sim if natural):
1. Each job gets its **own workdir**.
2. They must **not** write the same relative artifact path in a shared root in a way that collides; isolation by separate workdirs is required.
3. "Different process instances" alone is **not** enough to claim isolation if they share a workspace path.
4. Prove both can finish **serially and/or concurrently** without stomping each other.
5. Default TeleAgent entry (`bin/run-job.py` without inprocess) unchanged. Regressions green.

## Deliverables
- Two example charters under `jobs/examples/` (distinct names + distinct artifact filenames), e.g. `iso-a.charter.yaml` / `iso-b.charter.yaml` writing `iso-a.txt` / `iso-b.txt`.
- Helper or test that runs both with separate workdirs and asserts:
  - workdir_a != workdir_b
  - artifact of A not in B's workdir and vice versa
  - optional concurrent run (threads) still isolated
- If scheduler is used: only as needed for isolation proof; do not expand system-install.
- Doc: `docs/framework-v01/knife9-two-job-isolation.md`
- Tests: `src/test_framework_b9.py`
- Commit: `feat(framework): path-B knife9 two-job inprocess isolation`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`

## Verify
1. unittest b6–b9 green (or at least b7–b9 + relevant)
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
3. Run A and B with `--backend inprocess` separate workdirs (or via test) — both ok, isolated
4. Print short+full SHA, whether isolation succeeded (serial + concurrent if tested)

## Success
Two inprocess jobs isolated by workdir/artifacts; TA default intact; pushed.
