# Windows 验证记录

初始日期：2026-09-07。最新实机补充：2026-09-09。当前迁移分支 `windows-integration`，上游基线 `3e4526f`，Windows 合入提交 `a9200a8`。

## 2026-09-09 实机更新

- 找到此前遗漏的凭据来源：SAC 进程已经清理认证环境变量，但 TeleAgent runtime Node 子进程仍保留本机 API 所需三项值。适配器只读取路径严格匹配的 runtime Node 环境块，并验证监听 PID 的镜像严格匹配 SAC。
- `windows/collab.ps1 doctor` 已连接 `http://127.0.0.1:4397`，健康检查为 true，后端版本 1.2.27；没有输出或持久化凭据。
- JSON 小文件工单 `65f9977a7cb7468994f7a0fed1dc2511` 已通过：12 字节、SHA-256 `6bc0da1f42f96fc37b8bd7ed20ba57606d2a0da5cda2b135c7854fbdc985b8a3`，由组长独立解析后批准。
- 文本 hello 工单 `f7428320804b44dca8d50f372f177992` 被取消：`hello.txt` 被注入 1,918 个 U+200B、1,666 个 U+200D 和“AI生成”；返工时工人违反章程使用 PowerShell，并在上下文压缩后卡住。
- 迁移清单工单 `46ae6df68f604c5c9042de8a9fffe069` 完成了真实请求/决定链：工人请求父级 `workspaces/*` 的 `external_directory` 权限，组长因越界回写 `reject`；工人继续并提交清单；初稿因事实与方案错误被 `fail` 退回；修正版经 JSON、引用、源码 hash 和目录完整性核验后 `pass`。
- 实测会话创建会回显 `[{permission:"*",pattern:"*",action:"ask"}]`，但工作区 `write` 与 TeleAgent 自定义 `powershell` 没有产生权限请求；代理配置显示 `powershell=allow`、`bash=deny`、`external_directory=ask`。因此回显不等于有效工具门禁。
- 控制器已增加：越界 external-directory 自动拒绝、`forbidden_tools` 验收门禁、`min_approved_permissions` 零审批防冒充、abort 后远端 idle/消失确认。31 项测试通过。

完整迁移清单保存在本次工单工作区 `migration-inventory.json`，原始 SHA-256 为 `f80ab6222569ddeaa902e53837e48c36d456c7f38853ae6ee6e1875af45badd2`。

## 2026-09-07 当时已验证

- GitHub 仓库已 clone 到 `G:\codex\teleagent-collab`；未向远程推送。
- 原版的两处行为有可执行复现：强制验收仍接受部分产物；相邻目录被路径前缀判断误放行。
- `python -X utf8 -m unittest discover -s tests -v`：26 项通过。
- 测试包括 Windows DPAPI 合成凭据往返、真实 loopback HTTP 的 local-v1 签名、禁止带认证重定向、错误正文不外泄，以及 fake API 的调度/审批/验收/恢复故障场景。
- `python -m compileall -q win_collab` 与 `git diff --check` 通过；PowerShell 启动入口 `status` 返回空任务列表。

## 2026-09-07 当时的实机探测

- TeleAgent 界面版本 2.4.1，SAC 本地监听 4397；未经认证的请求返回 HTTP 401 `local_auth_missing`。
- 读取指定 SAC 进程的 Windows 环境块时，找不到 Linux 原脚本依赖的三项本地认证环境变量。没有导出账号 token 或扫描任意进程堆内存。
- 用户明确授权“重启 TeleAgent，并临时开启本机诊断端口，用完关闭”后执行诊断。
- Node `--inspect=127.0.0.1:9235` 没有形成诊断监听。
- Electron `--remote-debugging-address=127.0.0.1 --remote-debugging-port=9235` 同样没有形成诊断监听。
- 安装包 `main.jsc` 的静态字符串中，`remote-debugging-port` 等调试/安全开关与 `removeSwitch` 连在同一段，结合实际运行结果，判断该发行版主动清理这些参数。
- 诊断结束后重新以**无诊断参数**的方式启动 TeleAgent，并检查 9235 没有监听。
- 未修改 TeleAgent 可执行文件、asar、认证机制或全局权限配置；未创建真实 worker session。

## 仍未验证，不能报完成

- 厂商支持且跨版本稳定的 Windows 外部认证入口；当前是受限兼容发现。
- session-local `permission: ask` 覆盖全部工具；实测已证明当前版本没有覆盖 write/powershell。
- 一次符合章程的真实 `once` 批准后继续执行；现已验证真实 `reject` 回写与继续。
- Windows 真实三路并行、账号侧并发额度、独立 Codex CLI 组长调用。
- 操作系统级文件/网络/凭据隔离、恶意工人无法篡改控制器状态、abort 后子进程停止。

因此当前交付是**已接通实机并跑过监督工单的 Windows 控制器预览**。连接、拒绝、返工和独立验收已验证；对自动允许工具的事前门禁、跨版本兼容和系统安装动作仍需补齐，不能称为生产版。
