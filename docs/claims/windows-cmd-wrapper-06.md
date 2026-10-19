# Claim: windows-cmd-wrapper-06

- Base: origin/main @ 7d5bc32347e35d6c8c73e922a0cbdfdef72429d4
- Branch: feat/windows-cmd-wrapper-06 (main line: pm/simple)
- Scope agreed with Windows-side agent Hermes (reply a26, 2026-10-18):
  Linux writes contract tests + this claim first (red SHA); Windows owns
  `windows_cli.py` and delivers a patch; Linux applies, both sides re-verify
  the final SHA, then merge.

## Files

### This SHA (Linux, contract-first; expected RED until Windows patch)
- docs/claims/windows-cmd-wrapper-06.md
- src/test_platform_contract.py (add `WindowsCmdWrapperContractTests` only)

### Windows delivers (patch only; not in this SHA)
- src/platform_services/windows_cli.py

No changes to cli.py, __init__.py, paths.py, frozen files (app_service,
cancel, schema, agy_account_pool), or product wiring.

## Interface (Hermes a26; reuse existing contract)

```python
# platform_services/windows_cli.py (Windows-owned)
class CmdWrapper:
    def __init__(self, *, cmd_exe: str | None = None):
        # Production: CmdWrapper() resolves System32\cmd.exe (not PATH/COMSPEC).
        # Tests may inject cmd_exe so Linux can assert argv structure without
        # a real Windows System32.
        ...
    def wrap(
        self,
        script: str,
        args: tuple[str, ...],
        cwd: str | None,
        env: Mapping[str, str] | None,
    ) -> LaunchSpec:
        # wrapper_kind == WRAPPER_WINDOWS_CMD
        # shell == False (always)
        # argv: <cmd.exe>, "/d", "/v:off", "/s", "/c", <command payload…>
        # cwd/env preserved as passed (env=None vs explicit env kept distinct)
```

Rules (conservative support set; no “escape anything” promise):
- Accept only `.cmd` / `.bat` (suffix case-insensitive). Other suffixes → `CliLaunchError`.
- Support ordinary args, empty args, Unicode (e.g. Chinese), and paths with spaces.
- Reject in script path and in every arg: NUL, CR/LF, `"`, and `^ & | < > % ! ( )` → `CliLaunchError`.
- Reject args that end with `\` (quote-closure / argv decode ambiguity). Internal `\` in ordinary paths OK.
- No `CALL`, no `shell=True`, no PowerShell, no fallback launch mode after failure.
- Locate `cmd.exe` from the Windows system directory only; if `env` is given, do not
  silently borrow parent env for missing keys; fail clearly when unresolvable.
- Construct `LaunchSpec` only: no process spawn, no temp files, no input mutation.
- Quoting in the `/c` payload is Windows-owned and must be proven by real `cmd.exe`
  roundtrips on Windows (not string-shape alone).

## prepare_cli wiring (unchanged product behavior)
- Explicit `cmd_wrapper=` uses the existing hook.
- Without injection, `.cmd/.bat` still raises `PlatformWrapperRequired` even if
  `windows_cli` exists (no auto-wire from module presence).

## Tests added (`WindowsCmdWrapperContractTests`)

Cross-platform (must fail with AssertionError if `windows_cli` is missing — never skip):
- construct / signature / LaunchSpec type / `wrapper_kind` / `shell`
- cwd/env identity; `env=None` vs explicit env
- accept `.cmd`/`.bat`; reject other suffixes
- reject dangerous chars, non-str, joined-string args, empty script, trailing `\`
- argv contains fixed switches `/d` `/v:off` `/s` `/c`; no `CALL`; no temp-file side effects
- `prepare_cli(..., cmd_wrapper=real)` succeeds; without inject still `PlatformWrapperRequired`
- `cmd_exe=` injection (or equivalent) so Linux never needs a real System32 `cmd.exe`

Windows-only (`skipUnless` win32; real `subprocess.run(..., shell=False)`, no launch mock):
- fake `.cmd`/`.bat` forwarding to a Python argv recorder: ordinary/empty/Chinese/spaced
  script path and cwd roundtrip; returncode / stdout / stderr / explicit env
- rejected inputs must not execute the script or create marker files

Do not assert a single unique `/c` string spelling; assert structure, safety, and semantics.

## Delivery order (Hermes a26)
1. Linux pushes this red SHA (contract + claim).
2. Hermes implements `windows_cli.py` against it in isolation, real cmd.exe tests, delivers patch + baseline SHA + file hash + acceptance log.
3. Linux applies patch, pushes final SHA; dual re-verify; then PR/merge.

## Not in this slice
- Auto-wiring product callers to `get_cmd_wrapper()`.
- Expanding the rejected-character set or adding PowerShell/CALL.
- Knife 7 (`test/deepseek-default-path-xplat-07`).

## Status (Hermes a27)
- `windows_cli.py` applied verbatim from Hermes patch (baseline `aa8af01`, patch sha256
  `cb43dc93…656e`, source sha256 `ac4ee995…eb49`).
- Windows roundtrip cwd assertion now normcases both sides (Windows preserves directory
  case; path semantics equal). No product cwd change.
