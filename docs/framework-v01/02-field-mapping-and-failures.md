# 旧新字段映射 + 失败分类（阶段 0）

## 1. 旧 charter / collab → 新合同

| 旧字段 / 概念 | 新位置 | 映射规则 | 禁补规则 |
| --- | --- | --- | --- |
| `name` | `Goal.title` / `goal_id` 派生 | 可生成稳定 id | 不得省略 `boundaries` |
| `goal` | `Goal.desired_outcome` | 原文 | — |
| `must` / `must_not` | `Goal.boundaries.must/must_not` | 逐条拷贝 | 空 must_not  ≠ 允许一切 |
| `allow_secret_globs` / `allow_paths` / `allow_keys` | 同名边界字段 | **原样**，含 `[]` | **缺失字段不得补成通配允许**；缺省=未授权 |
| `done_when` / `acceptance` | `Goal.acceptance` + `Task.done_when` | 产物列表进 Task | 无验收标准不得自动 succeeded |
| `timeout_sec` | `Goal.budget.wall_sec` | 数值拷贝 | 不得静默放大 |
| `max_reworks` | `Goal.budget.max_reworks` | 缺省沿用旧 glue 默认但须写入合同 | 返工不得靠新建 goal_id 清零预算 |
| `force_lead_review` | `Task.inputs.force_lead_review` + 验收角色 | 布尔 | 不得因省额度默认关掉安全收工 |
| `task_kind` / `install_roots` / `network_allow` / `user_gate_permissions` | `Task.inputs` + 能力要求 | 保留 | 系统动作不得映射成「普通文件任务」 |
| `lead_review_steps` | 决策 kind 触发配置 | permission→`action_approval`；job_end→`artifact_review` | — |
| session / permission HTTP 字段 | **仅** TeleAgent 后端适配器 | 公共 Run 只存 `native_handle` | 内核状态机不解析 TeleAgent JSON |
| `application_id` + `context_summary` | `Decision.binding` | 旧路径继续严格回显；新路径可用 `context_digest` | **禁止**非法响应事后补齐字段当批准 |
| `jobs/runs/*/status.json` | `get_report` / Event 投影 | ok/state/error → Run/Task 终态 + error_class | — |
| 并行 `job_id` / workdir | `Run.workspace_id` + resource claim | 一 Task 一引擎负责人 | 禁止两套状态库同时拥有同一 Task |

样例：`contracts/charter-to-goal.example.json`（mini-live-hello）。

## 2. 失败分类 → 重试哪一段

| error_class | 含义 | 可重试 | 不可做 |
| --- | --- | --- | --- |
| `implementation_failed` | 工人实现未达产物/工具失败 | 新 Run（同 Task） | 偷偷扩大授权 |
| `acceptance_failed` | 产物在或哈希变了 / 语义验收 fail | 返工 Run 或重验收 | 把 lead 协议错误算验收失败次数混进业务返工（应分账） |
| `decision_channel_failed` | 审批超时、缺字段、绑定错误、非法 JSON | 有限重试审批/换审批实例 | **不得**因此让工人重写已完成产物 |
| `backend_unavailable` | :4399 宕、未登录、unsupported | 换后端或 `awaiting_user` | 伪装成功 |
| `budget_exhausted` | 墙钟/返工/lead 次数用尽 | 需用户扩预算 | 清零编号绕过 |
| `awaiting_user` | 登录、扩权、真待决 | 等 `resolve_decision` | 模糊回复批一串不相关动作 |
| `unknown` | 取消未确认、启动响应丢失、心跳丢 | 先对账/核实副作用 | 把重试当回滚；释放可能冲突资源给新 Run |

对照旧 collab 现象：

- `application_id_mismatch` / `context_summary_mismatch` → **`decision_channel_failed`**（已在 `3e4526f` 钉 schema；仍属决策通道）。
- 整单 `lead_review` fail 但产物齐 → 优先归验收/决策，不默认「实现失败重做文件」。
- TeleAgent 未登录无 :4399 → `backend_unavailable` 或 `awaiting_user`。

## 3. 状态对照（简）

| 旧 glue 大致状态 | 新 Task/Run |
| --- | --- |
| 跑工人中 | Task `running` / Run `running` |
| 等权限/lead | `awaiting_decision` |
| 产物齐等 lead_review | Task `review` |
| ok | `succeeded` |
| fail + 可归类 error | `failed` + error_class |
| abort 未确认停 | `cancel_requested` → 确认后 `cancelled`，否则 `unknown` |
