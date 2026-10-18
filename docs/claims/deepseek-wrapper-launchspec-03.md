# Claim: deepseek-wrapper-launchspec-03

- Base: origin/main @ f7a60ca2e61e46b87bd14446cb0ad263e5390c7f
- Branch: feat/deepseek-wrapper-launchspec-03 (main line: pm/simple)
- Scope agreed with Windows-side agent Hermes (reply a18, 2026-10-18).

## Files (this slice writes only these)
- bin/run-deepseek-lead.py
- src/test_deepseek_harness_lead.py
- docs/claims/deepseek-wrapper-launchspec-03.md

No frozen files touched (app_service, cancel, schema, account_pool,
platform_services/*). No CmdWrapper / windows_cli added. No round4 work.

## Change
`invoke_dsh` no longer does `build_dsh_cmd` then `subprocess.run(cmd, ...)`.
It now:
- calls `prepare_cli(harness_argv[0], harness_argv[1:] + ["--profile", "headless", "-"], cwd=tmp, env=os.environ.copy())`;
- starts the child with `subprocess.run(list(spec.argv), ..., cwd=spec.cwd, env=dict(spec.env) if spec.env is not None else None, shell=False)`.
`main()` catches `CliLaunchError` (in addition to TimeoutExpired / OSError) and
returns `_fail(...)` with a clear message. No alternate launch path on failure.
`build_dsh_cmd` and `resolve_harness_bin` are kept (which/isfile stay as
complementary checks; tests still inspect `build_dsh_cmd`).

Acceptance extras covered by tests:
- unit spy: `invoke_dsh` calls `prepare_cli`, and `spec.argv` / `cwd` / `env` /
  `shell=False` drive the real `subprocess.run`;
- e2e: explicit-interpreter `fake_dsh.py` (no shebang / no exec bit) still
  succeeds, non-zero exit → rc 2 + empty stdout, headless argv preserved, no
  `--json`, no `cmd.exe` splice; path-with-spaces case;
- rejection: win32 `.cmd`/`.bat` without `cmd_wrapper` → `PlatformWrapperRequired`;
  `.ps1` without explicit interpreter → `CliLaunchError`; wrapper maps both to
  rc 2 / empty stdout / no invented once|pass.

Explicit Python argv prefix remains a valid path. This slice does **not** claim
`.cmd` / `.ps1` product support.

## Not in this slice
- round4 heartbeat (session attribution / heartbeat timing): knife 4.
- Frozen: app_service, cancel, schema, account_pool; process / file_lock public
  signatures; platform_services implementation files; WIN-WORKER.
- No CmdWrapper on main; no PowerShell support.

## Linux evidence
- src/test_deepseek_harness_lead.py: 37 tests OK (skipped=1, optional live dsh smoke).
- src discover: 1084 OK (skipped=7).
- tests discover: 138 OK (skipped=1).
- Run with `PYTHONPATH=.:src`.

## Follow-up (a19)
Hermes Windows re-verify of 9cd7f0c found two new module failures caused by
hard-coded `/usr/bin/true` preflight fixtures (resolve_harness_bin returned
missing on Windows before CliLaunchError mapping ran). Product probe 3/3
passed. Fixtures now use `sys.executable` (cross-platform) in
`src/test_deepseek_harness_lead.py` only; product code unchanged.
