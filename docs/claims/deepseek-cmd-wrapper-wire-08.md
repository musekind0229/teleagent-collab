# Claim: deepseek-cmd-wrapper-wire-08

- Base: origin/main @ d41e870909851a9f28012b1132cec53a31f73977
- Branch: feat/deepseek-cmd-wrapper-wire-08 (main line: pm/simple)
- Scope set by Windows-side agent Hermes (reply a31, 2026-10-19): Linux writes
  and commits; Windows re-verifies on the same fixed SHA. No patch hand-off.

## Files (this slice writes only these)
- bin/run-deepseek-lead.py
- src/test_deepseek_harness_lead.py
- docs/claims/deepseek-cmd-wrapper-wire-08.md

No change to platform_services/__init__.py, cli.py, paths.py, or the four
Windows platform implementation files. No frozen file touched (app_service,
cancel, schema, agy_account_pool). AGY / Grok / Codex callers are not wired.

## Change
- Product `invoke_dsh` now resolves a CmdWrapper only when the harness binary
  is a Windows `.cmd`/`.bat` target (resolved path or bare name on env PATH),
  then passes it explicitly into `prepare_cli(..., cmd_wrapper=...)`.
- `prepare_cli` itself is unchanged and still rejects `.cmd`/`.bat` without an
  injected wrapper; there is no global auto-wire.
- Non-cmd targets (`.exe`, explicit interpreter prefixes, POSIX) still get
  `cmd_wrapper=None`. `.ps1` stays unsupported. Dangerous cmd characters still
  raise once with no retry and no `shell=True`. A missing `windows_cli` raises
  `PlatformWrapperRequired` (fail-closed).
- Tests: unit cases with `platform="win32"` + injected `cmd_exe`; real
  product→cmd→fake-Python round trips are Windows-only (`os.name == "nt"`).
  Existing "product rejects .cmd without wrapper" / contract cases remain.

## Red/green
- On main (pre-change): new wire tests error (no `_cmd_wrapper_for` /
  `get_cmd_wrapper` on the product module).
- After: Linux target OK; Windows must confirm real cmd/bat round trip and
  no new full-suite failures vs this main.

## Linux evidence (PYTHONPATH=<repo>:<repo>/src)
- TestDeepSeekCmdWrapperWire: OK x3 (skipped=2; real cmd/bat skip on POSIX).
- src/test_deepseek_harness_lead.py: Ran 48, OK (skipped=3).
- src discover: Ran 1113, OK (skipped=12).
- tests discover: Ran 138, OK (skipped=1).
- platform contract: Ran 39, OK (skipped=3).

## Windows evidence
- Pending: Hermes same-SHA re-verify (target true execute green; full run
  relative to this main has no new failures).

## After this slice
Knife 9 (test/agy-round3-fixture-xplat-09) stays queued per Hermes a31. This
slice is limited wiring verification, not a real dsh / account / deploy gate.
