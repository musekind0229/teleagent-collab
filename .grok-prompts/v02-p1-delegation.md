# Path-B v0.2 P1 — minimal delegation semantics + simple caller

Repo tip ~98d58da on main. Do NOT clone. No Cloud Agent. No Hermes/OpenClaw product. No deploy. No glue rewrite. Do NOT re-implement P0-satisfied modules (closed_loop channel≠rework, budget reserve, workdir claim, outbox, stale coordinator, the five P0 surgical fixes). Default TeleAgent `run-job` entry unchanged. You implement; coordinator only accepts. Do NOT start P2.

Design: `/workspace/agent-exec-framework-v0.2.md` §5 / §8 / §11 P1. Prior map: `docs/framework-v01/v0.2-p0-map.md`.

## Goals
Reuse existing Goal/plan. Add **minimal delegation semantics**:

1. **Autonomy scope** — charter/submit declares whether local planning/rework/reassign is allowed (explicit plan mode vs bounded autonomy). Kernel stores it; plan revision respects it.
2. **Submitter identity** — `submit_goal` records submitter id / external goal ref; required for audit; conflict rules stay as P0.
3. **Return-to-upper conditions** — when local coordinator must escalate (over-budget, out-of-scope, insufficient auth); durable surfaces these as pending/decision kinds, not silent retry.
4. **Ownership** — wire claim/handoff already in `goal_ownership` into durable submit/get so a simple caller sees coordinator + version.

## Simple caller (required)
CLI or small script (extend `bin/durable-cli.py` or add `bin/delegate-demo.py`) that can:
- submit a Goal (with submit_key, submitter, autonomy, budget/boundaries)
- list/read events (or outbox/pending notifications)
- resolve one pending decision (with actor_id)
- cancel_goal
- get_report / get_goal

No Bot product install. File-backed is fine.

## Tests
- `src/test_framework_v02_p1.py` (or extend b14): autonomy flag stored; submitter recorded; escalate-style pending; ownership visible on get; CLI path smoke.
- Keep b14/b12 green; hello dry-run ok.
- Doc: `docs/framework-v01/v0.2-p1-delegation.md` with how to run the caller.

## Deliverables
- Commit: `feat(framework): v0.2 P1 minimal delegation + durable caller`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print: short+full SHA; exact commands to run the simple caller; test summary

## Verify
```
PYTHONPATH=src python3 -m unittest src.test_framework_b14 src.test_framework_v02_p1 -q
# or whatever test module you add
PYTHONPATH=src python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
# plus documented durable-cli / demo sequence
```
