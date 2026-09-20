# Windows 应用入口实机验收（2026-09-20）

TeleAgent 桌面 2.5.2、内核 1.2.27，普通权限启动。使用现有桌面 worker（本次 4397），未使用 stdin-wrap。Grok Build 负责规划和 Task 验收，TeleAgent 负责实际文件操作。

## 已通过的范围

外部 HTTP 请求 → Grok 规划两个依赖 Task → A 写 seed.json → 组长验收 A → 框架交接已验收文件 → B 实际 read seed.json 并写 derived.json → 组长验收 B → Goal completed。

- Goal：`goal_Two_dependent_JSON_files_seed_then_deriv_1415703c`
- A session：`ses_f40e7550affeVIehunSCR4Zffo`
- B session：`ses_f40e68a81ffemKQVYhn2KMoIL7`
- 两个 Task 为 succeeded，两个控制器工单为 passed。
- B 的 completed read 指向自己 UUID 工作区内交接的 seed.json；两份 JSON 的 value 一致，derived 的 ok=true。
- 显式 forbidden_tools 为 bash、powershell、shell；本次工具记录中没有其 completed 调用。
- 本次测试服务 8774 已停止，未停止桌面 TeleAgent。
- 70 项相关回归通过（test_app_service、tests.test_windows_engine、tests.test_windows_client）。

原实测脚本漏识别工具输入 filePath，曾将 read_evidence 记为空而误报失败。补充字段解析后，对同一批持久化记录做离线核验通过，没有重新生成产物。现场原始结果保留，独立核验另存 independent-verification.json；这些文件在测试主机交接目录，不含在仓库发布物中。

## 代码变化

每个工人接收本 Task 指令，保留目标约束；显式 forbidden_tools 经过契约与持久化传递。后继只接收同 Goal 已成功直接依赖中声明且已验收的文件，来源限定在真实运行工作区；拒绝越界路径、链接和冲突内容。重复交接允许相同内容，不覆盖不同内容。

Task 验收使用本 Task 产物条件，并附完整 Goal 与其他 Task 上下文；后续 Task 的明确要求不直接作为当前 Task 必须完成的产物。待审决定可以继续调用组长，对可重试错误有次数上限，登录或额度错误交给外部处理。

## 使用与限制

按 application-api.zh-CN.md 启动，选择 `--planner grok --backend teleagent-windows`，不加 `--teleagent-stdin-wrap`。先运行 `python -m win_collab doctor`。本轮证明普通文件任务可小范围试用，不代表系统操作或长期无人值守已验收。

- forbidden_tools 是完成后的违规检测，不是操作系统强制隔离。
- 依赖交接当前最多 32 个文件、每个不超过 512 KiB；不支持目录整体交接，文件名冲突拒绝。
- Task A/B 文本归属是有限的提示处理；复杂任务宜明确 Task 指令和产物，不代表任意自然语言约束都能形式化验证。
- 此闭环以各 Task 验收推进 Goal，不等于额外独立的全局语义验收层。
- 工人重启、端口及凭据轮换后的在途恢复未完成实机验收；管理员/普通权限混用仍可能影响连接。
- stdin-wrap 的模型授权 401 属于另一条未通过的辅助路径，不能据此断言桌面路径不可用。
