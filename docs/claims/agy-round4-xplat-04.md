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
- Timing: SLEEP[5] -> SLEEP[8] in the two silent-run cases so a slow child
  start cannot end the run before the 2.8 s observation. Thresholds unchanged.

## Not in this slice
- CancelStopsLeadTests (POSIX process groups / killpg, cancel.py frozen).
- Any product heartbeat logic change.
- No claim that Windows has fd-scan attribution.

## Linux evidence (PYTHONPATH=.:src)
- src/test_retest_round4.py: 14 OK (4 repeated runs OK)
- src discover: 1085 OK (skipped=7) on 3 of 4 runs; 1 run had a single
  failure that did not reproduce (output not captured; not attributed).
- tests discover: 138 OK (skipped=1)
