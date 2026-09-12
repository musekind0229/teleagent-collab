# Knife 3 — hard-rules via public→native; PendingItem public ids

## Scope
- Scheduler/glue hard-rule inputs go through `prepare_permission` / `to_native_for_rules`.
- Kernel/scheduler must not parse TeleAgent field names for rule decisions; native shape is adapter-local.
- `PendingItem` persists `request_id` + `session_id` (public). Recovery: no re-dispatch, no re-send of decided replies.
- Old entry points + regressions stay green. No full glue rewrite. No second backend / Hermes. No deploy.

## Touch
- `src/teleagent_adapter/permission_view.py` — `prepare_permission`
- `src/glue.py` — `_handle_permission_for_session` + `session_id_of_permission`
- `src/scheduler.py` — `handle_one_permission` PendingItem writes `request_id`
- `src/state_store.py` — `PendingItem.request_id` + compat `from_dict`
