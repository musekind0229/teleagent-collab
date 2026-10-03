# v0.3 阶段1 第三刀：§5.3 平台锁（workdir_claim + persist_lock）

仓库：musekind0229/teleagent-collab  
基线 HEAD：`156fb06`（当前 main）  
设计：`/workspace/agent-exec-framework-v0.3.md` §5.3  

你是实现 agent。改码、加测试、提交并 `git push origin main`。工坊只派收，不要等它改内核。

## 问题

1. `src/execution_backend/workdir_claim.py` **顶层** `import fcntl`：Windows 连 inprocess 入口都 import 失败。
2. `src/framework/persist_lock.py`（§5.1）：Linux `fcntl.flock`，Windows 未实现 / 直接报错。本刀一起补。

## 必须

- 锁与进程实现**下沉到平台服务**，按平台加载（lazy / 工厂）。模块顶层不得无条件 `import fcntl`。
- **不许用无锁空实现**糊 Windows 或「import 成功但互斥失效」。
- Windows：用等效互斥（如 `msvcrt.locking` / `LockFileEx` via ctypes / portalocker 同类语义），保证跨进程排他。
- Linux：同目录独立进程互斥；不同目录可并行；异常退出占用按**已有已验证语义**恢复（不要静默改语义）。
- 原有回归必须绿：`src.test_framework_v03_s51`、`src.test_framework_v03_s52`、`src.test_framework_b12`、`src.test_framework_b14`（及 workdir_claim 既有测若有）。

## 验收

- Linux（本机必做）：
  - workdir_claim：同目录两进程互斥；不同目录可并行；异常退出占用可按既有语义恢复。
  - persist_lock：§5.1 跨进程 RMW 测仍绿。
  - 上述回归全绿。
- Windows：
  - **若无 Windows 真机**：在 commit message / 收工说明明确标「Windows 导入/锁语义未验收」，不要假装过了。可用单元测在非 Windows 上 mock 平台层，但不得把 mock 写成已验真机。
  - 若有条件：Windows 能 `import` 相关模块并跑 inprocess；锁语义实测。

## 不做

工人池、阶段2、部署、自动换组长、§5.2 合同语义回改（除非为接平台锁做最小接线）。

## 交付

1. 实现 + 测试（建议 `src/test_framework_v03_s53.py`）。
2. 清晰 commit（§5.3 平台锁；注明 Windows 验了没）。
3. `git push origin main`。
4. 收工写清：SHA、Linux 测试命令与结果、Windows 验了没。

开始前确认基线 `156fb06`。勿提交 `.grok-prompts/`、jobs 脏文件。
