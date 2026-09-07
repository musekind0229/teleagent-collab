# 组长适配接口（条3）

外部组长经**结构化协议**读待决、提交决定。不再把 Grok CLI 写死在编排核心；Grok CLI 只是一种 `LeadAdapter` 实现。

## 布局

| 文件 | 作用 |
| --- | --- |
| `src/lead_adapter/base.py` | `LeadAdapter` Protocol / ABC |
| `src/lead_adapter/schema.py` | `build_lead_request` / `validate_lead_decision` / JSON schema |
| `src/lead_adapter/grok_cli.py` | Grok CLI 后端（保留 `--disallowed-tools`，**禁止**失败后去掉限制再重试） |
| `src/lead_adapter/inprocess.py` | 当前 Codex/对话当组长：目录协议 `pending/` + `decisions/`，或 `decision_fn` 进程内回调 |
| `src/lead_adapter/claude_code.py` | **待办 stub**：`call_failed`，禁止假 PASS |
| `src/lead_adapter/codex_cli.py` | **待办 stub**：独立 Codex CLI 进程；`codex` 别名仍→inprocess |
| 工厂 | `get_lead_adapter(kind=...)`；`COLLAB_LEAD_ADAPTER=grok_cli\|inprocess\|claude_code\|codex_cli` |
| `bin/run-live-grok-lead.py` | 真机：创建→权限→GrokCLI→继续→验收 |

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


## Claude / Codex 待办

- `claude_code` / `codex_cli` 仅骨架：返回 `_lead_status=call_failed`，**绝不**自动 `once`/`pass`。
- 对话当组长继续用 `inprocess`（`COLLAB_LEAD_ADAPTER=inprocess` 或历史别名 `codex`）。
- 真机回归集：Grok 见 `bin/run-live-grok-lead.py`；Claude/Codex CLI 接线后替换 stub。
