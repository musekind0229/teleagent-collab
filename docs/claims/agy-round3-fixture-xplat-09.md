# Claim: agy-round3-fixture-xplat-09

- Base: origin/main @ 17f56d70cab492eea50ea943925a30b2691b97da
- Branch: test/agy-round3-fixture-xplat-09 (main line: pm/simple)
- Scope proposed by Windows-side agent Hermes (reply a34, 2026-10-20): Linux owns
  the fixture change; Windows re-verifies on the same SHA. No user decision needed.

## Files (this slice writes only these)
- src/test_retest_round3.py
- docs/claims/agy-round3-fixture-xplat-09.md

No product code. No frozen files touched (app_service, cancel, schema,
account_pool). antigravity_cli_v1.py and platform_services/* untouched.

## Root cause (Hermes a34, Windows, main 17f56d7, 3 runs: 17 pass / 4 fail / 7 skip)
The product's `--print=` argument is multi-line. `fake_agy.cmd` forwarded it via
`%*`, so the child Python saw only the first line; SLEEP/SESSION/WRITE were lost
and the fake exited at once. Failing tests:
- AgyStaleTests.test_live_but_silent_agy_heartbeat_ages
- AgyStaleTests.test_session_activity_keeps_heartbeat_fresh
- AgyStaleTests.test_goal_progress_goes_stale
- PhaseVisibilityTests.test_acceptance_failure_is_testing_phase
Not a proven heartbeat product bug; thresholds stay as they are.

## Change
- New `_xplat_backend(case, root)`: plain `fake_agy.py` (no shebang, exec bit,
  `.cmd` or /bin/sh shim); launch prefix from `prepare_cli(script, [],
  interpreter=sys.executable)`; `build_agy_argv` wrapped (test-only mock, stopped
  via addCleanup) to swap only `bin_path`; the product's full argv and multi-line
  prompt reach a real child process. observe is not mocked. Windows child env
  keeps SYSTEMROOT/WINDIR/TEMP/TMP/PATHEXT/COMSPEC (same list as round4).
- The four tests above use it. Fixture preconditions added:
  SLEEP cases assert the fake worker is still alive when observed;
  SESSION case asserts the session file is created and grows.
- `_reap_runs(be)` in `finally`: cancel live runs, wait the process, join the
  stdout/stderr reader threads.
- New all-platform `test_fake_agy_launch_keeps_multiline_prompt`: argv starts
  `[sys.executable, fake_agy.py]`, the `--print=` prompt is multi-line and
  contains the instruction, no `.cmd` / `/bin/sh` / shebang.
- Business assertions and thresholds unchanged (no widened tolerance).
- `_backend` / `_fake_agy` kept for the POSIX-only orphan tests and the
  prompt-only warning tests (which do not depend on multi-line forwarding).

## Separate product risk (not in this slice)
A Windows batch boundary can truncate multi-line arguments. With a Python
fixture this slice proves heartbeat/session semantics only; it does NOT claim
that multi-line transport through a real `agy.cmd` is verified. If that must be
supported end to end, open a separate product launch-contract investigation and
agree on shared product file scope first.

## Linux evidence (PYTHONPATH=<repo>:<repo>/src)
- src/test_retest_round3.py: Ran 29, OK x3.
- RED probe (scratch copy only, not committed): truncating every argv element to
  its first line (emulating `%*`) makes the 4 tests plus the new launch test fail,
  with the fixture-precondition messages ("fake agy exited early", "never wrote
  its session file"), matching the Windows symptoms.
- src discover: Ran 1114, OK (skipped=12).
- tests discover: Ran 138, OK (skipped=1).
- src/test_platform_contract.py: Ran 39, OK (skipped=3).

## Windows evidence
- Pending: Hermes same-SHA re-verify.
