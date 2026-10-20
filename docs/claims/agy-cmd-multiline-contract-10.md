# Claim: agy-cmd-multiline-contract-10

- Base: origin/main @ 5a86df9ddbe1adda5e602f911ad4f2463afae7cd
- Branch: test/agy-cmd-multiline-contract-10 (main line: pm/simple)
- Scope proposed by Windows-side agent Hermes (reply a36, 2026-10-20):
  investigation only, build a launch-transport contract; do not fix product.
  Linux writes the test and this claim; Windows runs real system cmd + fake
  child and accepts independently. No user decision needed.

## Files (this slice writes only these)
- src/test_agy_cmd_transport.py (new)
- docs/claims/agy-cmd-multiline-contract-10.md (new)

No product code. antigravity_cli_v1.py, platform_services/* (and signatures),
and frozen files (app_service, cancel, schema, account_pool) untouched.
No real agy, model, login, account or service. Switch-lock and the other
existing Windows failures stay on the books; not handled here.

## What is tested
Prompts: single-line, multi-line, blank line inside, Chinese, Chinese multi-line.
Args are product-shaped (`build_agy_argv`: `--output-format=json`,
`--model=...`, `--print=<prompt>`). A probe child writes `sys.argv[1:]` as
ASCII-escaped JSON to a file (no console code page involved).

1. ExplicitPythonTransportTests (all platforms, control):
   `[sys.executable, probe.py, *args]` -> every prompt arrives verbatim.
2. BatchForwarderTransportTests (Windows only): `probe.cmd` = the knife-9
   `@"python" "probe.py" %*` forwarder, launched like the product
   (`Popen([cmd, *args])`, list argv, no shell).
   - single-line and Chinese single-line: expected verbatim.
   - multi-line / blank-line / Chinese multi-line: characterization test pins
     the reproduced truncation (child sees only `--print=<first line>`, flags
     before it intact). Suite stays green; a later product fix needs its own
     red/green gate.
   - same args via explicit Python vs batch, side by side.
3. CmdWrapperBoundaryTests (all platforms, construction only, nothing
   launched): existing `windows_cli.CmdWrapper` rejects CR/LF args with
   CliLaunchError rather than truncating; single-line/Chinese args pass through.
4. InstalledAgyCmdPending: skipped placeholder.

## Reproduced vs not verified
- REPRODUCED (pending Windows confirmation on this SHA): our test batch
  forwarder truncates multi-line arguments at the cmd boundary.
- NOT VERIFIED: the installed `agy.cmd` launcher. Claiming it behaves the same
  needs its concrete launcher text and version (redacted, provided by the
  user or Hermes). Do not run the real agy (no version/doctor/login). Touching
  the real install path or real CLI needs separately agreed scope.
- If a product launch-layer change turns out necessary, submit a concrete plan
  and file list first; no fix method (escaping, stdin, temp file) is promised.

## Linux evidence (PYTHONPATH=<repo>:<repo>/src)
- src/test_agy_cmd_transport.py: Ran 7, OK (skipped=4: 3 Windows-only + pending) x3.
- src discover: Ran 1121, OK (skipped=16).
- tests discover: Ran 138, OK (skipped=1).
- src/test_platform_contract.py: Ran 39, OK (skipped=3).

## Windows evidence
- Pending: Hermes same-SHA run with real system cmd.exe.
