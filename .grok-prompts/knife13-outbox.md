# Path-B knife 13 — transactional state + outbox + receiver dedup

Repo tip ~adf6e7e on main. Do NOT clone. No Cloud Agent. No Hermes. No deploy. No glue rewrite. Default TeleAgent entry intact. You implement; coordinator only accepts.

## Goals
1. **State change and event commit in the same transaction** (atomic persist of new state + outbox row).
2. Outbound notifications use a **durable outbox** (pending → sent). Receiver **deduplicates** by event id / delivery key.
3. Cover at least: `task queued→running`; `blocked/queued` due to deps or resources; `goal` completed.
4. Clarify: **at-least-once delivery ≠ external side effect only once** — tests prove crash-replay does not lose pending outbox; duplicate deliveries are deduped on receive.
5. Regressions green; TA default path unchanged.

## Suggested shape (non-binding)
- `src/framework/outbox.py` (or similar): OutboxStore with `append_in_txn`, `claim_pending`, `mark_sent`; EventLog / DedupStore on consumer side.
- Tiny `commit_transition(goal_or_task, new_state, events[])` that writes state + outbox atomically (file-based txn / journal is fine).
- Tests `src/test_framework_b13.py`: crash before mark_sent → replay still delivers; second identical delivery ignored.
- Doc `docs/framework-v01/knife13-outbox-dedup.md`.

## Deliverables
- Commit: `feat(framework): path-B knife13 transactional outbox + dedup`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print: short+full SHA; outbox crash-replay ok; receiver dedup ok

## Verify
1. unittest b13 (+ b12 if quick) green
2. `python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml` ok
3. Demo/test: pending not lost on crash; duplicate delivery deduped
