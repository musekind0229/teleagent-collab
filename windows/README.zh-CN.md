# Windows 本地执行器（预览）

从仓库根目录运行。Python 3.12+，运行时无需 pip 安装依赖。
命令行入口 `windows/collab.ps1`；使用**已安装并登录的桌面 TeleAgent**（端口发现 4399/4397/4398，默认 :4397），控制器不会创建其他云账号，也**不要**为通单默认启用 stdin-wrap。

本入口是从 Windows 真机验证分支合入的兼容通道；统一推荐入口与双控制器边界见 [Windows 整合状态](../docs/WINDOWS-UNIFIED.zh-CN.md)。应用 Goal API 侧一键探活：`python bin/collab-service.py --check-gui`。

## 当前状态

控制器曾接通本机 TeleAgent 2.4.1 / 后端 1.2.27，并完成真实单工人试运行。**这仍是预览，不是生产级权限沙箱。**
Windows 适配器从已验证的 TeleAgent runtime Node 进程环境中只选取三项本机 API 值，并单独验证 4390–4410 范围内唯一监听进程是已安装的 SAC；值只保存在进程内存，不写报告。
该历史版本的 `doctor` 已返回健康状态。当前 GUI 版本可能不再把本地 API 凭据放进可读取的进程环境；此时该兼容入口应明确失败，改用公共 Windows adapter 的受控凭据通道，不能关闭鉴权绕过。
已在用户明确授权后验证临时诊断方式：本发行版未开放 Node/Electron 调试入口，安装包存在清理调试参数的逻辑；已恢复无诊断参数启动，9235 未监听。详情见 [实机验证记录](../docs/WINDOWS-VALIDATION.zh-CN.md)。
启动前若 `doctor` 失败，停止于诊断，不会为通单改动全局权限或自动放行。当前发现方式是对已安装版本的兼容适配，并非厂商承诺的稳定外部 API；升级后必须重新跑兼容性验证。

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
- 当前控制器不支持凭据白名单。普通外部输入只能通过 `external_inputs` 声明：最多 8 个仓库内普通文件，每个都固定绝对路径和 SHA-256，单文件不超过 512 KiB；文件变化、路径不一致、符号链接/目录联接和凭据类名称都会拒绝。
- `forbidden_tools` 可列出章程禁止使用的工具；工具即使被 TeleAgent 自动执行，带有已完成违规工具的工单也不能通过验收。
- `min_approved_permissions` 可要求通过前至少观察到指定次数的真实 `once` 批准，防止用零审批运行冒充审批闭环。
- `external_directory` 请求只允许本工单精确工作区，或元数据精确指向仍满足路径与哈希约束的 `external_inputs` 文件；其余请求自动拒绝。父级 `workspaces/*`、兄弟工单和仅靠宽泛 pattern 命中的文件均不能由组长覆盖放行。

### Windows 系统安装两阶段门禁

`task_kind=system_install` 使用单独的动作状态机，目前只支持哈希固定的 MSI：

1. 工人先生成 `system-action-request.json` 并停止；批准前若执行 PowerShell/shell，工单失败。
2. 组长只能批准与章程逐字段相同的动作。批准瞬间重新核验源 MSI，随后才复制到工单工作区并恢复同一 session。
3. MSI 必须声明 `elevation=runas`、有限的静默参数、允许效果、敏感效果的 `user_authorized_effects` 和回滚计划。
4. 模糊的恢复请求不会自动重放；系统安装禁止自动返工，最终验收要求已绑定的动作 hash，并检查工具记录中只有一次带 `-Verb RunAs` 的批准调用。

该流程仍依赖 TeleAgent 工人遵守指令，不能替代 Windows 容器或完整 capability 沙箱；但安装包在动作批准前不会进入工单目录，且 TeleAgent 对提权调用会再产生可绑定的 `remote_guard` 请求。

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

当前是**监督式命令行预览**。目录和 session 隔离并非完整的操作系统安全边界；TeleAgent 的 `powershell` 使用 `workspace_offline` 沙箱，但其代理配置明确为自动允许，内置 `write` 也没有产生权限请求。
本入口请求每个新 session 的 `ask` 策略并要求服务器回显。实测证明回显只是必要的协议检查，不能证明所有工具的有效规则都是 `ask`。只有 `/permission` 实际返回且被绑定到本 session 的请求，才算真实审批。
文本写入还可能被 TeleAgent 注入 U+200B/U+200D 与“AI生成”标记；要求原始字节完全相等的工单应改用兼容的产物格式或保持失败，不能暗中剥离水印后通过。
生产级权限隔离、自动唤醒当前 Codex 对话、精确 token/积分总预算、existing repo/worktree 合并均不在本次已验证能力中。RustDesk 的服务安装已验证，但没有配置永久密码或无人值守凭据。

## 验证

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 review/reproduce_upstream.py
```

测试覆盖真实 loopback HTTP 签名与错误处理、DPAPI 的合成密钥往返，以及 fake TeleAgent 的状态机故障场景。
模拟测试不消耗 TeleAgent 积分，也不能代替账号侧真实运行测试。
