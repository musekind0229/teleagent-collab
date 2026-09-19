# Windows 整合状态

本分支以 Grok 上游公共框架为基线，合入 Codex 在 Windows TeleAgent 上真机验证过的监督控制器。目标是保留可复现的真实工人证据，同时逐步把能力接入公共 Goal / Task / Run 框架；当前不声称两条状态机已经完全合一。

## 两条路径

| 路径 | 入口 | 当前用途 |
| --- | --- | --- |
| 公共框架 | `src/framework/`、`src/execution_backend/`、`src/teleagent_adapter/` | Goal/Task/Run、预算、依赖、持久化决定、跨平台连接 |
| Windows 监督控制器 | `windows/collab.ps1`、`win_collab/` | 已验证的派工、一次性审批、拒绝、返工、独立验收和两阶段 MSI 系统动作 |

公共框架是后续主线。Windows 监督控制器暂作为兼容及实机验收通道，避免在公共 TeleAgent ExecutionBackend 尚未覆盖审批状态机前丢失已经跑通的能力。不要同时让两条控制器接管同一个 session 或工作目录。

## 本次整合保留和修正的能力

- Windows 监督控制器的 40 项回归及 RustDesk 真机验证记录随代码合入；不会在回归中重装 RustDesk。
- 命令式可插拔组长支持 `system_action` 的 `approve | reject | deny_job`，继续绑定 request ID 和上下文 hash。
- 系统动作的准备与执行提示携带完整工人合同；控制器专用的 MSI 源路径不会下发给工人。
- 上游 `stdin_wrap` 不再按端口终止未知监听进程。只停止本控制器状态文件记录的旧 wrap；未知占用等待后失败关闭。
- Windows 主机运行回归时，POSIX realpath 和“kernel32 不存在”用例按平台跳过，避免把平台假设误报为产品失败。

## 尚未统一的部分

- 公共 ExecutionBackend 还没有把 TeleAgent 权限请求、Question、组长决定和系统动作作为一个完整后端暴露；`bin/run-job.py` 的公共框架路径不能替代 `win_collab` 的全部监督能力。
- 公共 Goal 的计划、预算、依赖和上交决定已有内核能力，但尚未用 Windows 真实 TeleAgent 工人完成目标级端到端验收。
- 两条路径使用不同的持久化格式。当前不迁移在途任务，也不让旧程序读取新状态。
- 普通自动允许工具仍缺少操作系统级强制隔离；审批日志和最终工具轨迹不是完整 capability 沙箱。

## 应用入口预览

本分支新增 `bin/collab-service.py`，外部调用方只提交 Goal、边界和验收条件。框架内部再调用可插拔规划组长、生成 Task 依赖并派给 ExecutionBackend，因此永续层不需要知道具体组长或工人的接口。请求、任务、运行句柄、事件和报告均进入 durable store；入口只绑定本机 loopback，并支持 Bearer token。

当前默认工人仍是确定性的 `inprocess.local_v1`。显式选择 `--backend teleagent-windows` 后，新入口会通过 `teleagent.windows.supervised_v1` 复用旧 Windows 控制器；Grok 组长可负责规划、普通权限和产物验收，Question 与系统动作继续上交外部 decision API。使用方法和 HTTP 契约见 [`application-api.zh-CN.md`](application-api.zh-CN.md)。

2026-09-20 实机验证中，真实 Grok 成功生成两项依赖计划；Windows TeleAgent 桥接真实创建 session `ses_f4443d3f4ffeRzqOFW9zR1dxv6`，随后模型调用失败且无产物。控制器和 Goal 均正确记录失败，辅助 4401 内核在服务退出时关闭，GUI 4398 保持运行。当前剩余阻塞是 TeleAgent GUI 模型登录态/上游授权。

## 后续收敛顺序

1. 用只读 doctor 和隔离 hello 工单验证上游 Windows 连接适配，不启动第二个内核、不重启 GUI TeleAgent。
2. 为 TeleAgent 实现公共 ExecutionBackend，先覆盖普通文件任务及 permission/question，再接返工和独立验收。
3. 把结构化 `system_action` 迁入公共决定协议；保留批准前不投放安装包、批准后重新验 hash、不确定派发不重放等性质。
4. 使用多步骤无系统副作用的目标做 Goal→Task→Run 真机验收；稳定后再决定 `win_collab` 的弃用周期。

## 验证口径

模拟或 fake API 测试只证明状态机行为。Windows live 通过必须记录 TeleAgent 版本、端口、凭据来源类型、真实 session、审批往返及独立产物检查，同时不得输出凭据。系统安装验证不通过重复安装现有软件完成。

### 2026-09-20 本机整合验证

- `win_collab` 回归 41 项通过；公共 Windows 适配、框架及 glue 相关抽测 156 项通过（5 项按平台跳过）；Python compileall 与 diff check 通过。
- 公共 glue 原先仍创建 Linux adapter，并会把 Windows stdin-wrap 从 4401 改回旧的 GUI 地址。本分支改为按当前平台创建适配器，并保留 Windows 适配器在凭据刷新后选择的实际地址；新增回归覆盖。
- `run-job` 第一次真实调用发现状态目录直到独立 `glue.main()` 才创建；本分支改为每次写状态前创建目录，并新增回归覆盖。
- 强制使用受控 stdin-wrap 后，本地 API 成功创建真实 session `ses_f4558b675ffeyRnL0FQJfxCRpz`，但复制的 GUI 模型登录态返回 `40108 invalid token`。任务没有产物，结果正确记为 fail；不算 Windows worker live 闭环通过。
- 试验用 4401 辅助内核已停止并清理；原 GUI TeleAgent 的 4398 监听保持运行。未重装 RustDesk、未执行系统动作、未输出任何 token。
- 完整 `src` 测试发现运行 545 项，其中 24 项未通过、10 项跳过；失败集中在上游 Antigravity/DeepSeek/POSIX 测例对 Windows 路径、脚本执行格式和外部二进制的假设。这些不由本次 Windows glue 修改引入，但说明仓库尚不能宣称全平台全套回归通过。
