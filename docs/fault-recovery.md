# 故障恢复与真实测试（条6）

## 持久化

`src/state_store.StateStore`（默认根目录 `jobs/state/<run_id>/`，gitignore）：

| 文件 | 内容 |
| --- | --- |
| `jobs.json` | 任务状态、session、dispatch_token、墙钟、handled 权限 id |
| `pending.json` | 待决权限项（open / decided / abandoned） |
| `decisions.json` | 已发送决定（防重启重发） |

调度器 `ParallelScheduler(persist=True)` 默认启用；单测可 `persist=False`。

## 重启后保证

| 保证 | 机制 |
| --- | --- |
| 不重复派工 | `dispatch_token` + `claim_session` + `should_dispatch`；`restore_from_store()` 标记 `restored=True`，只监控不重 prompt |
| 不重复发送决定 | `already_decided(permission_id)` → skip；`handled_perm_ids` 从 store 回填 |
| 不误接管旧会话 | 活跃 job 已 claim 的 `session_id` 不可被另一 job 再 claim |

## 取消 / 超时

| 状态 | 含义 |
| --- | --- |
| `cancel_requested` | **取消请求已收到**（执行可能仍在跑） |
| `cancelled` | **执行已停止**（仅本 `job_id`） |
| `timeout` | 本任务墙钟耗尽；**不影响兄弟任务** |

API：`scheduler.request_cancel(job_id)` → `effect_cancel(job_id)`（refresh 时对 `cancel_requested` 自动 effect）。

## 回归覆盖（模拟）

`src/test_p2_auth_recovery.py`：

- 跨会话误审批（session 过滤 / claim）
- 多路径并行隔离
- 返工审批 + ReworkBudget
- 回写失败（reply POST 失败不 mark handled）
- 缺产物不成成功
- 超时仅本任务
- 重启恢复（不重派、不重发决定）
- 取消请求 vs 执行停止
- 条5：file vs install、user_gate、install_roots

## 真机路径

见交付报告中的测试表。本机 Linux TeleAgent **2.5.0**、SAC HTTP `:4399`；lead 可用 `COLLAB_LEAD_ADAPTER=inprocess` 或 grok。  
卡住（登录/会话）须如实标阻塞，勿假装通过。

## Astra P1 修复（ac1279a 复核）

- 取消/超时：`effect_cancel` 与墙钟超时先 `POST /session/{id}/abort`；abort 失败保留 `cancel_requested` / `stop_pending_confirm`，不得把请求收到当成已停止。
- 恢复合同：`JobRecord` 持久化完整 charter（must/must_not/产物/force_lead_review/授权/预算）+ `contract_version`；缺失或不兼容 → restore **阻塞**（FAIL），禁止空约束续跑。
- 回归：`src/test_astra_p1_fixes.py`、`review/reproduce_ac1279a.py`。
