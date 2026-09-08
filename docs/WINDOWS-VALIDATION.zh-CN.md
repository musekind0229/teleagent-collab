# Windows 验证记录

日期：2026-09-07。本地分支 `windows-codex-lead`，上游基线 `aa6a9b4`。

## 已验证

- GitHub 仓库已 clone 到 `G:\codex\teleagent-collab`；未向远程推送。
- 原版的两处行为有可执行复现：强制验收仍接受部分产物；相邻目录被路径前缀判断误放行。
- `python -X utf8 -m unittest discover -s tests -v`：26 项通过。
- 测试包括 Windows DPAPI 合成凭据往返、真实 loopback HTTP 的 local-v1 签名、禁止带认证重定向、错误正文不外泄，以及 fake API 的调度/审批/验收/恢复故障场景。
- `python -m compileall -q win_collab` 与 `git diff --check` 通过；PowerShell 启动入口 `status` 返回空任务列表。

## 实机探测

- TeleAgent 界面版本 2.4.1，SAC 本地监听 4397；未经认证的请求返回 HTTP 401 `local_auth_missing`。
- 读取指定 SAC 进程的 Windows 环境块时，找不到 Linux 原脚本依赖的三项本地认证环境变量。没有导出账号 token 或扫描任意进程堆内存。
- 用户明确授权“重启 TeleAgent，并临时开启本机诊断端口，用完关闭”后执行诊断。
- Node `--inspect=127.0.0.1:9235` 没有形成诊断监听。
- Electron `--remote-debugging-address=127.0.0.1 --remote-debugging-port=9235` 同样没有形成诊断监听。
- 安装包 `main.jsc` 的静态字符串中，`remote-debugging-port` 等调试/安全开关与 `removeSwitch` 连在同一段，结合实际运行结果，判断该发行版主动清理这些参数。
- 诊断结束后重新以**无诊断参数**的方式启动 TeleAgent，并检查 9235 没有监听。
- 未修改 TeleAgent 可执行文件、asar、认证机制或全局权限配置；未创建真实 worker session。

## 尚未验证，不能报完成

- Windows 真实 API 认证与会话创建。
- TeleAgent 是否接受/实施 session-local `permission: ask`。
- 真实 pending → 当前 Codex 决策 → API 回复 → 产物 → 独立验收的完整流程。
- Windows 真实三路并行、账号侧并发额度、独立 Codex CLI 组长调用。
- 操作系统级文件/网络/凭据隔离、恶意工人无法篡改控制器状态、abort 后子进程停止。

因此当前交付是**有回归测试的 Windows 控制器预览，不是实机已跑通的软件版本**。
真实集成需要 TeleAgent 提供可合法取得连接信息的接口/插件/受支持的启动方式。仅替换端口、改模型提示词或继续修改 controller 均不能解决客户端认证入口缺失。
