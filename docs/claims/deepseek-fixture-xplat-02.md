# Claim: deepseek-fixture-xplat-02

- Base: origin/main @ 042cbf390a4019f6058b3c59e66de16ad6832c15
- Branch: feat/deepseek-fixture-xplat-02 (main line: pm/simple)
- Scope agreed with Windows-side agent Hermes (reply a16, 2026-10-18).

## Files (this slice writes only these)
- docs/claims/deepseek-fixture-xplat-02.md
- src/test_deepseek_harness_lead.py

No product wrapper, lead adapter, or CLI entry point is touched. No new
fixture helper file was needed (the fake stays inline as `_FAKE_DSH`).

## Change
`TestWrapperDshInvoke.test_fake_dsh_script_success_and_failure` no longer
relies on a POSIX shebang or exec bit:
- the fake is written as `fake_dsh.py` with no shebang; on POSIX the test
  sets mode 0644 and asserts it is NOT executable;
- the launch argv is built with
  `prepare_cli(fake, [], interpreter=sys.executable)` (asserts
  `wrapper_kind == "interpreter"`, `shell is False`);
- that argv reaches the real wrapper process through the existing
  `COLLAB_DEEPSEEK_HARNESS_BIN` input, quoted with `shlex.join` (round-trip
  asserted, so Windows backslash paths and spaces survive the wrapper's
  `shlex.split`);
- the wrapper itself spawns interpreter + fake via its own `subprocess.run`
  argv list; nothing in the launch chain is mocked.
All original assertions are kept (success -> rc 0 + bound `once`;
non-zero exit -> rc 2 + empty stdout; `--profile headless -`, no `--json`;
headless task text on stdin). Added: the fake's argv[0] is `fake_dsh.py`
and the remaining argv is exactly `--profile headless -`. No platform skip
was added.

## Gap to note (not fixed here)
`bin/run-deepseek-lead.py` does not call `prepare_cli` or accept a
`LaunchSpec`; it accepts an argv prefix via `COLLAB_DEEPSEEK_HARNESS_BIN`
(shlex-split). So the interpreter is explicit and the launch is argv-only,
but the product does not itself enforce the `prepare_cli` rules. Wiring the
wrapper to `prepare_cli` would touch a product file and is left for a
separate, agreed slice if wanted.

## Not in this slice
- round4 heartbeat test (session attribution / heartbeat timing): separate
  slice; Windows still has not executed that case; a skip is not a pass.
- Frozen: app_service, cancel, schema, account_pool; process / file_lock
  public signatures and semantics; no safety check disabled for the fixture.

## Linux evidence
- RED (old fixture with the exec bit removed, simulating Windows): wrapper
  rc 2, `dsh spawn failed: [Errno 13] Permission denied`.
- GREEN: src/test_deepseek_harness_lead.py 31 tests OK (skipped=1, the
  optional live dsh smoke); src discover 1078 OK (skipped=7);
  tests discover 138 OK (skipped=1). Run with `PYTHONPATH=.:src`.
