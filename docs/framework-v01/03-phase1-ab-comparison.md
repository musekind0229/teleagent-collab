# 阶段 1：内核路径 A/B 比较（只比较，不施工）

同一小案例视角（建议沿用 mini-live：小文件 + 一次弹权 + 收工审查），列**证据缺口**；**未拍板前不建两套完整系统**。

## 方案定义

| | **A：Hermes Kanban 真源** | **B：演进 collab 内核** |
| --- | --- | --- |
| 任务真源 | Hermes Kanban DB / dispatcher | 演进后的 collab Goal/Task/Run 状态库 |
| TeleAgent | 外部 worker lane | `ExecutionBackend` 适配器（现 glue 下沉） |
| 授权/验收 | 扩展服务挂到 Kanban | 现有 hard_rules / lead_adapter / completion 演进 |
| Hermes 角色 | 内核 | 可选后端或上层调用方 |
| 本机 Hermes tip（参考） | `b6f42c66`（**未**固定为产品基线） | 不强制 |

## 同案证据缺口表

| 比较点 | A 现状证据 | B 现状证据 | 缺口（A） | 缺口（B） |
| --- | --- | --- | --- | --- |
| **外部工人** | 文档：external CLI lane **非铺好路径**；需 plugin `spawn_fn` + 自行映射 complete/block（kanban-worker-lanes 原文） | Linux TeleAgent local-v1 **已跑通**迷你真机 | TeleAgent→lane 合同、鉴权、permission 往返全未接 | 需把 TA HTTP 从内核剥到 backend 包 |
| **同模型多实例** | Profile lane / orchestrator lane 文档存在；未在本环境用同一模型拆协调/执行/验收实测 | 现多为「单工人会话 + 外置 lead」；同模型多实例分工 **未产品化** | 多实例+我们的授权绑定未验证 | 需 RoleSpec/AgentInstance 模型与派发 |
| **精确待决恢复** | Kanban `blocked`/`unblock`、claim TTL、run 历史较完整（文档+源码在） | `state_store` + astra 恢复有测；待决与 lead 绑定较严 | 与我们的 Decision.binding / 硬规则如何共存未设计 | 待决类型拆 action/question/review 尚未做 |
| **独立验收** | `kanban_request_review` / reviewer 模型有；默认可派 `sdlc-review` | `force_lead_review` + 产物指纹 **已有**；验收者改产物需新 Run 的语义未完全合同化 | 验收是否调用我们的 artifact_review 适配未知 | 验收实例与执行实例上下文隔离待建 |
| **总预算** | 任务/run 级 runtime、failure breaker 有；**目标级**成本汇总是否满足「子任务+审批汇总」未证实 | `timeout_sec` + ReworkBudget；**目标级**多 Task 汇总弱 | Goal 级预算账本 | 多 Task 预算与预留/对账 |
| **取消 / 未知** | PID 消失 reclaim、max_runtime；取消确认粒度 vs 我们「未确认=unknown 不放资源」需对齐 | abort ≠ cancelled 已强调；真机确认面仍窄 | 与 TeleAgent cancel 语义对接 | 各 backend 统一 cancel 观察接口 |

## 关键判断（设计原文门槛）

> 若 Hermes 必须**大量修改核心**才能保持授权语义 → 倾向 **B**；若扩展接口即可且维护更低 → 倾向 **A**。

### 对 A 的硬风险（已有公开证据）

1. **外部工人未铺好**：官方写明 non-Hermes CLI lane 仍是 per-integration 设计工作，历史 PR 未落地 runner。把 TeleAgent 当成 lane **不是配置开关级**工作。
2. **授权语义错位**：Hermes 默认「审批/完成」面向 Kanban 生命周期；我们的硬规则、白名单、`application_id` 绑定、decision_channel_failed 不重做产物——要保持这些，几乎肯定要在 lane 外包一层**我们的决策服务**，或改 dispatcher 行为；后者接近「改核心」。
3. **双真源风险**：若 Kanban 与 collab state_store 并行，违反「一 Task 一引擎负责人」。A 要求**停写**竞争性调度状态——迁移成本高，但方向干净。

### 对 B 的硬风险

1. 要从 glue/scheduler **抽出**公共生命周期，避免整文件重写踩坑。
2. 同模型多实例、Goal 级预算、第二后端——都要从零产品化（但可沿现合同渐进）。
3. Win 真实分支不在本机，B 仍须单独接 migration 证据，不能靠 WindowsBlockedAdapter。

## 建议（供老板拍板，非开工令）

**建议倾向 B（演进 collab 内核）**，理由：

- 已有可运行的 Linux×TeleAgent 授权/验收闭环与合同钉死经验（`3e4526f`）。
- A 的外部 lane 官方未铺好；接 TeleAgent + 保授权语义 **大概率超出「扩展接口」**，触发设计稿「大改核心 → 选 B」条款。
- Hermes 更适合下一阶段作为**可选执行后端/编排前端**，而不是本轮唯一任务真源。

**若选 A 的前提（拍板条件）**：先做 **2 周内 spike（仍不算完整系统）**：固定 Hermes commit；用最小 plugin 证明 TeleAgent permission 可阻塞在 lane 外决策服务；证明 Kanban 为唯一状态真源且 collab 只读适配。Spike 失败则锁定 B。

**明确不做**：在拍板前同时建设 A、B 两套完整内核。
