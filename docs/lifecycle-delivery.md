# Durable lifecycle delivery and independent post-run review

This feature is not a business acceptance gate, model grant or automatic maintenance executor. Existing runtime-registration, same-store, caller-permission, resource, review/no-verdict and owner approval gates remain authoritative.

## Event source and safety

The registered GoalService attaches a publisher to the existing durable store persistence boundary. Completed/failed/cancelled goals and pending continuity decisions produce immutable `lifecycle-events/EVENT.json` packets. Identity is the same SHA256 of `[goal_id, terminal_state_or_continuity_decision_id]` used by owner-handoff. No manual wait is needed to create the lifecycle packet. Ordinary task completion, polling/ACK and a process exit code are not goal outcomes. An explicitly observed host exit without a durable outcome is `unknown`, never forged success/failure. This feature does not install a second host/process observer.

Store data is persisted first. A publisher fault records a local blocked health marker without rewriting the business result. On restart the host first validates each immutable event, rebuilds ONLY missing consumer metadata, then classifies pre-existing inflight sends as unknown/blocked before reconciling current persisted snapshots. Corrupt/hash-mismatched events or delivery rows are retained byte-for-byte, never silently replaced; per-event recovery-blocked side records identify product_owner responsibility, and the notification fault does not become a business-startup gate. A crash in the tiny store-to-outbox gap can recover the current durable state; a superseded transient decision during that gap is not claimed reconstructible without a persisted history event. The store remains authoritative; outbox/report/receipt files are side records and do not edit old claims or results.

Packets contain selected engineering fields only: event/goal/task/run identity, runtime root/HEAD/branch, backend name, retained attempt/rework records, structured review status/code, numeric available usage and resource/claim metadata. Artifact hashes and execution identity come from the existing collector's `result.session_receipt` (or retained `native_result.session_receipt`), not an assumed task-manifest producer. The accepted source is `backend_observed_process_and_files` with exact task/run binding, boolean fresh_session and integer returncode; only relative outputs `{relative,bytes,sha256}` are mapped into the manifest. Absolute/traversal/backslash paths, duplicate paths, wrong digests or mismatched identities reject the receipt's manifest as unavailable. Workspace/conversation/raw/private receipt fields are excluded. Bounded prior attempt records use the same whitelist; omitted old records are explicitly marked truncated. A missing run/receipt has unknown observed-run count, not a fabricated one call. Missing/invalid usage stays unavailable, never inferred zero cost. Raw stdout, thought, model self-report, private lists, arbitrary result strings, conversations, memory, credential configuration and unrelated filesystem material are never traversed. Packets stay local; do not upload them wholesale. A goal title is reduced to one bounded line and attachment/control markers are neutralized before QQ text generation.

## Independent consumers

`lifecycle-delivery/EVENT.json` stores separate QQ, PM and report-owner statuses. Three bounded host-owned delivery workers are independent of the business coordinator and each other: slow/busy PM cannot block QQ or goal processing. They are not separate services, model owners or a new platform. HOLD never starts outbound delivery. No route file is installed by merely adding source.

QQ text is exactly three lines: goal, actual durable state (not user acceptance), next step with product_owner responsibility. A confirmed provider success is retained once per event. An uncertain timeout/crash may already have sent remotely: it becomes delivery_blocked, never blindly sent twice. Exactly-once remote side effects across a network/process crash cannot be guaranteed by the current non-idempotent provider interface; local successful sends are deduplicated and ambiguous delivery is explicit.

PM gets only an event/local-packet pointer/hash, not the QQ body or a business approval request. `queued`, `settled` and exit code zero do NOT equal consumption. PM must read/hash-check the packet and explicitly consume, then submit a substantive healthy-no-change or proposed-change report. Report hash is independent. The secretary must read/hash-check that report and consume it independently. No method grants maintenance authority or executes a proposal; healthy runs may need no change. Any future repair needs the existing explicit secretary approval and preserves the original problem's repair/review-round history.

Delivery compensation uses exponential 30-second initial backoff capped at 30 minutes, within a 24-hour first-offer window. It is not a fixed two-attempt message quota. Two consecutive same rejection statuses without new evidence stop blind repetition. A trusted operator may resume the SAME event with actual channel/occupancy/route-change evidence; all attempts/delivery IDs persist. An expired window remains expired and requires owner disposition, not silent window renewal. Queued-but-unconsumed offers also become explicitly blocked at expiry. Interrupted inflight sends are unknown, not reset to pending. Receipts arriving synchronously before the transport returns cannot be overwritten by queued status. No lease deletion or alternate Bot owner is used.

## Approved-route installation, not performed by tests

The operator file is `lifecycle-routes.json` next to the existing local runtime registration (outside Git). It must contain `approved: true`, the absolute installed Hermes executable, and separately approved routes. Example placeholders only:

    {"approved":true,
     "hermes_executable":"ABSOLUTE_HERMES_EXECUTABLE",
     "qq":{"profile":"APPROVED_PROFILE","target":"qq:APPROVED_TARGET"},
     "pm":{"profile":"project-manager"},
     "owner":{"profile":"default"}}

Never guess a QQ ID, inspect credentials/pools or use route discovery that opens private settings. Missing/unapproved/invalid routes remain explicit pending/blocked; they do not create model grants. Route approval is an operator assertion, not cryptographic authorization or an OS sandbox. Review the eligible historical backlog before installing routes: startup reconciliation can expose existing pending/terminal goals, so enabling routes without that review could notify old events. Do not use this to restart a paused goal or manufacture a QQ test.

The installed Hermes `send_cmd.py` supports `send --to TARGET --json --file -` (stdin UTF-8). Its own send operation uses its authorized profile secret environment internally; this feature does not read/print credential files. The Bot channel uses the existing canonical `hermes -p PROFILE chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file PATH`, letting its existing busy ownership gate refuse. It does not clear or acquire a competing owner via a new conversation name. Bot/QQ responses are not copied verbatim into engineering packets. No real send or Bot/model consumer test is part of offline testing.

The host owns delivery while running. After host shutdown, durable pending items remain but no compensation daemon is silently installed. If unattended draining after shutdown is needed, PM must first inventory existing no_agent schedules and register/validate one bounded invocation of the existing lifecycle drain command, not a new daemon or duplicate daily report. No sentinel schedule is modified. Until route binding and that installation decision are verified, do not say end-to-end automatic delivery is enabled.

## Existing entry commands

All use the same existing registered checkout, same state and existing-state checks. No new runner or production bypass is introduced:

    python bin/goal-entry.py --state STATE --existing-state lifecycle status --event-id EVENT
    python bin/goal-entry.py --state STATE --existing-state lifecycle consume --event-id EVENT --consumer pm --sha256 PACKET_SHA --receipt-id ACTUAL_READ_RECEIPT
    python bin/goal-entry.py --state STATE --existing-state lifecycle report --event-id EVENT --sha256 PACKET_SHA --report-file LOCAL_REPORT_JSON --receipt-id PM_REPORT_RECEIPT
    python bin/goal-entry.py --state STATE --existing-state lifecycle consume --event-id EVENT --consumer owner --sha256 REPORT_SHA --receipt-id ACTUAL_SECRETARY_READ_RECEIPT
    python bin/goal-entry.py --state STATE --existing-state lifecycle resume --event-id EVENT --consumer pm --evidence ACTUAL_NEW_CHANNEL_STATE_EVIDENCE
    python bin/goal-entry.py --state STATE --existing-state lifecycle drain --event-id EVENT

Drain performs the production registered/published runtime gate; receipt/status/report operations are local trusted owner actions, not business result changes. An acceptable report includes `conclusion` (`healthy_no_change` or `proposed_change`), nonempty evidence and preferably proposal, alternatives, minimal scope/resources/validation/unknowns. Receipt/report data is local operator input: mere schema-valid text is not proof someone actually read it. Production acceptance must verify the real PM report and secretary consumer, not simply run these commands as a synthetic surrogate.

## Verification and outstanding natural acceptance

Unit tests use synthetic snapshots, sender spies and independent process/network denial. Tests exercise dedupe/restart/partial persist recovery, exact hash rejection, status/report ordering, interrupted/ambiguous delivery, busy-state evidence resume, expiry, parallel QQ with a blocked PM, synchronous consumption races, attachment injection prevention and unchanged maintenance authority. Owned child-process tests run under an independent audit guard, persist the event, restart, read/consume/report a synthetic healthy result and query it through the existing physical CLI. This is not actual PM/secretary Bot consumption.

Final real gates: PM reviews/publishes only the authorized integration branch; approved QQ/Bot route binding; any required no_agent drain schedule inventoried/registered/verified; PM-organized synthetic real Bot read/report/secretary receipt test; then the next already-authorized portal goal naturally verifies one QQ message plus real PM review/secretary consumption. No new goal/model package/QQ test is authorized by this code. All SOUL/shared-skill installation differences, including normalization, still need user approval.
