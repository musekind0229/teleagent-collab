# Surgical fix: install_roots lead gating for system_install (P2 rework)

Repo tip on main. No Cloud Agent. No Hermes. No deploy. No glue rewrite of main loop. Do NOT touch TeleAgent/SAC. Coordinator will re-run live job after this lands.

## Bug
Live run `p2-st-reinstall-20260913T055711Z`: Grok lead returned `demand_safe_path` for charter-required `rm -rf /home/box/SillyTavern` under `install_roots`, mapped to reject; pending stuck; wall timeout. Charter now declares install_execute under install_roots must be allow/once.

## Fix (minimal)
1. In `src/glue.py` (and scheduler twin if same allow_hint exists): when charter `task_kind=system_install` and `install_roots` non-empty, extend `allow_hint` / lead permission prompt so that:
   - paths/commands under declared `install_roots` for install_execute (stop listen, rm/delete root, git clone into root, npm install, start server) → prefer **`once`** (or allow);
   - **do not** prefer `demand_safe_path` for those in-charter install actions;
   - still reject/demand_safe_path for secret-adjacent, paths outside install_roots+workspace, sudo/firewall/etc.
2. Optionally include `install_roots` list and a short must-line excerpt in the lead request authorized_scope.
3. Tests: unit test that format/prompt or glue helper for system_install include install_roots once-guidance and do not instruct demand_safe_path for in-root rm. Keep existing secret-adjacent tests green.

## Deliverables
- Commit: `fix(glue): system_install install_roots lead once not demand_safe_path`
- Push: `GIT_SSH_COMMAND='ssh -i /home/box/.ssh/id_ed25519 -o IdentitiesOnly=yes' git push origin main`
- Print short+full SHA

## Verify
```
PYTHONPATH=src python3 -m unittest src.test_hard_rules src.test_p2_auth_recovery -q
# plus any new test you add
```
