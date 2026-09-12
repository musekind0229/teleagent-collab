# Knife 5 — public Run observation (TA status/message/finish)

## Scope
- `teleagent_adapter.run_observe`: `build_run_observation` / `fetch_run_observation`
- Adapter `observe_run` on Linux local-v1 / Windows blocked
- Scheduler live completion + abort-confirm consume public obs (`activity` / `finish_successful` / …)
- Glue main + redo loops: permission GET stays; status/message finish via `fetch_run_observation`
- Missing/unusable fields → unknown / not successful (缺字段不补授权)
- No full glue rewrite. No second backend / Hermes. No deploy.

## Next (suggestion only)
Second backend adapter behind the same public observe + permission surfaces — pick target when ordered (do not start here).
