# Path-B knife 12 — Goal coordinator ownership + versioned plan revisions

Repo tip ~92e7520 on main. Do NOT clone. No Cloud Agent. No Hermes ledger. No deploy. No glue rewrite. Default TeleAgent entry intact. You implement; coordinator only accepts.

## Goals
1. **One effective coordinator per Goal** + **ownership version**.
2. Plan revisions go through a **structured proposal**; kernel validates legality then commits.
3. Submissions from a **stale ownership instance are rejected**.
4. **Handoff / succession must bump** the ownership version.
5. Tests: one successful revision; one rejected stale-coordinator submit.
6. Regressions green; TA default path unchanged. No Hermes. No deploy.

## Suggested shape (non-binding)
- `src/framework/goal_ownership.py` or `execution_backend/`: GoalOwnership record `{coordinator_id, version, updated_at}`; `propose_plan_revision` / `commit_revision` with version check; `handoff` bumps version.
- Persist under workdir or `.collab-goal-ownership/<goal_id>.json`.
- Wire lightly into charter/Goal projection if needed; do not rewrite glue main loop.
- Tests `src/test_framework_b12.py`; doc `docs/framework-v01/knife12-goal-ownership.md`.

## Deliverables
- Commit: `feat(framework): path-B knife12 goal ownership + plan revision`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print: short+full SHA; successful revision once; stale coordinator rejected

## Verify
1. unittest b12 (+ b11 if quick) green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
3. Demo/test: revision ok; expired ownership submit denied
