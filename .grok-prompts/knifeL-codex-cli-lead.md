# 刀L: Codex CLI as an optional lead adapter (`codex_cli`)

Repo: this directory. Replace the stub `src/lead_adapter/codex_cli.py` with a real implementation modeled on
`src/lead_adapter/grok_cli.py` and `src/lead_adapter/deepseek_harness.py`. Do NOT run the real codex binary, do NOT
log in, do NOT read any codex credentials (~/.codex/auth.json etc.). Tests use fake binaries / mocks only.

## Verified CLI facts (codex-cli 0.155.0, from `codex exec --help`)
`codex exec [OPTIONS] [PROMPT]`; PROMPT `-` (or omitted) => read instructions from stdin.
Relevant options: `-s/--sandbox read-only|workspace-write|danger-full-access`, `-C/--cd <DIR>`,
`--skip-git-repo-check`, `--ephemeral` (no session files), `--color never`, `--output-schema <FILE>` (JSON Schema
file for the final response), `-o/--output-last-message <FILE>`, `-c key=value` (TOML value), `--json` (JSONL events,
we do NOT use it). Dangerous flags that must NEVER appear: `--dangerously-bypass-approvals-and-sandbox`,
`--full-auto`, `--approve-for-me`, `--dangerously-bypass-hook-trust`, `--add-dir`, `--worktree`,
`-s workspace-write`, `-s danger-full-access`. No `--ask-for-approval` flag on exec; use `-c approval_policy="never"`.

## Final command (exactly this shape, prompt on stdin)
```
<codex> exec --sandbox read-only -c approval_policy="never" --ephemeral --skip-git-repo-check --color never
        -C <cwd> --output-schema <tmp>/schema.json -o <tmp>/last.json -
```
- `<tmp>` = fresh `tempfile.mkdtemp(prefix="collab-codex-lead-")`, always removed in `finally` (shutil.rmtree, ignore errors).
- `<cwd>` = the `cwd` passed to `decide` if it is an existing directory, else `<tmp>`. (Read-only sandbox, so codex
  cannot modify it.)
- Prompt (stdin, UTF-8): same as grok_cli: `format_lead_request_prompt(request, allow_hint=...)`, legacy_prompt
  prefix handling identical, plus a final line telling the model: reply with ONLY one JSON object matching the
  schema, no prose, no code fences, do not run commands or edit files.
- Expose `build_command(...)` (or similar pure helper) so tests can assert the argv, and a module constant tuple
  `FORBIDDEN_FLAGS`; add an assertion/guard in the adapter that no forbidden flag is in argv (if one is, return
  call_failed without spawning).
- Never retry. No second attempt with any loosened setting (no sandbox change, no dropping schema, nothing).

## Output schema file (Codex/OpenAI strict structured output compatible)
Codex `--output-schema` uses strict structured output: every property must be in `required`,
`additionalProperties: false`, and prefer `enum` over `const`. Write a helper `codex_output_schema(pinned)` that takes
the result of `pin_lead_response_schema(schema, request)` and returns a deep copy where: each `const: X` becomes
`enum: [X]` (keep type), `required` = all property keys, `additionalProperties` = False. Optional fields like
`safe_path_hint` become required strings (model may send ""). After parsing, drop `safe_path_hint` if it is "".

## Output parsing (strict)
- Primary source: `<tmp>/last.json` (`-o`). If missing/empty, fall back to stdout (stripped). stderr is progress
  logs: never parse decisions from stderr.
- Strict: `json.loads(text.strip())` must yield a dict. No regex extraction, no code-fence stripping, no
  "first {...}" search. Anything else => illegal output.
- Return value contract (`decide` returns `(raw, parsed)`):
  - success: `(text[:3000], obj)` — upstream `validate_lead_decision` does the application_id/context_summary binding
    check; ALSO pre-check in the adapter: if `obj.get("application_id") != request["application_id"]` (string
    compare, request id non-empty) return `("ILLEGAL_OUTPUT", {"_lead_status": "error", "error": "codex_cli
    application_id mismatch: expected ... got ...", "lead_error_code": "application_id_mismatch"})`.
  - illegal output: `("ILLEGAL_OUTPUT", {"_lead_status": "error", "error": "codex_cli illegal output: <short
    reason>", "lead_error_code": "illegal_output"})`. Raw text must NOT be returned as `raw` in this case (otherwise
    `validate_lead_decision` might regex-extract a JSON from prose). Do not echo more than ~300 chars of model text
    in the error.
  - timeout: `safe_failure("timeout", "codex_cli timeout after Ns")`.
  - non-zero exit: `("CALL_FAILED", {"_lead_status": "call_failed", "error": <stderr tail <=1500 chars or
    exit=N>, "returncode": N})` — even if stdout/last.json contains a JSON decision (non-zero exit is never trusted).
  - spawn OSError / binary not found: `safe_failure("call_failed", ...)` with a clear message.
- The adapter NEVER synthesizes `decision`/`verdict`. All failure envelopes contain neither key.
- Sanitize error text: run it through a small redactor that masks things looking like tokens
  (`sk-...`, `Bearer ...`, long hex/base64 runs >= 32 chars) before putting it in the envelope. Keep simple.

## Binary resolution `resolve_codex_bin(env=, which=, platform=, is_file=)`
Order: `COLLAB_CODEX_LEAD_BIN` (codex-specific override, so a grok `COLLAB_LEAD_BIN` does not collide) →
`COLLAB_LEAD_BIN` → PATH `codex` (then on Windows `codex.cmd`, `codex.exe`). Each explicit value only if it is a file.
Raise `CodexBinNotFound(FileNotFoundError)` listing what was tried. Resolution is LAZY: `CodexCliLeadAdapter()` and
`get_lead_adapter("codex_cli")` must not raise when no binary exists; `decide` then returns call_failed with the
CodexBinNotFound message. Constructor accepts `bin_path=None` override. Document the DESKTOP-TBB531F path
`C:\Users\Admin\.local\share\TeleAgent\runtimes\node\codex.cmd` (0.155.0) as an example for `COLLAB_CODEX_LEAD_BIN`.

## Windows `.cmd` / `.bat` invocation
Python must not rely on CreateProcess implicitly running batch files (BatBadBut-style escaping issues). When the
resolved binary ends with `.cmd`/`.bat` (case-insensitive) AND we are on Windows (inject `platform` for tests):
- Validate every argv element: reject (call_failed, no spawn) if any contains one of `" % ! ^ & | < > \r \n`.
  Our argv is fixed flags + temp/cwd paths, so this only trips on weird paths; message must say which arg was refused.
- Spawn `[comspec, "/d", "/s", "/c", "\"" + subprocess.list2cmdline(argv) + "\""]` where comspec =
  `os.environ.get("COMSPEC") or "cmd.exe"`; pass it as a single command-line string so cmd's /s quote rule applies
  (i.e. `subprocess.Popen(cmdline_string, ...)` on Windows). Prompt still goes via stdin.
- Non-batch binaries: spawn the argv list directly.
Put this in a pure helper `spawn_spec(argv, platform=...) -> (args, use_string)` so it is unit-testable on Linux.

## Timeout / process tree
Use `subprocess.Popen` + `communicate(input=prompt, timeout=...)` with `stdin/stdout/stderr=PIPE`,
`encoding="utf-8", errors="replace"`. Windows: `creationflags=CREATE_NEW_PROCESS_GROUP`; on timeout run
`taskkill /T /F /PID <pid>` (capture output, ignore errors) because cmd /c leaves node children holding pipes; posix:
`start_new_session=True` and `os.killpg(pid, SIGKILL)` on timeout. Then `communicate(timeout=5)` best-effort, then
return the timeout envelope. Never hang forever.

## Alias `codex`
`get_lead_adapter("codex")` (and env `COLLAB_LEAD_ADAPTER=codex`) must STILL return InProcessLeadAdapter (do not
change behavior), but log a warning via `logging.getLogger("lead_adapter")`:
`lead adapter alias "codex" means inprocess (dialogue-as-lead), not the Codex CLI; 要用 Codex CLI 请设 codex_cli`.
Factory: `codex_cli`/`codex-cli` → `CodexCliLeadAdapter(**{bin_path})` filtered like grok. Update factory docstring.

## doctor_hint
`{"status": "wired_unverified", "name": "codex_cli", "fake_pass": False, "bin_path": <resolved or None>,
"bin_error": <msg or None>, "sandbox": "read-only", "approval_policy": "never", "live_verified": False}`.
Must not raise when the binary is missing.

## Tests: new `src/test_codex_cli_lead.py` (unittest, simulated, no real codex)
Use fake binaries: a small Python script written to a temp dir and a posix shell shim (`#!/bin/sh` exec python
script) or mock `subprocess.Popen`. The fake reads argv to find `-o` path and writes a configured reply. Cover:
1. legal permission JSON (`once`) and review JSON (`pass`) → `validate_lead_decision` accepts; argv contains
   `exec --sandbox read-only -c approval_policy="never" ... -` and NONE of FORBIDDEN_FLAGS; prompt arrived on stdin
   (fake records stdin to a file) and contains application_id; schema file had enum-pinned application_id and all
   keys required.
2. application_id mismatch → adapter error envelope + validate raises LeadDecisionError (code call_failed).
3. illegal output: prose; JSON inside prose; code-fenced JSON; JSON array; empty → error envelope, no
   decision/verdict, validate raises.
4. timeout (fake sleeps; small timeout_sec) → timeout envelope, validate raises code timeout; returns promptly.
5. non-zero exit even with valid JSON in last.json → call_failed.
6. binary not found (lazy) → factory ok, decide call_failed, doctor_hint ok.
7. resolve order: COLLAB_CODEX_LEAD_BIN > COLLAB_LEAD_BIN > PATH codex / codex.cmd (injected which/is_file/platform).
8. `.cmd` path: `spawn_spec([...codex.cmd...], platform="win32")` → string starting with comspec `/d /s /c "`;
   argv with `&` or `%` refused before spawn (Popen not called). Non-batch → list. Add a Windows-only test
   (`skipUnless(sys.platform=="win32")`) that writes a fake `codex.cmd` calling `python fake.py %*` and runs a full
   decide through it (legal JSON path + a path with a space in the temp dir if feasible).
9. alias warning: `assertLogs("lead_adapter", "WARNING")` for `get_lead_adapter("codex")`, message contains
   `codex_cli`; result is still inprocess. No warning for `inprocess`.
10. no auto pass/once: for every failure mode above, assert parsed has no decision/verdict and validate raises; no
    retry (Popen/fake called exactly once per decide).
11. temp dir cleaned after success and failure.
Update `src/test_p3_lead_question_install.py::test_codex_cli_stub_no_fake_pass` to the new semantics (no binary →
call_failed, no verdict) by forcing a missing binary via env/bin_path, keep `test_codex_alias_still_inprocess`.

## Docs
Update `docs/lead-adapter.md`: table row for codex_cli (real now, live unverified — boss not logged in yet), a section
"Codex CLI（codex_cli）" with the final command line, permissions (read-only sandbox, approval never, ephemeral, no
bypass/full-auto, no retry), binary resolution order, Windows .cmd handling, output parsing + failure semantics table,
the `codex` alias note (still inprocess + warning), and that live regression is pending login. Update the
"Claude / Codex 待办" section accordingly (Claude still stub). Update `src/lead_adapter/__init__.py` docstrings.

## Constraints
Minimal changes elsewhere. Do not touch skills/, .env, credentials, scheduler/glue semantics. Run
`python3 -X utf8 -m unittest src.test_codex_cli_lead` style runs from `src` (`cd src && python3 -m unittest
test_codex_cli_lead test_p3_lead_question_install test_grok_cli_lead_bin test_deepseek_harness_lead test_p1_completion_lead`)
and make them pass. Do not commit.
