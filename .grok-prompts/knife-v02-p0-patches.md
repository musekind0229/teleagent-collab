# Path-B v0.2 P0 surgical patches (NOT knife 15 features)

Repo tip ~362614b on main. Do NOT clone. No Cloud Agent. No Hermes. No deploy. No glue rewrite. Do NOT re-implement satisfied modules (closed_loop channel≠rework, goal_budget reserve, workdir_claim cancel≠early release, outbox, stale coordinator).

Audit source (already written): `.grok-prompts/v0.2-p0-map.md` — also keep a copy at `docs/framework-v01/v0.2-p0-map.md` in the commit.

## Exactly five fixes

1. **submit_goal content conflict** (`durable_api.py`): same submit_key + different payload fingerprint → `ok=False` reason `submit_content_conflict` (do not silently keep first). Same content → idempotent as today.

2. **permission_view native** (`teleagent_adapter/permission_view.py`): preserve full TA raw under `native` (or merge unknown keys). Do NOT map missing→allow. Hard rules still read public/native for rules only.

3. **plan revision contract ceiling** (`goal_ownership.py`): refuse plan revisions that expand Goal contract auth/budget (`budget`, `allow_*`, `boundaries` expansions). Tightening OK. Expanding → reject. Contract expansion stays unsupported / separate.

4. **get_goal projection** (`durable_api.py`): when ownership store present, attach read-only `ownership` + `plan_revision` (coordinator_id, version).

5. **resolve_decision actor** (`durable_api.py`): require `actor_id` (optional ownership version if Goal has coordinator); refuse if absent or not authorized. Record actor on resolution. Minimal — no identity memory promotion.

## Tests
Extend `test_framework_b14` (1,4,5), b12 (3), b3 or adapter/hard_rules (2). Keep b14/b13/b12 green. `run-job.py --dry-run jobs/examples/hello.charter.yaml` ok.

## Deliverables
- Commit: `fix(framework): v0.2 P0 surgical gaps (submit conflict, native preserve, plan ceiling, get_goal owner, resolve actor)`
- Include `docs/framework-v01/v0.2-p0-map.md` (copy from audit; add a short "patched" note listing the five SHAs after fix)
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print: short+full SHA; which of 5 landed

## Verify
```
PYTHONPATH=src python3 -m unittest src.test_framework_b14 src.test_framework_b12 src.test_framework_b13 -q
PYTHONPATH=src python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
```
