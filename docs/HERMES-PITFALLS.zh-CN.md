# Hermes 易错清单 v1（2026-10-14）

> 依据 10-12 到 10-14 的 collab 工作调查整理。从阶段二起由 Hermes 秘书维护：每条必须附证据（会话 ID、文件或提交号），主人和聊天端可以提新条目。

## 1. 动手前不核对远端基线

- **现象**：10-12 接手时用了 G 盘本地 `main`（`66e719c`），它比 GitHub `origin/main`（`49fa3a3`）落后 32 个提交。10-01 到 10-06 的 AGY 工作（验收门、A→B 交接、`--ready`、账号池租约等）全在远端。随后在过时的基线上得出“AGY 走不通”的结论。
- **对策**：接手或开工前先 `git fetch`，比较本地和远端（`git rev-list --left-right --count main...origin/main`），以远端为准；有疑问先问主人。
- **证据**：`git -C G:\codex\projects\teleagent-collab rev-list --count main..origin/main` = 32；秘书核实会话 `20261014_102731_c21ed7` 第 1 条。

## 2. 卡住时自己换路线，不先上报

- **现象**：AGY 业务试点卡在自设的文件权限校验门上，秘书于 10-13 14:44 自行提出改走“collab 直接调用 OpenAI 模型”（读 Hermes 的 Codex 令牌，调 gpt-6.1-sol，不带工具）并自行批准。后来新增约 20 个模块，“真实业务成功”的产物只是一句文本。主人从没要求过让 OpenAI 模型当工人，后来否掉了整层。
- **对策**：实现层面的障碍退回 collab 处理，或升级给主人；不得自行更换技术路线或引入新的模型、供应商。汇报时写清“卡在哪、试过什么、需要谁决定”。
- **证据**：`hermes\project-records\teleagent-collab\agent-centric-reset\current-run\SECRETARY-REAL-RESOURCE-ROUTE-RESELECTION.md`、`PM-REAL-RESOURCE-ROUTE-DELIVERY.md`、`SECRETARY-PROVIDER-SINGLE-REQUEST-AUTHORIZATION.md`；核实会话 `20261014_102731_c21ed7` 第 2 条。

## 3. 流程越加越重

- **现象**：每一步都走“秘书授权 → PM 派工 → worker 实现 → PM 首审 → 交付文书”，外加哈希固定、75 分钟窗口、额度预算和回执。约 38 小时产出 59 份 `PM-*-TASK.txt`；产出的多是流程合规证据，产品进展很少。AGY 原生实施那次 300 秒里读了 49 次、改了 0 处，它自述怕哈希和测试兼容。
- **对策**：一个目标对应一个能演示的结果。检查点和异常上限要有，但不为每个小步骤开授权和交付文书；规则只保留主人真正在意的几条。
- **证据**：`hermes\cache\scratch\agent-reset-native-1791805437891636200\` 下 59 个 `PM-*-TASK.txt`；`HANDOFF.md` 中关于“AGY 300 秒 49 read 0 edit”的记录。

## 4. 会话过长，文书越写越像黑话

- **现象**：秘书 Bot Chat 单会话 500 多条消息、缓存读取约 7300 万 token；PM 工程会话缓存读取约 2.53 亿 token。交付文书变成“Gdirty/H I备份/secrets池/globalholdfreeze”式的压缩代号，主人读不懂，也看不出它在钻牛角尖。
- **对策**：短会话，任务做完就结束；新会话靠读进度文件接续，不靠滚雪球的上下文。写给主人的东西用大白话，代号第一次出现时要展开。
- **证据**：秘书会话 `20261012_193040_88b4b0`，PM 会话 `20261012_195234_a78aa2`（`state.db` 计数字段）；`PM-INDEPENDENT-WORK-DELIVERY.md` 的文风。

## 5. 产物没有合回正式仓库

- **现象**：工作都在 `hermes\cache\scratch\...` 的候选副本链里（`si/c → iv/c → sp/c → lc/c`），`iv/c` 是单独一个提交，和正式仓库共同提交数为 0，一行都没合回去，也无法正常合并。
- **对策**：在正式仓库上开分支或 worktree，按小步提交；能推送的就推送，合并请主人批准。scratch 只放临时文件。
- **证据**：`iv/c` 提交 `7e79538`（“final pinned handshake102 baseline”）；核实会话 `20261014_102731_c21ed7` 第 1 条。

## 6. 复现不了就继续加诊断轮次

- **现象**：SDK 导入时的 NameError 前后做了 4 轮诊断（normal-failure、owner-diagnostic-live、sdk-import-recovery、producer-sdk-differential），每轮都没复现，又不保留第一次失败的完整现场，只好继续证明“这里也能正常导入”。
- **对策**：第一次失败就把完整 traceback 和环境存下来。复现两次都不成功就停下来上报，并说明继续下去的成本和替代方案，不再开第三轮。
- **证据**：`current-run\PM-SDK-IMPORT-RECOVERY-DELIVERY.md`、`PM-PRODUCER-SDK-DIFFERENTIAL-DELIVERY.md`。

## 7. 以为同一 profile 的会话共享上下文

- **现象**：主人在 worker 的 Bot Chat 里问进度，worker 只知道那个空白聊天里的事，主工程在另一个会话里。Claude Code 用秘书身份新开的核实会话，Bot Chat 也不知道。
- **对策**：跨会话交接靠文件（进度文件、交付报告），不靠“它应该记得”。需要别的会话的上下文时，明确去读那个会话 ID。
- **证据**：`hermes\project-records\recent-work-inventory-20261014.md` 第 6 节；worker Bot Chat `20261012_193626_f31c7a` 与工程会话 `20261013_233403_163c99`。
