# Claim: agy-model-fixture-xplat-05

- Base: origin/main @ 1a5ae5c61352bb4aa7f574b8a1a3bcecfe570236
- Branch: feat/agy-model-fixture-xplat-05 (main line: pm/simple)
- Scope proposed by Windows-side agent Hermes (reply a24, priority 1,
  2026-10-18): Linux owns the fixture change; Windows re-verifies on the same SHA.

## Files (this slice writes only these)
- src/test_agy_model_select.py
- docs/claims/agy-model-fixture-xplat-05.md

No product code. No shared helper changed (test_contract_render.py untouched;
only its `_FAKE_AGY` body is imported). No frozen files touched (app_service,
cancel, schema, account_pool, platform_services/*, antigravity_cli_v1).

## Change (ModelPrecedenceTests only)
- The three spawn tests (`test_charter_model_reaches_spawn`,
  `test_env_model_without_charter`, `test_builtin_default_without_env_or_charter`)
  no longer use `_write_fake_agy` (POSIX exec-bit file / Windows `fake-agy.cmd`,
  which the launch guard rejects in isolated acceptance).
- The fake is a plain `fake_agy.py` (shared `_FAKE_AGY` body, shebang line
  dropped, no exec bit, no `.cmd` or `/bin/sh` shim).
- Launch prefix comes from `prepare_cli(script, [], interpreter=sys.executable)`.
  During the run, `antigravity_cli_v1.build_agy_argv` is wrapped (mock,
  test-only) to swap `bin_path` for that prefix; every other argv element,
  including `--model=`, is the product's own. A real child process still runs
  and writes its argv; model assertions are unchanged.
- Extra assertion: the child's `argv[0]` is `fake_agy.py` (it really ran).
- New all-platform `test_fake_agy_launch_is_explicit_interpreter`: argv starts
  `[sys.executable, fake_agy.py]`, script has no shebang, no `fake-agy.cmd`.
- The launch guard is not relaxed; the account pool is not touched.

## Not in this slice
- Windows CmdWrapper (`windows_cli.py`), queued as feat/windows-cmd-wrapper-06.
- DeepSeek default-path assertion, queued as test/deepseek-default-path-xplat-07.
- Any other pre-existing failure (account lease, PID liveness, cancel).

## Linux evidence (PYTHONPATH=<repo>:<repo>/src)
- src/test_agy_model_select.py: OK x5 (spawn tests run, not skipped).
- src discover: Ran 1087, OK (skipped=7).
- tests discover: Ran 138, OK (skipped=1).

## Windows evidence
- Pending: Hermes same-SHA re-verify.
