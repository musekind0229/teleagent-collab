# Usage A/B Final — GUI vs HTTP (TeleAgent portal metering)

Date: 2026-09-06  
Workspace: ``probe-sandbox/…``  
Account / userId: `<teleagent-userId>` (TeleAgent owner)  
Cloud base: `https://agent.teleai.com.cn/superCowork/sapi`  
Auth: existing login `X-Token` (redacted in artifacts)

Markers:
- HTTP: `USAGE-AB-HTTP-20260906`
- GUI: `USAGE-AB-GUI-20260906` (reply `PONG.`)

Screenshot (GUI chat succeeded):  
``<local-path>``

---

## Executive conclusion

| Path | Local tokens recorded? | Portal overview/records move? |
| --- | --- | --- |
| **HTTP-only worker** (`127.0.0.1:4399` / NewApi) | **YES** | **NO** |
| **GUI chat** (TeleAgent desktop session) | **YES** | **YES** |

**Portal metering per path**

1. **HTTP-only worker bypass of GUI/portal credit dashboard: YES** — confirmed. After the HTTP A/B job, portal `taskCount` / `callCount` / `pointsUsed` and records stayed at zero while local `teleagent.db` gained ~24983 tokens.
2. **GUI path meters on portal: YES** — confirmed. Within ~3–4 minutes of the GUI chat, portal overview became `taskCount=1`, `callCount=1`, `pointsUsed=0.05`, with a matching usage record for the GUI `sessionId`.

Primary metering signal for the desktop “积分用量” surfaces is **renderer-side sync** (`USAGE_SYNC` / portal quota APIs), not the mere fact that NewApi LLM calls ran on the local worker.

---

## Stage table (portal numbers)

| Stage | Time (Asia/Singapore) | taskCount | callCount | pointsUsed | records.total | Local DB sum_total | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Baseline** (before HTTP) | 2026-09-06 11:59:10 | **0** | **0** | **0** | **0** (`list: null`) | 4,711,461 | `usage-ab-baseline.json` |
| **After HTTP** | 2026-09-06 12:00:03 | **0** | **0** | **0** | **0** (`list: null`) | 4,736,444 (+24,983) | `usage-ab-after-http.json` |
| **After GUI** | 2026-09-06 12:04:06 | **1** | **1** | **0.05** | **1** | 4,769,192 | `usage-ab-after-gui.json` (immediate query; no wait needed) |

`USER_SNAPSHOT` (`/api/v1/user-snapshot?...`) returned `40101 missing super agent security headers` on this `X-Token`-only path at every stage — **not** used as the A/B signal. Overview + records are authoritative here.

### After-GUI portal record (detail)

```json
{
  "sessionId": "ses_f8b1fbe1cffe1eFzycZ1bjZphV",
  "interactionId": "q_01a074e0-4213-7d78-9c11-bcff1ff67bc3",
  "statTime": "2026-09-06T12:00:55+08:00",
  "modelLevel": "旗舰",
  "modelUsage": 0.05,
  "tools": "-",
  "toolUsage": 0,
  "totalUsage": 0.05
}
```

Matches the GUI session id and completion time (~12:00:55 SGT).

---

## Path details

### HTTP job (Part 1)

| Field | Value |
| --- | --- |
| session_id | `ses_f8b212a8dffenW1b7eCTJ878iW` |
| model | NewApi / **chat-lite** |
| when | 11:59:15 → 11:59:22 Asia/Singapore |
| assistant tokens | input 24946, output 37, **total 24983** |
| portal delta | **none** (still all zeros ~40s later) |

### GUI job (Part 2 — already done before this probe)

| Field | Value |
| --- | --- |
| session_id | `ses_f8b1fbe1cffe1eFzycZ1bjZphV` |
| title | `AB-GUI使用报告` |
| directory | ``probe-sandbox/…`` |
| prompt | `USAGE-AB-GUI-20260906 please reply with one short sentence only.` |
| reply | `PONG.` |
| model (from server log) | NewApi / **chat-pro** (旗舰) |
| when | 12:00:49 → 12:00:55 Asia/Singapore |
| assistant tokens | input 30116, output 84, **total 30200** |
| cost log | `[cost] provider=NewApi tokens(in=30116 out=84 cacheRead=0) cost=$0.000000` |
| portal delta | **taskCount 0→1, callCount 0→1, pointsUsed 0→0.05**, records 0→1 |

Note: local DB also includes an intervening `_SYS_MEMORY_DAILY_LOG_` session (~2548 tokens) between HTTP and GUI; that did **not** create a portal record. Only the GUI session appears in portal records.

---

## Local evidence (GUI)

- **teleagent.db**: GUI session + messages present; assistant `msg_074e042350010GfcSWnSGLmP1p` holds the 30200-token usage blob.
- **Logs**: `super-agent-server-*.log` shows Prompt start for `ses_f8b1fbe1…` and matching `[cost]` line at 04:00:55 UTC / 12:00:55 SGT.
- **LevelDB** (`Partitions/owner%3Av1_public_…/Local Storage/leveldb`):
  - `usage_upload_policy_*` still `mode=metering`
  - GUI / HTTP session and message ids **absent** from pending strings (consistent with successful sync already cleared, or never left pending)
  - No leftover `USAGE_SYNC` pending payload found for these probe ids

Artifacts: `gui-session-detail.json`, `leveldb-after-gui.json`, `gui-cost-log-hits.txt`, `usage-ab-after-gui-meta.json`.

---

## Clear YES/NO answers

| Question | Answer |
| --- | --- |
| Does **HTTP-only** worker usage show on TeleAI portal overview/records? | **NO** |
| Does **GUI** NewApi chat show on TeleAI portal overview/records? | **YES** |
| Does HTTP skip LLM execution / local token accounting? | **NO** (local DB + `[cost]` still record tokens) |
| Does HTTP skip (or never enter) **GUI credit / portal metering**? | **YES** |
| Same-account A/B conclusive for dashboard metering? | **YES** |

---

## Risks of HTTP-only worker bypass

1. **Credit/quota dashboard under-count** — automation, collab glue, or any client that only hits `127.0.0.1:4399` can burn NewApi/preset-model capacity while the GUI “积分用量” stays flat. Operators looking only at the portal will under-estimate spend.
2. **Policy / plan mismatch** — UI copy says preset models consume credits; HTTP path still invokes the same NewApi/ops-gateway stack locally, but **does not** drive the renderer `USAGE_SYNC` path that feeds portal stats. Billing may be (a) GUI-sync-only for displayed credits, (b) separately metered server-side, or (c) both — this A/B proves **(a) for the portal surfaces we queried**, not full server-side ledger completeness.
3. **False “free automation” assumption** — even if portal points stay 0, upstream TeleAI may still rate-limit, log, or bill ops-gateway traffic. Do not treat portal zeros as proof that HTTP is free end-to-end.
4. **Inconsistent observability** — local `teleagent.db` / `[cost]` logs grow; portal does not. Monitoring only one side produces wrong alerts (either silent overuse or phantom “no usage”).
5. **Compliance / chargeback gaps** — if product intent is “all preset desktop usage is credited,” HTTP workers are a metering hole unless server-side enforcement is added (or workers are forced through the same sync path as the GUI).

---

## Artifacts

| File | Role |
| --- | --- |
| `usage-ab-baseline.json` | Portal + local DB before HTTP |
| `usage-ab-after-http.json` | Portal + local DB after HTTP |
| `usage-ab-after-gui.json` | Portal + local DB after GUI (immediate; already non-zero) |
| `usage-ab-after-gui-immediate.json` | Same first query |
| `usage-ab-after-gui-meta.json` | GUI session + LevelDB + portal summary |
| `usage-ab-partial.md` | Part 1 write-up |
| `usage-ab-final.md` | This document |
| `ab-http/job-result.json` | HTTP session/token delta |
| `usage-findings.md` | Earlier auth/API pattern notes (reuse; secrets redacted) |

---

## Method notes

- Portal GETs used the same paths as Part 1: `/user/portal/usage/stats/overview`, `/user/portal/usage/stats/record?page=1&pageSize=10`, and best-effort `user-snapshot`.
- GUI portal query succeeded on the **first** attempt (~3–4 min after GUI completion); the planned 60–90s re-wait was **not** required. Immediate + final numbers are the same non-zero set.
- Secrets/JWTs redacted; token material not reproduced here.
