# Governance observation and single daily summary (candidate, not installed)

## Boundaries

The module is a deterministic collector/executor, not another business entry,
model runner, scheduler or PM approval layer. Default CLI mode only observes
exactly four role SOUL paths and three named skills per profile. It never reads
chat, memory, goal state, raw model output, auth, config, .env or backup trees.
No recursive profile scan. Reparse paths and files over 2 MB are refused.

Actual SOUL/skill originals, candidate full text, full diffs, migrations,
reminder-setting candidates and approvals belong in a private local evidence
package outside Git. This repository contains only code and synthetic tests.
All rule installation, including normalization, requires the user's approval.
A missing skill is missing, not implicitly approved. Candidate/preflight hashes
are observations, never approval. Role SOUL hashes need not equal one another;
shared skills compare the same mother, each SOUL its individual approved hash.

## Data contract / counting

Run with Python 3.12+, repository `src` on PYTHONPATH:

    python -B -m framework.governance_daily --profile-home-root ABS_HOME --ledger ABS_LEDGER --ledger-checkpoint ABS_CHECKPOINT --input ABS_CURATED_INPUT --output ABS_REPORT --cron-metadata ABS_CRON_METADATA

All ABS paths above are operator-controlled, local, non-public bindings. Preview
never reads a route or executes a process. It produces a local JSON report and
exactly 10 safe text lines. No attachment directives, raw commands or source
contents go into the message; incidental absolute home paths are masked.

Curated input is engineering metadata, NOT a raw log, session export or product
manifest. Missing/old source date yields explicitly stale/unknown completion,
current-owner and decision summaries, never yesterday's progress relabeled as
current. Evidence/freshness is included. Report date always comes from current
UTC+08:00, not from the last input file. `yesterday`, `current`, `decisions`,
`evidence` are short approved engineering summaries. `date` describes their
observation day, not an instruction to send a historical report.

`metrics` must declare kind `api_call_aggregate`, previous local calendar day
`window_date`, timezone `UTC+08:00`, coverage `complete`, source_reference and
`counts` keyed by default/project-manager/worker/watchdog/sentinel. Only exact
nonnegative integers count (bool/float/negative/missing rejected as unknown).
Session counts, partial coverage or wrong time window are not API counts.
No safe approved aggregate was assumed available: all actual counters stay
unknown until an actual safe adapter/source provides this exact contract.
Mechanical code is no-model; actual Bot consumption is NOT claimed free.

`goals` similarly requires kind `goal_outcome_aggregate`, same yesterday/timezone,
complete coverage, source_reference, and completed/failed exact integer counters.
It does not read original goal state. Otherwise counts are unknown, not zero.

`repositories` requires kind `framework_tree_and_checkout_inventory`,
source_reference, as_of, and exact nonnegative source_copies, fresh,
historical_excluded, other, bare, worktrees and new_shared_worktrees.
Historical source_copies must equal fresh + historical_excluded. Summary labels
last verified vs historical unrescanned counts, other/bare and shared-object
worktrees separately. Marker/directory totals or impossible old counts are
unknown, not fresh full clones. This stage adds a shared worktree, not a clone.

## Stable round ledger

One stable issue_id across names/workers/narrow subtasks; aliases are not new
round budgets. Preserve evidence, results, owner, recovery and every real round.
Round numbers are contiguous starting 1. Beyond 2, every added actual round
requires its own upper by/evidence/approved_round (not a blanket grant for 4).
Unknown old counts stay explicitly unknown, with pause/evidence intact.

`audit_ledger` persists its own local snapshot. Removed/renamed issues, shortened
or rewritten settled history are blocked, not silently accepted as a new ledger.
Only an in_progress round may finalize its result. Changing implementation
branch/worktree is not new identity. Existing archive/ACL failures and original
lifecycle third-round exception must remain in the private seed. This is a daily
accounting check, NOT automatic repair, resource release or permission to resume
paused material review. Missing upper approval produces a local blocked report
and nonzero status owned by PM; it never starts another business resource.

## One daily identity and delivery

`DailyExecutor` binds daily identity + UTC+08 date to one packet SHA under the
existing persistence lock/atomic JSON write. Reserve inflight before calling the
sender. Restart/parallel invocation cannot send again. Unknown/exception/crash
is delivery_blocked (or still inflight until observed), with PM owner and
reconciliation instruction; never blind retry. Same-day changed packet is
refused, not a second notification. New day gets a new identity. Receipts persist
in ONE canonical local receipt root, shared by scheduled/manual invocations.
Transport `sent` is not user-read, owner consumption or product acceptance.

Preview CLI does not import a sender. `--send` requires the real published /
registered runtime gate, approved existing route file, receipts, current-day
report and checked scheduler metadata; multiple matching jobs refuse delivery.
It reuses HermesLifecycleSender's explicit default-profile qqbot route contract.
Worker tests use synthetic sender/guard injection only; actual runtime refusal
has a separate unpatched negative control. No real QQ/Bot/model tests ran.
Do not point development worktree at production route; tests are not a second
registered runtime.

## PM installation / rollback / exact verification protocol

1. Independently review this commit and private hashes/diffs/ledger. Rules and
   reminder settings stay uninstalled until user confirms EVERY difference.
   This does not block report/ledger acceptance. Do not reset old rounds.
2. Integration/publish requires secretary-coordinated protected-host stop/restart
   first. Worker does not merge, push or deploy. Until then launcher is inert.
3. Refresh ONLY safe cron metadata for all five profiles and canonical runtime
   registration. List before deciding IDs. Detect this daily_id in job name or
   purpose and the canonical `collab_governance_daily.py` script. Existing
   sentinel security summary and lifecycle pump are NOT this daily; do not edit.
   With any existing same executor, update that owner/job rather than duplicate.
4. PM creates local APPROVED-DAILY-BINDING.json beside the inert launcher only
   after review. It has approved=true, daily_id=collab-v2-comprehensive-daily,
   timezone=UTC+08:00, checkout_root=registered published checkout and absolute
   paths named profile-home-root, ledger, ledger-checkpoint, input, output,
   cron-metadata, receipt-root, route-file; approved-baseline remains absent
   until user approval. Bind route path only; no target in Git or worker input.
   Use the SAME checkpoint and receipt-root across restarts/launchers. No copying
   old baseline into a newly blank ledger to wash rounds.
5. With the installed cron API, create/update ONE paused no_agent job after a
   fresh list. Explicit fields: name=collab-v2-comprehensive-daily,
   schedule='0 9 * * *', no_agent=true, script=absolute local
   collab_governance_daily.py, deliver='local', failure_deliver='bot-chat:default',
   enabled_toolsets=[], prompt='Approved deterministic comprehensive daily'.
   The script itself sends through the existing approved route; local cron
   delivery prevents a second QQ copy. Do NOT use deliver=origin/all or attach
   a second notification channel to its stdout. No model pin or new account.
   Create paused=true with reason 'PM installation validation pending' if new;
   no resume until validated. This document does not execute that create.
6. Verify scheduler local timezone is UTC+08:00 (host timezone matters for cron),
   next_run is next local 09:00; read back exact job, count this executor across
   five profiles, no_agent/script/delivery/failure target. Preserve scheduler
   rollback metadata and canonical root. Runtime must pass published gate.
7. First exercise the committed synthetic unit + physical tests; then preview
   with safe actual hashes/curated metadata. Refresh no-chat aggregate sources
   or leave unknown. Ledger errors stop delivery, leave PM-owned blocked report.
   Do not run a production QQ test under worker authorization. Any explicit
   actual scheduler/QQ acceptance is for PM under its separate approval only.
8. After approved activation, verify actual scheduler tick exit/one <=10-line
   send/receipt and no repeat for same day. Readback transport ID, cron job and
   receipt separately; never claim actual user consumption without evidence.
   Failures notify the existing owner via failure_deliver; preserve blocked
   receipt. If integration/tick fails, pause THIS job only, reconcile unknown
   delivery, retain its binding/receipt/ledger; do not alter sentinel, restore
   old rules without user consent, clear locks or retry potential duplicate.

The local launcher template calls `scheduled_main(binding)` with those exact
arguments; physical tests execute the same code path twice across processes,
with network/process denial and synthetic transport, proving one spy invocation.
Actual installation, safe API-count source, real scheduler tick and naturally
occurring business-event acceptance are separate remaining gates.
