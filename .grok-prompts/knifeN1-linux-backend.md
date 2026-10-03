# 刀N1: Linux sync — teleagent-linux backend, Linux --ready/doctor, deps, systemd unit

Develop/verify on this Debian box only. Never connect to any remote server. Never print, log, or commit secrets
(TeleAgent local API username/password/session key, COLLAB_API_TOKEN). Do not touch
`src/execution_backend/antigravity_cli_v1.py::build_agy_prompt`, `src/charter.py` contract rendering, or
`validate_plan`/done_when handling (another fix is pending there).

## 1. `teleagent-linux` worker backend for `bin/collab-service.py`
Today collab-service only has `--backend teleagent-windows` (`src/execution_backend/windows_supervised_v1.py`,
which drives the `win_collab` Engine/Store: external_inputs isolation, contamination gate, permission scope summary,
review decisions, desktop lock). Linux TeleAgent (GUI 2.5.x, opencode-style local API, same local-v1 HMAC and the
same three env keys `win_collab.client.KEYS`) listens on loopback :4399 once the GUI is logged in.

- Reuse the engine; do not fork it. Preferred: a thin subclass/sibling `LinuxSupervisedExecutionBackend` with
  `backend_id = "teleagent.linux.supervised_v1"` in `src/execution_backend/linux_supervised_v1.py` (or generalize
  the Windows class with a platform/client-factory parameter and keep `WindowsSupervisedExecutionBackend` behavior
  and backend_id unchanged). No stdin_wrap on Linux.
- Linux discovery `discover_linux()` (put it in `win_collab/client.py` next to `discover()`, or a new
  `win_collab/linux_discovery.py`):
  - base URL: `TELEAGENT_URL` if set, else `http://127.0.0.1:4399`. Must stay loopback (Client already enforces
    127.0.0.1).
  - creds: if all three KEYS are set in this process env, use them. Else scan `/proc/[0-9]*/environ` like
    `src/teleagent_adapter/linux_local_v1.py::default_find_creds`, but ONLY for processes whose `/proc/<pid>/exe`
    resolves to a TeleAgent image: default candidates `/opt/TeleAgent/teleagent`, and anything under
    `~/.local/share/TeleAgent/runtimes/` (super-agent-code / node) for the TeleAgent user; allow override via
    `TELEAGENT_LINUX_IMAGES` (os.pathsep-separated). Collect all matches, require them to agree
    (`pick_unique_creds`), otherwise raise a clear RuntimeError. Count verified / permission-denied / keys-missing
    like `discovery_failure_message` so the error says WHY (e.g. "TeleAgent processes found but environ not readable:
    run collab-service as the TeleAgent user or root"; "no TeleAgent process"; "keys not in environ (GUI not logged
    in?)"). Never include values.
  - `Client(base, creds)` from `win_collab.client` stays the HTTP layer (it imports ctypes at module top; make sure
    importing it on Linux is fine; do not call Windows-only functions).
- Desktop lock (`win_collab/desktop_lock.py`) already falls back to `~/.local/share/teleagent-collab/desktop-locks`;
  verify it works on Linux (file lock via platform_services posix).
- Make sure Engine path code (`external_directory_scope`, `_pattern_directory`, `contained`, `hard_reject`,
  workspace paths) handles POSIX patterns like `/home/u/.../external-inputs/<job>/0/*`; add tests with POSIX paths.
- `bin/collab-service.py --backend teleagent-linux` (alias `teleagent_linux`), state dir `persist/linux-controller`.
  Error at startup only when dispatching (lazy client) is fine, but the service must start without TeleAgent so
  `/health`-style endpoints and `--ready` can explain the problem.

## 2. Linux `--ready` / doctor
`--ready` (and `--check-gui`) today call `assess_win_gui_readiness` (`src/framework/app_service.py`), Windows only.
Add `assess_linux_gui_readiness(...)` with injectable probes (for tests) returning the same top-level shape
(`ready`, `dispatch_allowed`, `occupancy`, `session_count`, `tip_ok`, `checks[]`, `hints[]`), with checks:
python version; `jsonschema` importable (report `missing_dependency` with "pip install -r requirements.txt", does
not by itself block dispatch unless code needs it — decide from actual imports; it is used by tests); TeleAgent
process present (by image); X display hint (Xvfb/DISPLAY presence only, informational); port 4399 (or
TELEAGENT_URL) listening on loopback; creds discoverable (presence only, never values; distinguish
"not readable as this user" vs "not logged in"); `/session/status` occupancy via the client when creds are
available; running-tip check same as Windows. Exit codes identical to the Windows `--ready`. `--ready` picks the
Linux assessor when `sys.platform` is linux (or `--backend teleagent-linux` is given), Windows otherwise. Each
failing check carries a concrete next step, e.g. "TeleAgent GUI not logged in: open the GUI (VNC to the Xvfb
display) and log in; :4399 starts after login".

## 3. Dependencies
Add `requirements.txt` (runtime: stdlib only unless something imports a third-party lib — check with rg; tests:
`jsonschema>=4`) — or `requirements.txt` + `requirements-dev.txt`; keep it simple and document in README.
Doctor/ready reports a missing jsonschema clearly (see above).

## 4. systemd unit (in repo only, never install)
`deploy/systemd/collab-service.service` (+ optional `collab-service.env.example` with placeholder values only, no
real secrets) and `deploy/systemd/README.md` (Chinese ok): run as the TeleAgent user (so /proc environ is readable)
or root; `WorkingDirectory=/opt/collab/teleagent-collab` example; `ExecStart=/usr/bin/python3 bin/collab-service.py
--persist /var/lib/teleagent-collab --port 8765 --backend teleagent-linux --planner deterministic`;
`EnvironmentFile=-/etc/teleagent-collab/collab-service.env` (COLLAB_API_TOKEN etc. live there, mode 600);
`Restart=on-failure`; hardening that does not break /proc reads (NoNewPrivileges=yes, PrivateTmp=yes,
ProtectSystem=full; do NOT set ProtectProc=invisible). Install steps (copy, daemon-reload, enable --now, `--ready`
check, journalctl), uninstall steps, and prerequisites (Xvfb + TeleAgent GUI logged in, port 4399).

## 5. Tests
New `src/test_linux_supervised_backend.py` (simulated: fake /proc tree via injectable root path, fake Client /
fake HTTP): discovery explicit env; /proc scan with matching image; non-TeleAgent process ignored; conflicting creds
→ error; permission denied counted; no values in error text; backend start_run/observe/collect/list_pending_actions
round-trip with a fake client (reuse patterns from existing windows supervised tests, `rg -n
WindowsSupervisedExecutionBackend src tests`); POSIX scope summary; collab-service `_backend("teleagent-linux")`;
`assess_linux_gui_readiness` cases: no process, process but port closed, creds unreadable, ready+idle, busy, missing
jsonschema. Run `python3 -X utf8 -m unittest discover -s src -p 'test_*.py'` and
`PYTHONPATH=src:. python3 -X utf8 -m unittest discover -s tests -t .` — baseline: src only 1 known Windows-only error
(`test_prepare_injects_configured_proxy_and_userprofile`), tests/ all OK. Keep that. Do not commit.
Update docs: a short `docs/LINUX.zh-CN.md` (what works on Linux, backend choice, ready, login requirements).
