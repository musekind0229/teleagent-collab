# 刀P1: GitHub issue #14 — AGY result has no workspace, dependency handoff refused

Repo: this directory (main, clean). Python stdlib. Never print tokens. Do not commit.

Issue #14 (real run, --planner grok + --backend antigravity): lead plan A→B (B depends_on A). A succeeded on
antigravity.cli_v1, artifacts exist (absolute artifact paths only in the result). B failed before dispatch:
`dependency handoff failed: dependency artifact workspace is missing`. src/framework/artifact_handoff.py
(collect_direct_dep_artifacts ~L186) needs result.workspace or task.workspace; `_workspace_root` raises. Neither the
AGY task nor the AGY result carries workspace. AppCoordinator knows workspaces_root/goal/task at dispatch time (it
computes `root` for start_run) but never binds/persists it.

Requirements (from the issue):
- FIRST write a failing regression (RED) through the real AppCoordinator / CollabApplication with a planner that
  returns A→B (recording/fake planner like existing tests) and the production AGY result shape: use the real
  AntigravityCliExecutionBackend with a fake agy binary (see src/test_contract_render.py `_write_fake_agy`; extend
  the fake so it writes the requested artifact content given via env/args, and for B it must read the handed-off
  a.txt and write b.txt accordingly) — confirm it fails with that exact error before fixing. Keep a short note of
  the RED output in your final message.
- Fix: bind and persist the trusted run workspace from the scheduler side: when AppCoordinator dispatches a task it
  records `workspace` = the directory it created/passed to start_run (derived from workspaces_root + goal + task,
  not from worker output) on the durable task record (durable_api bind_task_run or a new field), and handoff uses
  that trusted value. Never infer the trusted root from LLM artifact paths or unverified worker self-report; if a
  backend result reports a workspace that differs from the trusted one, ignore it and use the trusted one (or fail
  with a clear error) — decide and test.
- Do NOT remove/weaken `_workspace_root` or containment checks: keep rejection of path traversal, symlink/junction,
  credential-like files, out-of-workspace files, name collisions, and unaccepted artifacts (existing tests must
  stay green; add a test that an AGY artifact path pointing outside the trusted workspace is still refused).
- Legacy/recovered task records without workspace: recompute from verified scheduler metadata (same deterministic
  function used at dispatch) only if that directory exists and matches; otherwise fail with an explicit
  `dependency workspace unrecoverable: ...` reason. No silent bypass, no auto re-dispatch.
- Also check the windows/linux supervised backends' results get the same trusted workspace binding (shared code path).
- GREEN: A succeeds, B is dispatched, gets a.txt handed off, b.txt is written with the expected content and verified
  in the test.
Tests in a new src/test_handoff_workspace.py. Run `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` (only
allowed error: Windows-only `test_prepare_injects_configured_proxy_and_userprofile`) and `PYTHONPATH=src:. python3 -X
utf8 -m unittest discover -s tests -t .` (all OK). Tests must be Windows-safe too (fake agy on Windows is a .cmd shim
that truncates multi-line argv — avoid depending on the full prompt there, or skip that assertion on nt).
