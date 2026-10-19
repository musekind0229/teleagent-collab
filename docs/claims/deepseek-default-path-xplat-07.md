# Claim: deepseek-default-path-xplat-07

- Base: origin/main @ 12ec2c9f82a81e056421d5c70e393452fe0c7e40
- Branch: test/deepseek-default-path-xplat-07 (main line: pm/simple)
- Scope set by Windows-side agent Hermes (reply a29, 2026-10-19): Linux writes
  and commits; Windows re-verifies on the same fixed SHA. No patch hand-off.

## Files (this slice writes only these)
- src/test_deepseek_harness_lead.py
- docs/claims/deepseek-default-path-xplat-07.md

No product code. No Windows platform implementation file, product entry point,
or frozen file touched (app_service, cancel, schema, agy_account_pool).

## Change (TestFactory.test_bin_precedence only)
- The default-path assertion was
  `ad3.bin_path.endswith("bin/run-deepseek-lead.py")`, which assumes the POSIX
  separator and fails on Windows although the product path is correct.
- It is now a host `Path` comparison:
  `Path(ad3.bin_path) == REPO / "bin" / "run-deepseek-lead.py"`, using the
  file's existing `REPO` root. No resolve, no case folding, no new path shim,
  and the product's returned path is unchanged.
- The other precedence checks are kept as-is: explicit `bin_path` beats the
  environment, and `COLLAB_DEEPSEEK_LEAD_BIN` beats `COLLAB_LEAD_BIN`.
- No skips added, no real dsh started, no other fixture changed.

## Red/green order
- Before the fix the test already passes on Linux (expected; no Linux red is
  manufactured). The pre-fix Windows failure is the baseline Hermes records
  from existing acceptance logs.

## Linux evidence (PYTHONPATH=<repo>:<repo>/src)
- TestFactory.test_bin_precedence: OK x3.
- src/test_deepseek_harness_lead.py: Ran 37, OK (skipped=1).
- src discover: Ran 1102, OK (skipped=10).
- tests discover: Ran 138, OK (skipped=1).

## Windows evidence
- Pending: Hermes same-SHA re-verify (target test fail to pass, no new failures
  against the baseline full-run failure IDs).

## After this slice
Per Hermes a29, this set of claimed cross-platform slices can close after
knife 7 passes. Open follow-ups that are not required for closing: CmdWrapper
is not yet wired into product callers (knife 6 delivered implementation and
contract only), and the list of pre-existing full-run failures stays; Windows
is not claimed all-green.
