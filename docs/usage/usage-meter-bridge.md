# TeleAgent HTTP → portal 积分 metering bridge

Date: 2026-09-06 (Asia/Singapore)  
Workspace: ``probe-sandbox/…``  
Account userId: `<teleagent-userId>`

## Executive answer

**How to make HTTP worker sessions count on TeleAI portal 积分:**  
include a GUI-style **`queryID`** (`q_<uuid>`) on every local prompt (`POST /session/:id/prompt_async` or `/message`). Portal records key on that id as `interactionId`. Points are **computed server-side** from model tier (e.g. chat-pro / 旗舰 → 0.05), not from a client USAGE_SYNC token sum.

| Approach | Portal 积分 moves? | Evidence |
| --- | --- | --- |
| HTTP without `queryID` (prior A/B) | **NO** | `usage-ab-final.md` |
| HTTP + `queryID` + chat-lite + `prompt_async` | **NO** (overview unchanged) | `meter-bridge-result.json` |
| HTTP + `queryID` + chat-pro + `/message` | **YES** (+0.05, task/call +1) | `meter-bridge-pro-result.json` |
| HTTP + `queryID` + chat-pro + `prompt_async` | **YES** (+0.05, task/call +1) | `meter-bridge-async-pro-result.json` |
| Replay DB tokens via `POST /api/v1/usage/sync` | API **200 ok**, portal **NO** | `meter-bridge-result.json` experiment B |

**Sync bridge (DB → USAGE_SYNC) did not move 门户积分.** It is the wrong surface for preset-model portal credits under `metering` policy. Use **queryID on the live prompt** instead.

---

## Ranked options (for collab `glue.py`)

### 1) **(A′ / C) Official local field we missed: `queryID` — RECOMMENDED**

- OpenAPI client in GUI bundle already lists `queryID` on `prompt`, `promptAsync`, `command`, `summarize`.
- GUI always sets `queryID: \`q_${Eh()}\`` before `session.prompt(...)`.
- SAC stores it on the user message (`json:"queryID"`) and sends **`X-TeleAI-Query-ID`** on NewApi/ops-gateway calls when `useSuperAgentAuth` is set.
- Portal record `interactionId` **equals** that `queryID`.

**Glue change (minimal):**

```python
import uuid
body = {
    "parts": [{"type": "text", "text": prompt}],
    "model": {"providerID": "NewApi", "modelID": model_id},  # chat-pro if you need visible 旗舰 points
    "agent": "opencowork-default",  # match GUI; optional but used in successful probes
    "queryID": f"q_{uuid.uuid4()}",
}
# POST /session/{id}/prompt_async  (or /message)
```

**Notes for glue:**

- Prefer generating a fresh `queryID` per user turn (GUI does).
- **chat-pro** is proven to create portal rows (+0.05). **chat-lite** + queryID did **not** move overview in our probe (may be free / filtered); do not assume lite shows on 积分用量.
- No password theft, no forged inflated points — metering is server-side from the real NewApi call.
- Replaying old HTTP sessions that never had a queryID cannot invent a past ops-gateway bill; only **new** prompts with queryID will meter.

### 2) **(A) Client-side USAGE_SYNC bridge from teleagent.db — NOT for 门户积分**

- Schema is clear (see below). Server accepted a real-token NewApi payload (`code:0`).
- Portal overview/records **did not change**.
- Under `usage_upload_policy` `mode=metering`, renderer **excludes** official provider `NewApi` from enqueueing USAGE_SYNC (`zse` / `_B`). Portal 积分 for NewApi is **not** driven by this path.
- Still useful only if product later cares about `/api/v1/usage/token-records` / user-snapshot token ledgers (separate from portal stats).

### 3) **(B) Drive GUI for jobs — works, bad for automation**

- Proven by A/B (`usage-ab-final.md`). Heavy, flaky, not suitable for collab glue.

### 4) **(D) Impossible without vendor fix — FALSE**

- Not required for forward metering if glue sends `queryID`.
- Vendor fix would only be needed for: (a) backfilling historical HTTP without queryID, or (b) making chat-lite appear on portal if that is product intent.

---

## Research Q&A (with evidence)

### 1) USAGE_SYNC / token-records payload schema; where are points computed?

**Enqueue trigger (renderer):** finished assistant message with nonzero tokens, when `UPt` decides to call `HZ(...)`.

**Pending localStorage shape** (`usage_pending_<userId>`):

```json
[{
  "messageId": "msg_...",
  "sessionId": "ses_...",
  "producedMs": 1788667255916,
  "generation": 1,
  "payload": { /* posted body */ }
}]
```

**Posted body** (`HZ` → `x9t` → `POST .../api/v1/usage/sync`):

| Field | Type | Source |
| --- | --- | --- |
| `userId` | string | logged-in user id |
| `date` | `YYYY-MM-DD` | from `producedMs` (`sb`) |
| `sessionId` | string | session id |
| `messageId` | string | assistant message id |
| `parentMessageId` | string | parent/user message id |
| `providerId` | string | e.g. `NewApi` |
| `modelId` | string | e.g. `chat-pro` |
| `input` / `output` / `reasoning` / `cacheRead` / `cacheWrite` / `total` | number | message `tokens` |
| `isScheduleTask` | bool | session task meta `taskType==="schedule"` |

**Auth headers** (`dE` + optional `teleagent: BG`): `X-Token`, `X-Timestamp`, `X-Nonce`, `X-App-Version`, `X-OS-Type`, `X-SuperAgent-Device-Id`, `X-Channel-Id`, `X-Runtime-Env`.

**Points / portal:**

- Portal row fields (`modelLevel`, `modelUsage`, `totalUsage`) are **not** in the USAGE_SYNC body.
- Successful GUI/HTTP portal rows show server-assigned `modelLevel: "旗舰"`, `modelUsage: 0.05` for chat-pro.
- In `metering` mode, client **skips** USAGE_SYNC for `providerId === "NewApi"` (`OFFICIAL_PROVIDER_ID`). So **portal 积分 for preset models are server-side** (ops-gateway + query id), not client-computed from USAGE_SYNC.

### 2) Can we POST a valid sync from HTTP session DB tokens and have portal accept it?

- **API accept:** YES — real HTTP-AB tokens → `POST /api/v1/usage/sync` → HTTP 200 `{code:0,message:"ok"}`.
- **Portal 积分:** NO — overview stayed `taskCount/callCount/pointsUsed` unchanged after sync.
- Do not use this to “fix” portal credits; it is not the portal metering pipe for NewApi.

### 3) Does any local HTTP API on :4399 / SAC trigger the same metering path as GUI?

- **Same LLM path:** yes — NewApi + `X-Route-Target: ops-gateway` + `useSuperAgentAuth`.
- **Same portal attribution:** only when the prompt carries **`queryID`** (GUI always does; bare glue `prompt_async` did not).
- Local `POST /usage/statistics` is a **local aggregator**, not cloud portal.
- No separate “enable portal metering” toggle found on :4399 beyond sending `queryID` (and using a model the portal bills).

### 4) Server-side ops-gateway flag / header / session type?

| Signal | Role |
| --- | --- |
| `X-Route-Target: ops-gateway` | NewApi route (always on preset provider) |
| `useSuperAgentAuth: true` | SAC `CompatProvider.SetSuperAgentAuth` injects user auth on LLM HTTP |
| **`X-TeleAI-Query-ID` / message `queryID`** | **Attribution key** → portal `interactionId` |
| `teleagent: BG` | Deployment env header when `BG_ENV=BG` (not the metering switch) |
| Session type HTTP vs GUI | No DB column; difference was missing `queryID` (and historically missing `agent`) |

No evidence of a secret “HTTP sessions are free” flag beyond omitting `queryID`.

### 5) Practical options — see ranked list above.

---

## Mechanism (end-to-end)

```
GUI send:
  queryID = "q_" + Eh()           # UUID-like
  POST /session/:id/message { queryID, parts, model, agent }

SAC:
  persist user message.data.queryID
  NewApi call with useSuperAgentAuth + X-TeleAI-Query-ID

TeleAI ops-gateway / portal:
  bill by query / session → GET /user/portal/usage/stats/record
  interactionId == queryID
  modelLevel/points from model tier (server)
```

HTTP without queryID still burns tokens into `teleagent.db` + `[cost]` logs, but portal never gets an `interactionId` to attach.

---

## Experiment log (this probe)

| Stage (SGT) | Overview | Notes |
| --- | --- | --- |
| Before QID/lite | 1 / 1 / 0.05 | Post-GUI baseline |
| After HTTP chat-lite + queryID + prompt_async (~60s) | 1 / 1 / 0.05 | queryID stored on user msg; **no portal** |
| After USAGE_SYNC of prior HTTP-AB tokens | 1 / 1 / 0.05 | sync **ok**, portal unchanged |
| After HTTP chat-pro + queryID + `/message` (~8s) | **2 / 2 / 0.10** | record `interactionId` = our qid |
| After HTTP chat-pro + queryID + `prompt_async` | **3 / 3 / 0.15** | confirms async path also meters |

Artifacts: `meter-bridge-result.json`, `meter-bridge-pro-result.json`, `meter-bridge-async-pro-result.json`, before/after JSON snapshots.

---

## Recommended `glue.py` path

1. On every worker prompt, set **`queryID=f"q_{uuid.uuid4()}"`** (and ideally `agent="opencowork-default"`).
2. Keep using `prompt_async` — proven with chat-pro.
3. If portal visibility matters, prefer **chat-pro** (or verify chat-lite billing policy with vendor); do not expect lite to show like 旗舰.
4. Do **not** build a DB→USAGE_SYNC backfill for 门户积分; it won’t move portal numbers for NewApi metering.
5. Optional observability: log `queryID` next to `session_id` so portal `interactionId` is correlatable.

## Constraints honored

- Secrets/JWTs redacted in artifacts; token only used ephemerally for portal GETs/POSTs.
- No forged inflated points — only real local token counts / real NewApi calls.
- Sandbox-only; no yolo / always-approve changes.
