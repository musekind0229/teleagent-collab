# 组长适配接口（条3）

外部组长经**结构化协议**读待决、提交决定。不再把 Grok CLI 写死在编排核心；Grok CLI 只是一种 `LeadAdapter` 实现。

## 布局

| 文件 | 作用 |
| --- | --- |
| `src/lead_adapter/base.py` | `LeadAdapter` Protocol / ABC |
| `src/lead_adapter/schema.py` | `build_lead_request` / `validate_lead_decision` / `pin_lead_response_schema` / JSON schema |
| `src/lead_adapter/grok_cli.py` | Grok CLI 后端（保留 `--disallowed-tools`，**禁止**失败后去掉限制再重试） |
| `src/lead_adapter/inprocess.py` | 当前 Codex/对话当组长：目录协议 `pending/` + `decisions/`，或 `decision_fn` 进程内回调 |
| `src/lead_adapter/claude_code.py` | **待办 stub**：`call_failed`，禁止假 PASS |
| `src/lead_adapter/codex_cli.py` | **待办 stub**：独立 Codex CLI 进程；`codex` 别名仍→inprocess |
| `src/lead_adapter/deepseek_harness.py` | DeepSeek harness：**JSON-in/JSON-out 包装**；包装内调 `dsh --profile headless` |
| 工厂 | `get_lead_adapter(kind=...)`；`COLLAB_LEAD_ADAPTER=grok_cli\|inprocess\|claude_code\|codex_cli\|deepseek_harness`（`deepseek` 为别名） |
| `bin/run-live-grok-lead.py` | 真机：创建→权限→GrokCLI→继续→验收。默认 lead bin：`COLLAB_LEAD_BIN`（存在时）→ PATH `grok`/`grok.exe` → `~/.grok/bin/grok.exe`（Win）或 `grok`（posix）→ `/workspace/run-grok.sh`（仅当文件存在）。Win 默认 worker `http://127.0.0.1:4397`，非 Win `:4399` |
| `bin/run-deepseek-lead.py` | DeepSeek 包装（stdin / `--request-file`）→ `dsh --profile headless`；无 bin/key 时 fail-closed |

## 请求（每次决策自包含）

每条申请绑定：

- `application_id`
- `context_summary`
- `task_goal`
- `authorized_scope`（must / 授权范围）
- `prohibitions`（must_not）
- `acceptance_criteria`
- `current_application`（当前权限包或验收包）

**不假定**新进程记得前文。

## 响应

严格 JSON，且必须回绑 `application_id`（建议同时回绑 `context_summary`）。

- 权限：`decision` ∈ `once|reject|deny_job|demand_safe_path` + `reason`
- 验收：`verdict` ∈ `pass|fail` + `reason`

非法输出 / 超时 / 调用失败 → `LeadDecisionError` → **保持待决**（timeout/call_failed）或 **安全拒绝**（非法 JSON）。**禁止**「去掉 disallowed-tools 再重试」的降级（已从 `glue.call_lead` 删除）。

## 当前 Codex 对话当组长

```bash
export COLLAB_LEAD_ADAPTER=inprocess
export COLLAB_LEAD_EXCHANGE=/path/to/exchange
```

1. 编排写入 `pending/<application_id>.json`（含完整 request + prompt）
2. 对话侧读 pending，按 schema 写 `decisions/<application_id>.json`
3. 或在同进程注入 `InProcessLeadAdapter(decision_fn=...)`

不强制每次另起 Codex/Grok 进程。

## 与 glue / scheduler

- `glue.call_lead_request(request, schema=..., cwd=...)` — 首选
- `glue.call_lead(prompt, schema, cwd)` — 兼容旧调用，内部仍走 adapter
- `scheduler` 权限与 `force_lead_review` 验收均走同一协议

## 测例

`src/test_p1_completion_lead.py`（simulated）：绑定校验、inprocess 文件协议、无 disallowed-tools 降级、并行 `force_lead_review`。

`src/test_deepseek_harness_lead.py`（simulated）：`deepseek_harness` 工厂别名、合法 once/pass、非法 JSON / 超时 / spawn 失败 / `application_id` 不匹配 fail-closed；包装 mock `dsh --profile headless` 成功/失败路径。真 dsh+key 的 smoke 默认 skip。


## Windows 控制层（本机 Grok CLI）

工人层 TeleAgent 在 DESKTOP-TBB531F 的 HTTP 端口以发现为准（**4399→4397→4398**；曾见 **:4397**，重启后曾见 **:4398**），凭据仍走 PEB。控制层默认接本机 Grok CLI（`%USERPROFILE%\.grok\bin\grok.exe`），**不要**再把 Linux 的 `/workspace/run-grok.sh` 或 `:4399` 写死成 Win 默认，也不要把默认 `TELEAGENT_BASE_URL` 写死成 4398。

```
set COLLAB_LEAD_ADAPTER=grok_cli
set COLLAB_LEAD_BIN=%USERPROFILE%\.grok\bin\grok.exe
set TELEAGENT_BASE_URL=http://127.0.0.1:4397
python bin/run-live-grok-lead.py
```

未设 `COLLAB_LEAD_BIN` 时，`resolve_lead_bin()`（`src/lead_adapter/grok_cli.py`）按上面的顺序找 `grok.exe`。未设 `TELEAGENT_BASE_URL` 时，工人适配器按 **4399→4397→4398** 发现；lead 脚本示例仍可用 `:4397`。其它平台默认 `:4399`。失败后**禁止**去掉 `--disallowed-tools` 再试。

## Live Grok echo reliability

Live Grok CLI must echo `application_id` and `context_summary` **exactly** (byte-for-byte) from the request. `pin_lead_response_schema` sets JSON Schema `const` on both fields (and keeps `context_summary` in `required`) before `--json-schema` spawn so the model cannot rewrite them.

Do not loosen `validate_lead_decision`: empty `application_id` is `application_id_mismatch`; rewritten `context_summary` (newlines→spaces, added commentary, whitespace normalize) is `context_summary_mismatch`. Production glue/scheduler must not stitch these fields — dry_run-only stitch stays dry_run-only.

## Claude / Codex 待办

- `claude_code` / `codex_cli` 仅骨架：返回 `_lead_status=call_failed`，**绝不**自动 `once`/`pass`。
- 对话当组长继续用 `inprocess`（`COLLAB_LEAD_ADAPTER=inprocess` 或历史别名 `codex`）。
- 真机回归集：Grok 见 `bin/run-live-grok-lead.py`；Claude/Codex CLI 接线后替换 stub。

## DeepSeek harness（`dsh --profile headless`）

适配器 `deepseek_harness`（别名 `deepseek`）与 `grok_cli` 同构：一次 spawn、走 `build_lead_request` / `pin_lead_response_schema` / `validate_lead_decision`，非法 JSON / 超时 / spawn 失败 → `call_failed` 或保持待决。**禁止**假 `once`/`pass`，**禁止**「去掉工具限制再重试」。**不要把 `dsh` 当 TeleAgent 工人。**

调用面仍是 `COLLAB_LEAD_BIN` 指向的可执行包装：stdin（默认）或 `--request-file` 换 JSON 信封（含完整 request + 钉死的 schema + prompt）。包装在有 harness bin 与 key 时调用公网 `dsh` headless，stdout 必须是回绑 `application_id`（建议同时回绑 `context_summary`）的 **collab-lead-v1 决策 JSON**。

`dsh` 默认 stdout = 终答文本（exit 0/1）。`--json` 是事件流，**不是** lead schema；包装不传 `--json`。从 stdout 取**最后一个**合法 JSON 对象，再按 schema / `validate_lead_decision` 检查（至少 `application_id` 字节级回绑、`decision`/`verdict` 合法集）。validate 失败或 dsh 非 0 → 包装 **exit 2**，stdout **禁止**假 `once`/`pass`。

Lead 提示禁止 bash / 写文件 / 任何改仓库的工具，只出 JSON。包装把 dsh 的 cwd 放到临时目录，避免工具落到本仓或 job workdir。

```bash
export COLLAB_LEAD_ADAPTER=deepseek_harness
export COLLAB_LEAD_BIN=.../bin/run-deepseek-lead.py
export COLLAB_DEEPSEEK_HARNESS_BIN=dsh   # 或 npx 包装
export DEEPSEEK_API_KEY=...
```

安装：`npx @deepseek-ai/dsh` 或全局/源码。headless：`dsh --profile headless "task"`（省略任务或任务为 `-` 时从 stdin 读；包装走 stdin `-`）。

无 `COLLAB_DEEPSEEK_HARNESS_BIN` 且 PATH 无 `dsh`、或无 `DEEPSEEK_API_KEY` 时 **fail-closed**（exit 2），stderr 写清缺的是 bin 还是 key。不要 POST `api.deepseek.com`。若环境里已有 grok 的 `COLLAB_LEAD_BIN`，改成此包装，或设 `COLLAB_DEEPSEEK_LEAD_BIN`（优先于 `COLLAB_LEAD_BIN`）。`COLLAB_DEEPSEEK_IO=stdin|file` 仍可用。

测例：`src/test_deepseek_harness_lead.py`（mock subprocess：合法 once/pass、dsh 非 0、非法 JSON、`application_id` 不匹配）。不把 mock 当真实 DeepSeek 验收。本机同时有 `dsh` 与 `DEEPSEEK_API_KEY` 时才会跑 optional live smoke（默认 skip）。
