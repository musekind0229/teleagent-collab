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
- 验收：`pass | fail`；必须全部产物存在，hash 未变化，工人 idle，且内容没有 AIGC 水印或不可见字符。缺证据时应 fail 并给具体返工要求。`fail` 返工不看这条内容检查。
- 内容检查：`AI生成`（可含空白）、`人工智能生成`，以及 U+200B、U+200C、U+200D、U+2060、U+FEFF（文件头单独一个 BOM 除外）、U+180E、U+2061–U+2064，会使 `pass` 失败，错误以 `Artifact content contaminated` 开头。章程 `allow_aigc_marks: true` 关闭检查。解不开的二进制不扫描。
- 问题：`answer | deny_job`；answer 另带 `answers: [["第一题回答"], ["第二题回答"]]`。
- 当前控制器不支持凭据白名单。普通外部输入只能通过 `external_inputs` 声明：最多 8 个仓库内普通文件，每个都固定绝对路径和 SHA-256，单文件不超过 512 KiB；文件变化、路径不一致、符号链接/目录联接和凭据类名称都会拒绝。
- 提交时先用上述规则校验**原始**路径。通过后，每个文件被复制到 `<控制器主目录>/external-inputs/<job_id>/<序号>/<原文件名>`（一个文件一个子目录，TeleAgent 的 `/*` 通配符只能覆盖这一个文件）。复制件的 SHA-256 必须与钉住的哈希一致，否则提交失败并删掉该工单的复制目录。此后章程里生效的 `external_inputs` 是这些副本路径；原始路径留在 `external_inputs_source` 供审计。副本是控制器刚写出来的，即使 `store.home` 在仓库外，也不再套用「必须在本仓库内」——原始路径只校验这一次，不要对改写后的章程重跑 `validate_charter`。`charter_hash` 是改写后章程的摘要（含副本路径和 `external_inputs_source`），不是调用方提交的原始章程。
- 工人提示要求只读 `CHARTER.external_inputs` 里的副本绝对路径，不要读目标正文或其它字段里提到的路径。
- 工单进入终态（`passed` / `failed` / `cancelled` / `timed_out`）时删除 `<store.home>/external-inputs/<job_id>` 这一个目录：路径解析后必须仍在 `external-inputs` 里面，不跟随符号链接或目录联接。成功记事件 `external_inputs_cleaned`。清理出错只记事件，不让 `tick` 崩溃。`tick` 还会清扫副本目录还在的终态工单；目录已经没了则什么都不做。
- `forbidden_tools` 可列出章程禁止使用的工具；工具即使被 TeleAgent 自动执行，带有已完成违规工具的工单也不能通过验收。
- `min_approved_permissions` 可要求通过前至少观察到指定次数的真实 `once` 批准，防止用零审批运行冒充审批闭环。
- `external_directory` 请求只允许本工单精确工作区，或元数据精确指向仍满足路径与哈希约束的**副本**（章程 `external_inputs`）；原始路径不能靠批准放行。其余请求自动拒绝。父级 `workspaces/*`、兄弟工单和仅靠宽泛 pattern 命中的文件均不能由组长覆盖放行。
- 待决的 `external_directory` 会在决策上附带 `details.scope`（不写进随后做摘要比对的 TeleAgent permission 对象）：每个 pattern 的非通配前缀目录、该目录里实际有的文件（`/*` 只列这一层；`**` 递归且最多 50 条，多了标 `truncated`），以及 `only_pinned`（列出的文件是否都是本工单钉住的副本）。`details.summary` 形如 `permission: external_directory <pattern> [only pinned] -> dir contains 1 file(s): ext-input.txt`，或 `... [NOT ONLY PINNED] -> dir contains 3 file(s): a, b, c`。`NOT ONLY PINNED` 表示这个目录里还有副本以外的文件，批准 `once` 会让工人读到它们。

### TeleAgent 有效权限（桌面 2.6.0）

创建 session 时控制器提交 `[{"permission":"*","pattern":"*","action":"ask"}]`，服务器会原样回显。回显只表示这条会话策略被承认，**生效的是 agent 级规则**。`GET /config` 里 `agent.opencowork-default.permission` 实测为：`powershell=allow`、`question=allow`、`bash=deny`，`external_directory` 为 `{"*":"ask", ...}`。没有启用 yolo / auto-allow（日志里没有 `[permission] auto-allow`）。

因此：

- 工作区内的读、写、编辑和 PowerShell 执行不会产生 permission 请求。
- 仍会询问的只有 `external_directory`（访问工作区以外的路径）。
- 没有写进章程 `external_inputs` 的外部路径由 `hard_reject()` 直接拒绝，事件是 `hard_reject`，不会进入待决队列。Application API 以前不能传 `external_inputs`，所以经这套 API 产不出 permission 决策。
- 要得到可回传的 permission 决策，在 Goal 上钉住该文件：`external_inputs` 最多 8 项，每项只能是 `{"path": <绝对路径>, "sha256": <64 位十六进制>}`。客户端写法是 `python bin/hermes-collab-request.py open --external-input PATH`（可重复）。客户端解析绝对路径并计算 SHA-256；文件不存在则退出码 1，`code` 为 `bad_external_input`。文件是否在仓库内、哈希是否一致、是否为链接、是否超过 512 KiB，仍由 Windows `validate_charter` 把关；通过后控制器改用上面的单文件副本路径。antigravity 与 inprocess 忽略该字段。
- 权限决策的 summary 会带上该目录的 scope。看到 `NOT ONLY PINNED` 时，目录里不只有钉住的副本，不要把 `once` 当成「只读那一个文件」。
- agent 规则 `question=allow` 下，question 工具产生的是 `question` 决策，不是 permission 请求。

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
