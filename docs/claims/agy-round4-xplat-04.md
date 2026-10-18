# Claim: agy-round4-xplat-04

- Base: origin/main @ af3691084222a051271642c1ab6beb1072841e1c
- Branch: feat/agy-round4-xplat-04 (main line: pm/simple)
- Scope proposed by Windows-side agent Hermes (reply a18 item C, 2026-10-18):
  Linux owns the cross-platform fake AGY fixture and timing assertions;
  Windows re-verifies on the same SHA.

## Files (this slice writes only these)
- src/test_retest_round4.py
- docs/claims/agy-round4-xplat-04.md

No product code. No frozen files touched (app_service, cancel, schema,
account_pool, platform_services/*, antigravity_cli_v1 / agy_session_attribution).

## Change (SessionAttributionTests only)
- The fake agy is a plain `fake_agy.py` (no shebang, no exec bit). The
  `#!/bin/sh` `fake_agy` wrapper is gone.
- Launch prefix comes from `prepare_cli(script, [], interpreter=sys.executable)`.
  While the test runs, `antigravity_cli_v1.build_agy_argv` is wrapped (mock,
  test-only) so `bin_path` is swapped for that prefix; all other argv is the
  product's own.
- Child env on Windows also carries SYSTEMROOT/WINDIR/TEMP/TMP/PATHEXT/COMSPEC
  when present (Python children need SYSTEMROOT). Linux env is unchanged.
- Runs are cancelled and reaped (`proc.wait`) inside the temp dir so Windows can
  delete it.
- Class-level `skipUnless(_POSIX)` removed. Per test:
  - `test_without_fd_scan_two_runs_sharing_home_are_ambiguous`: all platforms
    (fd scan forced off, the Windows path).
  - `test_unused_reason_reaches_goal_progress`: all platforms (pure).
  - new `test_fake_agy_launch_is_explicit_interpreter`: all platforms; asserts
    argv starts `[sys.executable, fake_agy.py]`, no shebang, no /bin/sh.
  - `..._other_process_session_file_...` and `..._holding_its_own_session_file_...`:
    `skipUnless(fd_scan_available())` — they assert `/proc` fd attribution
    (`held open by another process`, `open_handle`), which Windows does not have.
- Heartbeat freshness (`..._holding_its_own_session_file_keeps_heartbeat`,
  after Hermes a21): asserted before `_reap`, so cancel/wait time is not
  counted. `last_heartbeat_at` is whole-second, so the check is
  `hb >= floor(observed_at - 1)` (true age under 1 s at observe); a stale
  start-time heartbeat (~2.5 s) still fails. Root cause of the earlier Linux
  flake: traceback showed `1.0096 not less than 1.0` with an integer-second
  stamp, i.e. truncation, not a slow heartbeat.
- Timing: SLEEP[5] -> SLEEP[8] in the two silent-run cases so a slow child
  start cannot end the run before the 2.8 s observation. Thresholds unchanged.

## Not in this slice
- CancelStopsLeadTests (POSIX process groups / killpg, cancel.py frozen).
- Any product heartbeat logic change.
- No claim that Windows has fd-scan attribution.

## Linux evidence (PYTHONPATH=<repo>:<repo>/src)
- 0cfda46: round4 failed 1 of 6 and 2 of 10 runs, always
  `test_run_holding_its_own_session_file_keeps_heartbeat` (`1.0096 not less
  than 1.0`, `1.0 not less than 1.0`); src 1085 OK on 3 of 4 runs.
- This revision: src/test_retest_round4.py 14 OK on 20 of 20 runs;
  src discover 1085 OK (skipped=7) on 2 of 2 runs; tests discover 138 OK
  (skipped=1). Logs kept on the Linux side.
