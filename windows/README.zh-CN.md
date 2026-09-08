# Windows 本地执行器（预览）

本地项目：`G:\codex\teleagent-collab`。Python 3.12+，运行时无需 pip 安装依赖。
命令行入口 `windows/collab.ps1`；使用已安装并登录的 TeleAgent，控制器不会创建其他云账号。

## 当前状态

控制器与回归测试已完成，真实 TeleAgent 鉴权仍待接通。**这不是已通过实机验收的发行版。**
本机 TeleAgent 2.4.1 的运行进程没有原版 Linux 脚本所需的认证环境变量；仅改端口无效。
需要通过应用合法提供的本地连接信息完成初始化，再跑 `doctor` 和实际小任务。
已在用户明确授权后验证临时诊断方式：本发行版未开放 Node/Electron 调试入口，安装包存在清理调试参数的逻辑；已恢复无诊断参数启动，9235 未监听。详情见 [实机验证记录](../docs/WINDOWS-VALIDATION.zh-CN.md)。
启动前若 `doctor` 失败，停止于诊断，不会为通单改动全局权限或自动放行。

## 当前对话里的 Codex 当组长

你向这个 Codex 对话说明目标；Codex 写短任务文件、派给 TeleAgent、查看队列并做决定，最后独立验证产物。
你不需要代替组长逐条点击审批。运行程序本身不包含模型；扫描只产生待决请求。

```powershell
# 在仓库根目录运行。也可以直接使用 Python -m win_collab。
.\windows\collab.ps1 doctor
.\windows\collab.ps1 submit .\windows\examples\hello.json
.\windows\collab.ps1 tick --seconds 40
.\windows\collab.ps1 status
.\windows\collab.ps1 inbox
```

`tick` 发现需要组长判断的请求就返回；没有请求时按 2–3 秒间隔扫描活动任务。
再次 `tick` 继续已有 session，不重新派工。没有活动任务时立即退出，不做空轮询。
当前对话结束后，程序不会自动唤醒或冒充这个 Codex 对话；未决任务保存，后续继续可恢复。
任务总时限包括等待组长的时间，不能靠反复启动控制器清零。

组长检查 `inbox` 返回的章程、完整申请、工具记录和产物，写决定文件，然后执行：

```powershell
.\windows\collab.ps1 decide .\windows\local\decision.json
.\windows\collab.ps1 tick --seconds 40
.\windows\collab.ps1 report <job_id>
.\windows\collab.ps1 cancel <job_id>
```

决定文件示例（ID/hash 必须取自对应请求）：

```json
{
  "request_id": "from-inbox",
  "context_hash": "from-inbox",
  "decision": "once",
  "reason": "完整申请只创建本工单约定的 hello.txt，内容与目标一致。"
}
```

- 权限：`once | reject | deny_job`；不接受 `always`。
- 验收：`pass | fail`；必须全部产物存在，hash 未变化，工人 idle。缺证据时应 fail 并给具体返工要求。
- 问题：`answer | deny_job`；answer 另带 `answers: [["第一题回答"], ["第二题回答"]]`。
- 当前控制器不支持凭据/外部目录白名单；需要此类任务时先设计明确授权，不默默放行。

## 换组长

默认是文件队列协议，任何有权限读取队列并按契约输出决定的 agent 都可接入。
`win_collab/lead.py` 还提供两个可选命令适配器：

```powershell
# 明确启动一个独立 Codex CLI 执行，不是当前对话。
.\windows\collab.ps1 lead <request_id> --backend codex --exe "C:\path\to\codex.exe"

# 中立适配：可执行文件 stdin 收 {packet, schema, instruction}；stdout 只输出一个 JSON 对象。
.\windows\collab.ps1 lead <request_id> --backend json-command --exe "C:\path\to\lead-adapter.exe"
```

Codex 适配使用 `exec --sandbox read-only --ephemeral --ignore-user-config --output-schema`。
使用临时控制目录，不把工人目录当作 Codex 项目根目录；完整短章程每次都提供。
不会自动替你选择模型、开第二个付费账号或循环重试失败的组长命令。
一次命令失败不授予权限；真实 CLI 调用兼容性尚未实测。

## 文件与限制

- `.collab-state/controller.sqlite3`：任务状态、审批队列、事件，默认不进 Git。
- `.collab-state/workspaces/<job_id>/`：每单独立的新目录，不覆盖已有项目。
- `.collab-state/intents/`：记录远端启动意图，崩溃后避免不确定的重复派工。
- 可选 `.collab-state/local-api.dpapi`：仅保存 Windows 当前用户加密的本地 API 连接信息；不保存账号登录 token。
- 也支持 `TELEAGENT_URL` 与 `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` / `SUPER_AGENT_LOCAL_SESSION_KEY`，仅用于已合法取得的本地连接信息。不要把值发进聊天或提交仓库。
- `COLLAB_PYTHON` 指定 Python 路径；`COLLAB_AUTH_FILE` 可指定 DPAPI 文件；`--home` 可换控制器数据目录。

当前是**监督式命令行预览**。目录和 session 隔离并非操作系统沙箱，同用户运行的工人仍可能具有更大的系统权限。
本入口请求每个新 session 的 `ask` 策略并要求服务器确认，但是否覆盖所有 Windows 工具/子进程仍须实测。
生产级权限隔离、无人值守后台服务、自动唤醒当前 Codex 对话、精确 token/积分总预算、existing repo/worktree 合并均不在本次已验证能力中。

## 验证

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 review/reproduce_upstream.py
```

测试覆盖真实 loopback HTTP 签名与错误处理、DPAPI 的合成密钥往返，以及 fake TeleAgent 的状态机故障场景。
模拟测试不消耗 TeleAgent 积分，也不能代替账号侧真实运行测试。
