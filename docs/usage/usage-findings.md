# TeleAgent / Grok usage metering investigation

Date of investigation: 2026-09-06  
Investigator: Grok Bot executor (usage-probe)  
Workspace: ``probe-sandbox/…``

## Executive answer

**Which product's tokens?** Collab smoke tests burned tokens in **two separate products**:

1. **TeleAgent worker (NewApi / 预置模型)** — HTTP `127.0.0.1:4399` sessions using `providerID=NewApi`, `modelID=chat-lite|chat-pro|chat-flash`. These call TeleAI cloud `https://agent.teleai.com.cn/superCowork/sapi/api/v1` (`X-Route-Target: ops-gateway`, `useSuperAgentAuth: true`). Account = TeleAgent / TeleAI user `v1_public_<teleagent-userId>`.
2. **Grok Build lead** — via `/workspace/run-grok.sh` → `~/.grok` / xAI auth (`~/.grok/auth.json`). Response JSON already includes `usage` / `total_cost_usd` / `modelUsage.grok-4.6-build`. Account = **xAI/Grok**, **not** TeleAgent.

If the user is looking at the **TeleAgent GUI usage/credits dashboard**, they are looking at product (1)'s portal metering — **not** Grok Build usage.

**Does HTTP bypass skip TeleAgent GUI metering?** **Yes (for the GUI/portal credit dashboard), with strong before/after + historical evidence.**

- Local SAC/DB **does** record tokens for HTTP sessions.
- TeleAI **portal** usage overview/records/user-snapshot stayed at **0** even after large Sep 5 HTTP collab usage and after today's identifiable HTTP probe.
- GUI metering is implemented as **renderer-side** `tokenUsage` → `POST .../api/v1/usage/sync` from Electron localStorage pending records. HTTP-only sessions never entered that pending store.

Server-side ops-gateway may still see the LLM traffic (same sapi baseURL); that is separate from the GUI "积分用量" surfaces we queried. Local `cost` fields are always `$0` / `0` because preset model cost tables are all zeros.

---

## How sessions are created (from discovery + glue)

| Item | Value |
| --- | --- |
| Endpoint | `http://127.0.0.1:4399` |
| Auth | HTTP Basic (`super-agent` + local password) + `X-SA-*` local-v1 HMAC |
| Create | `POST /session` + `x-opencode-directory` |
| Prompt | `POST /session/:id/prompt_async` with `parts` + `model: {providerID:NewApi, modelID:chat-lite}` |
| Collab glue | ``probe-sandbox/…`` (and cut2/cut3) |
| Docs | `teleagent-discovery.md`, `teleagent-worker-contract.md`, `teleagent-min-wrapper.md` |

Grok lead responses in `run-report*.md` include usage/cost — those are **Grok Build** fields, unrelated to TeleAgent portal credits.

---

## Surfaces checked

### Local TeleAgent (SAC)

| Path / API | Role | Finding |
| --- | --- | --- |
| `POST /usage/statistics` | Local helper: "Aggregate external usage records with local OpenCode session and message data" | Empty body → `tokenSum=0`. With a probe-shaped record → `tasks=1 conversations=1` but still `tokenSum=0` (field mapping). **Not** the cloud dashboard. |
| `teleagent.db` `message.tokens` | Per-message token store | Nonzero for HTTP NewApi sessions (collab + probe). |
| `log/super-agent-server-*.log` `[cost]` | Per-request token log | Records NewApi in/out; `cost=$0.000000`. |
| Electron LevelDB localStorage | `usage_upload_policy_*` mode=`metering`; pending usage keys | **Probe session/message IDs absent** after HTTP job. |
| GUI bundle `index-*.js` | Cloud constants | `USAGE_SYNC`, `USAGE_STATISTICS` (`/api/v1/usage/token-records`), `USAGE_QUOTA_OVERVIEW` (`/user/portal/usage/stats/overview`), `USAGE_QUOTA_RECORDS`, `USER_SNAPSHOT`. Metering via `x9t` → `as(USAGE_SYNC)`. |

### TeleAI cloud (GUI-facing, via HTTP + existing `X-Token`, secrets redacted)

Base: `https://agent.teleai.com.cn/superCowork/sapi`

| API | After probe / after Sep 5 collab |
| --- | --- |
| `GET /user/portal/usage/stats/overview` | `{taskCount:0, callCount:0, pointsUsed:0}` |
| `GET /user/portal/usage/stats/record?page=1&pageSize=5` | `{total:0, list:null}` |
| `GET /api/v1/user-snapshot?...&date=2026-09-05` | all token totals **0** (collab day) |
| `GET /api/v1/user-snapshot?...&date=2026-09-06` | earlier call returned all **0**; a later call got anti-abuse 40105 |

UI copy (bundle): usage quota is "Credit usage for preset models and tools in the desktop… Custom models and tools do not consume credits." NewApi is named **预置模型** (preset).

### Grok Build (separate)

| Path | Role |
| --- | --- |
| `/workspace/run-grok.sh` | Wrapper to `~/.grok/downloads/grok-linux-x86_64` |
| `~/.grok/auth.json` | xAI / IdP credentials (not read into report) |
| `~/.grok/sessions/` | Local Grok session artifacts for collab dirs |
| Lead JSON in collab reports | `usage`, `total_cost_usd`, `modelUsage["grok-4.6-build"]` |

Grok usage is counted on the **Grok/xAI account**, visible in CLI response metadata — **not** TeleAgent portal.

---

## Verification probe (HTTP)

| Field | Value |
| --- | --- |
| Session id | `ses_f8b291252ffegcfDaiQZAW55vq` |
| User message | `msg_074d6fbc10014TgcNYx5x2h9xf` |
| Assistant message | `msg_074d6fbe2001r5XQFj02TlRLsr` |
| Prompt HTTP request id | `01a074d6-fbbd-777b-b238-8e6e02146e44` |
| Model | NewApi / chat-lite |
| Prompt text marker | `USAGE-PROBE-MARKER-20260906` … reply `PONG` |
| Workspace | ``probe-sandbox/…`` |
| before_prompt | **2026-09-06 03:50:41 UTC** / **11:50:41 Asia/Singapore** |
| after_job | ~**2026-09-06 03:50:51 UTC** / **11:50:51 Asia/Singapore** (from poll finish) |
| Tokens burned | input **24949**, output **49**, total **24998** |
| Log line | `[cost] provider=NewApi tokens(in=24949 out=49 cacheRead=0) cost=$0.000000` |

### DB token sum before → after

Before (`before-db-token-sum.json`):

{
  "utc": "2026-09-06 03:50:04 UTC",
  "sgt": "2026-09-06 11:50:04 Asia/Singapore",
  "sum_input": 553253,
  "sum_output": 57978,
  "sum_total": 4686463,
  "sessions_with_tokens": 16,
  "message_count": 189
}

After (`after-db-token-sum.json`):

{
  "utc": "2026-09-06 03:50:57 UTC",
  "sgt": "2026-09-06 11:50:57 Asia/Singapore",
  "sum_input": 578202,
  "sum_output": 58027,
  "sum_total": 4711461
}

Delta: **+24949 input, +49 output, +24998 total** — matches the probe assistant message exactly.

### Same surfaces after probe

| Surface | Result |
| --- | --- |
| `POST /usage/statistics` empty | still `tokenSum=0` |
| LevelDB pending | **no** probe `msg_` / `ses_` ids |
| Portal overview | still `pointsUsed=0` / `taskCount=0` / `callCount=0` |
| Portal records | still `total=0` |

Timestamps file:

```
before_prompt_utc=2026-09-06 03:50:41 UTC
before_prompt_sgt=2026-09-06 11:50:41 Asia/Singapore
after_job_utc=2026-09-06 03:50:51 UTC
after_job_sgt=2026-09-06 11:50:51 Asia/Singapore

```

---

## Interpretation

1. **HTTP does not skip LLM execution** — NewApi calls happen; tokens land in `teleagent.db` and `[cost]` logs.
2. **HTTP does skip (or never enters) GUI credit metering** — portal overview/records and user-snapshot stay at 0; renderer localStorage never sees HTTP message IDs; sync path is GUI `USAGE_SYNC`.
3. Therefore the user's suspicion ("GUI usage dashboard didn't count HTTP-driven sessions") is **supported**.
4. Do **not** mix this with Grok Build costs in collab reports — those are xAI/`~/.grok`.

---

## Recommended next step (if still inconclusive for server-side billing)

1. **Same-account A/B:** one short **GUI** chat with NewApi/chat-lite vs one **HTTP** chat (this probe pattern). Re-check portal overview/records within a few minutes.
2. If GUI chat increments `pointsUsed`/`callCount` and HTTP does not → confirms GUI-sync-only metering for the dashboard.
3. If neither increments → check whether this account's `metering_billing_enabled` / plan treats ops-gateway calls as free, or whether portal stats lag.
4. Optional computerUse: open TeleAgent usage/quota page (already logged-in session; do not ask for passwords). Compare on-screen numbers to portal JSON above.
5. For **Grok** spend: use Grok/xAI account usage surfaces / response `total_cost_usd` — never TeleAgent GUI.

---

## Artifacts (this probe)

- `probe_usage.py` — signed local API helper  
- `create-session.out`, `prompt.out`, `poll-messages.out`  
- `before-db-token-sum.json`, `after-db-token-sum.json`  
- `before-usage-statistics.json`, `after-usage-statistics.json`  
- `cost-log-hits.txt`, `cloud-xtoken-result.json`, `cloud-overview-variants.json`  
- `newapi-provider-redacted.json`, `leveldb-after.txt`  
- Secrets/JWTs redacted; not reproduced here.

## Process note

TeleAgent GUI + SAC already running on :4399; `start-teleagent.sh` not needed. No yolo/always-approve. Sandbox-only.
