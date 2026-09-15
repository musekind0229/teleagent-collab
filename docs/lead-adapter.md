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
| `src/lead_adapter/deepseek_harness.py` | DeepSeek harness：**JSON-in/JSON-out 包装**；**真 harness 未接线验收** |
| 工厂 | `get_lead_adapter(kind=...)`；`COLLAB_LEAD_ADAPTER=grok_cli\|inprocess\|claude_code\|codex_cli\|deepseek_harness`（`deepseek` 为别名） |
| `bin/run-live-grok-lead.py` | 真机：创建→权限→GrokCLI→继续→验收 |
| `bin/run-deepseek-lead.py` | DeepSeek 示例包装（stdin / `--request-file`）；无真 harness 时 fail-closed |

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

`src/test_deepseek_harness_lead.py`（simulated）：`deepseek_harness` 工厂别名、合法 once/pass、非法 JSON / 超时 / spawn 失败 / `application_id` 不匹配 fail-closed；示例包装未接线验收。


## Live Grok echo reliability

Live Grok CLI must echo `application_id` and `context_summary` **exactly** (byte-for-byte) from the request. `pin_lead_response_schema` sets JSON Schema `const` on both fields (and keeps `context_summary` in `required`) before `--json-schema` spawn so the model cannot rewrite them.

Do not loosen `validate_lead_decision`: empty `application_id` is `application_id_mismatch`; rewritten `context_summary` (newlines→spaces, added commentary, whitespace normalize) is `context_summary_mismatch`. Production glue/scheduler must not stitch these fields — dry_run-only stitch stays dry_run-only.

## Claude / Codex 待办

- `claude_code` / `codex_cli` 仅骨架：返回 `_lead_status=call_failed`，**绝不**自动 `once`/`pass`。
- 对话当组长继续用 `inprocess`（`COLLAB_LEAD_ADAPTER=inprocess` 或历史别名 `codex`）。
- 真机回归集：Grok 见 `bin/run-live-grok-lead.py`；Claude/Codex CLI 接线后替换 stub。

## DeepSeek harness（**真 harness 未接线验收**）

适配器 `deepseek_harness`（别名 `deepseek`）与 `grok_cli` 同构：一次 spawn、走 `build_lead_request` / `pin_lead_response_schema` / `validate_lead_decision`，非法 JSON / 超时 / spawn 失败 → `call_failed` 或保持待决。**禁止**假 `once`/`pass`，**禁止**「去掉工具限制再重试」。

DeepSeek harness 的真实 CLI argv / 线上 API **未接线**。调用面是 `COLLAB_LEAD_BIN` 指向的可执行包装：stdin（默认）或 `--request-file` 换 JSON 信封（含完整 request + 钉死的 schema + prompt），stdout 必须是回绑 `application_id`（建议同时回绑 `context_summary`）的决策对象。

```bash
export COLLAB_LEAD_ADAPTER=deepseek_harness   # 或 deepseek
export COLLAB_LEAD_BIN=/workspace/teleagent-collab/bin/run-deepseek-lead.py
# 若环境里已有 grok 的 COLLAB_LEAD_BIN，请改成此包装，或：
# export COLLAB_DEEPSEEK_LEAD_BIN=/path/to/wrapper   # 优先于 COLLAB_LEAD_BIN
# export COLLAB_DEEPSEEK_IO=stdin    # 或 file
```

`bin/run-deepseek-lead.py` 是示例包装：读信封、拒绝猜测 DeepSeek CLI 参数或 POST 线上 API；即便设置了 `COLLAB_DEEPSEEK_HARNESS_BIN` 也 **fail-closed**（exit 2，stdout 不写 `once`/`pass`）。接线真 harness 时替换该包装，保持 JSON-in/JSON-out 与 `application_id` 回绑，不要改 glue 主路径。

测例：`src/test_deepseek_harness_lead.py`（模拟合法 once/pass、非法 JSON、超时/spawn 失败、application_id 不匹配）。不把 mock 当真实 DeepSeek 验收。
