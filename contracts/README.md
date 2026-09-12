# 公共合同草案 v0.1-draft（阶段 0）

版本：`contract.v0.1-draft`  
原则：缺失必需字段 **不得**补成无限授权；公共内核不解析 TeleAgent 专属 HTTP。

JSON Schema 文件：

| 文件 | 概念 |
| --- | --- |
| `goal.schema.json` | Goal 目标合同 |
| `task.schema.json` | Task 工单 |
| `run.schema.json` | Run 执行尝试 |
| `decision.schema.json` | Decision 决策记录 |
| `event.schema.json` | 事件 |
| `error-class.schema.json` | 失败分类枚举 |

旧 charter → 单工单 Goal 映射样例见同目录 `charter-to-goal.example.json` 与上级 `02-field-mapping-and-failures.md`。

## Code entry (path B knife 1)

- Python map: `src/framework/charter_map.py` → `map_charter_to_goal_task`
- Lifecycle vocabulary: `src/framework/lifecycle.py`
- Opaque TeleAgent handle helper: `src/teleagent_adapter/native_handle.py` (HTTP fields stay out of Goal/Task state)

## Execution backends

- `teleagent.linux.local_v1` — existing TeleAgent worker (unchanged entrypoints).
- `inprocess.local_v1` — deterministic second backend under `src/execution_backend/` (public observe/start/collect; not Hermes ledger).
