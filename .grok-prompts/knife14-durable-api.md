# Path-B knife 14 — durable layer minimal API

Repo tip ~ae5f71c on main. Do NOT clone. No Cloud Agent. No Hermes. No deploy. No glue rewrite. Default TeleAgent entry intact. You implement; coordinator only accepts.

## Goals
Minimal **durable/perpetual layer** interface (file or CLI ok; **do not bind to a transport**):

1. `submit_goal` — idempotent via submit key; duplicate submit must **not** open a second Goal
2. `get_goal` — read current Goal snapshot
3. `resolve_decision` — resolve **exactly one** pending decision at a time; forbid fuzzy batch of unrelated actions
4. `cancel_goal` — stop accepting new child tasks + request terminate in-flight; **`cancel_requested` ≠ `cancelled`**
5. `get_report` — report snapshot

Kernel must **not** promote identity/memory (no “晋升身份记忆”).

## Tests (required)
- Duplicate `submit_goal` with same key → same goal, no double-open
- `resolve_decision` rejects multi-unrelated batch
- After cancel: state shows `cancel_requested` before terminal `cancelled` (distinct)

## Suggested shape (non-binding)
- `src/framework/durable_api.py` (+ optional `bin/durable-cli.py` thin wrapper)
- Persist under workdir / `.collab-durable/`
- Tests `src/test_framework_b14.py`; doc `docs/framework-v01/knife14-durable-api.md`

## Deliverables
- Commit: `feat(framework): path-B knife14 durable layer minimal API`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print: short+full SHA; five-entry test results

## Verify
1. unittest b14 (+ b13 if quick) green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
