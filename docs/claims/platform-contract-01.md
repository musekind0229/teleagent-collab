# Claim: platform-contract-01

- Base: origin/main @ 2be5ae1867398d0792d5b847343a9e729967f3d7
- Branch: feat/platform-contract-01 (main line: pm/simple)
- Agreed with Windows-side agent Hermes.

## Files (this slice writes only these)
- docs/claims/platform-contract-01.md
- src/platform_services/__init__.py (add `get_cmd_wrapper`, `capability_report` only)
- src/platform_services/cli.py (new)
- src/platform_services/paths.py (new)
- src/test_platform_contract.py (new)

## Ownership
- Windows (Hermes) owns ONLY: src/platform_services/windows.py, win_job.py,
  windows_cli.py, windows_paths.py.
- Frozen for both sides: src/framework/app_service.py, src/lead_adapter/cancel.py,
  contracts/*.schema.json, src/execution_backend/agy_account_pool.py.
- process.py and file_lock.py: signatures unchanged.

## Interfaces
```python
# platform_services/cli.py
@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]; cwd: str | None; env: Mapping[str, str] | None
    wrapper_kind: str          # "direct" | "interpreter" | "windows_cmd"
    shell: bool = False        # always False
class CliLaunchError(ValueError)
class PlatformWrapperRequired(CliLaunchError, NotImplementedError)
class CmdWrapper(Protocol):    # implemented by windows_cli.CmdWrapper (Windows side)
    def wrap(self, script: str, args: tuple[str, ...], cwd, env) -> LaunchSpec  # wrapper_kind="windows_cmd"
def prepare_cli(binary, args, cwd=None, env=None, *, interpreter=None,
                platform=None, cmd_wrapper=None) -> LaunchSpec
```
Rules: argv list only, never shell=True or a joined string. POSIX: exec bit +
shebang/ELF/Mach-O header, else explicit `interpreter`. Windows: `.exe` direct;
`.cmd/.bat` require `cmd_wrapper` else `PlatformWrapperRequired`; other files
need `interpreter`.

```python
# platform_services/paths.py
@dataclass(frozen=True)
class PathInspection: root; normalized; link_chain: tuple[str, ...]; verdict; intent; reason
def inspect_path(root: str, relative: str, intent: str) -> PathInspection
    # intent in {"read","write","delete","exec"}; verdict in {"inside","escape","unknown"}
def require_safe(insp) -> str   # raises PathSafetyError on escape, or unknown+protected intent
```
POSIX: component walk with lstat/readlink (40-hop cap → unknown). Windows
non-symlink reparse points (junctions) → unknown until windows_paths.py refines.
Not an OS sandbox; TOCTOU races remain.

```python
# platform_services/__init__.py
def get_cmd_wrapper(*, platform=None) -> CmdWrapper | None  # None on POSIX; raises PlatformWrapperRequired if windows_cli missing
def capability_report(*, platform=None) -> dict[str, str]   # unknown host raises FileLockUnsupported
```

## Tests (src/test_platform_contract.py)
- CliContractTests: test_interpreter_argv_roundtrip_no_shell, test_string_args_rejected,
  test_missing_binary_rejected, test_posix_direct_requires_exec_bit_and_shebang (skip on Windows),
  test_windows_cmd_needs_wrapper, test_windows_cmd_hook_contract, test_windows_non_exe_needs_interpreter
- PathContractTests: test_inside_plain_and_nonexistent, test_dotdot_and_absolute_escape,
  test_bad_intent, test_unknown_rejected_for_protected_only, test_symlink_escape_absolute,
  test_symlink_escape_relative, test_symlink_inside_chain, test_symlink_loop_unknown
  (symlink tests skip with reason if symlink creation not permitted)
- CapabilityReportTests: test_current_host_report, test_unknown_platform_raises_no_fallback,
  test_posix_has_no_cmd_wrapper
