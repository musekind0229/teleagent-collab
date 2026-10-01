# 应用入口 API（v0.1 预览）

> 最新状态：2026-09-20 晚间，桌面 TeleAgent 2.5.2 的外部入口两步文件依赖任务已真实完成；见 [实机验收记录](WINDOWS-LIVE-20260920.zh-CN.md)。下文早期 401 记录针对 stdin-wrap 辅助路径，普通桌面路径请勿加该选项。

这个入口把框架作为一个本机应用使用。调用方只提交目标、边界和验收条件，不需要知道组长或工人的地址：

```text
外部调用方 -> 本机 HTTP 入口 -> 持久化 Goal -> 可插拔组长规划
                                      -> Task 依赖图 -> 工人后端 -> 状态/报告
```

永续层可以是入口的调用方，但不再负责直接联系组长或工人。组长和工人是框架内部可替换的实现。

## 当前边界

v0.1 已提供真实的请求、防重、持久化、规划、任务依赖、派发、异步 Run 恢复、取消、决定回传、事件和报告接口。服务仅允许绑定 loopback；配置 `COLLAB_API_TOKEN` 后，除健康检查外都要求 Bearer token。

默认挂载 `deterministic` 规划器与 `inprocess.local_v1` 后端。适合验证编排与状态机；会按验收清单写出占位产物，不会调用真实外部工具。`--planner grok` 让 Grok 担任规划组长；`--backend teleagent-windows` 把任务交给原 Windows 监督后端。二者同时启用时：Grok 组长可自动处理普通 permission 与 artifact review；Question 一律经外部 decision API 回传；system_action 投影为专类 `system_action_approval`，只经外部 decision API 回传（永不进组长 AUTO_RESOLVE）。

Windows 后端保留旧控制器的 session 级 `ask`、请求去重、同 session 恢复、独立产物验收、取消确认和不确定派发不重放。控制器继续使用自己的纯 ASCII UUID 工作区，避免含中文的 Goal ID 进入 TeleAgent HTTP 头。

同一个持久化目录只运行一个服务实例。运行句柄会在首次派工后立刻持久化；服务重启后会继续观察支持持久句柄的后端，无法恢复的后端会把任务明确标成失败，不会静默重派。当前进程实际具备哪些能力，以 `GET /v1/capabilities` 为准，不要从「没有弹出 permission」推断访问是安全的。

## 启动

在仓库根目录运行：

```powershell
$env:COLLAB_API_TOKEN = '换成一个本机随机值'
python bin/collab-service.py --persist .collab-app --port 8765
```

让 Grok 充当规划组长：

```powershell
python bin/collab-service.py --persist .collab-app --port 8765 --planner grok
```

让 Antigravity（agy CLI）充当工人后端（复用 run-job 池 / 配额 / 503 短冷却；**不是** TeleAgent 监督通道）：

```powershell
# AGY_AUTO_APPROVE 只包住本次进程；用 try/finally，勿留在 shell
$env:COLLAB_API_TOKEN = '换成一个本机随机值'
$env:COLLAB_AGY_ACCOUNT_POOL = (Resolve-Path 'jobs/agy-account-pool.json').Path
$prevApprove = $env:AGY_AUTO_APPROVE
$env:AGY_AUTO_APPROVE = '1'
try {
  python bin/collab-service.py --persist .collab-app --port 8765 `
    --planner deterministic --backend antigravity `
    --agy-account-pool $env:COLLAB_AGY_ACCOUNT_POOL
} finally {
  if ($null -eq $prevApprove) { Remove-Item Env:AGY_AUTO_APPROVE -ErrorAction SilentlyContinue }
  else { $env:AGY_AUTO_APPROVE = $prevApprove }
}
```

边界（诚实）：
- agy 运行句柄只在**本进程内存**；服务重启后对已绑定 `run_id` 的 observe 会失败，协调器把 Task 标失败（`backend resume failed`），**不会**静默重派。
- `reply_permission` 固定 **501 unsupported**（与 inprocess 同形）。没有 TeleAgent 的 permission / question / system_action 逐条回传；hello 烟测靠 `AGY_AUTO_APPROVE` 打开 `--dangerously-skip-permissions`，不是把 skip 映射成 once/approve。
- 账号池在每次 `start_run`（**per-dispatch**）选号+租约；`collect_result` 写回配额/503/冷却并释放 lease。同进程下一 Goal 可换号。一次 Popen 仍钉死该次 HOME（不 mid-run 换）。跨入口切号文件锁强化可另批。

Hermes / 终端调用方可用薄客户端 [hermes-collab-min-client.zh-CN.md](hermes-collab-min-client.zh-CN.md)（`bin/hermes-collab-request.py`：open / status / report / wait / decide）。后端仍由本服务启动参数决定。`decide` 只给人类提交者或操作者提交已经说出口的 verdict；服务拒绝时退出码 `5`，stdout 带上 `code`、`error`，以及有的话 `contamination` / `hint`。

## 推荐入口（桌面 GUI）

生产与日常联调只走**已登录的桌面 TeleAgent**。先探活，再开服务；**不要**加 `--teleagent-stdin-wrap`。

```powershell
python bin/collab-service.py --ready
# 退出码 0 才可派工。busy/unknown 含「不要派工」。步骤见 docs/WINDOWS-DAILY-STARTUP.zh-CN.md
# doctor-only（不看 session 占用）：python bin/collab-service.py --check-gui

python bin/collab-service.py --persist .collab-app --port 8765 `
  --planner grok --backend teleagent-windows
```

也可用 `python -m win_collab ready` 做同一就绪门。`python -m win_collab doctor` 只探活，不读占用。

## 诊断专用：stdin-wrap（非生产）

仅当显式排查「无 GUI 凭据」时才启用受控辅助内核。服务退出时会停止自己创建的辅助内核。该选项只验证了本地 API 和 session 派发；GUI 模型授权复用尚未通过实机验收，**不能作为生产入口**：

```powershell
python bin/collab-service.py --persist .collab-app --port 8765 `
  --planner grok --backend teleagent-windows --teleagent-stdin-wrap
```

启动时输出当前监听地址、规划器、工人后端和持久化目录。默认只监听 `127.0.0.1`。启用 wrap 时 stderr 会打一条 diagnostic-only 警告。

## 提交请求

`acceptance.artifacts` 必须是任务工作区内的相对路径。`idempotency_key` 相同且内容相同会返回原请求；键相同但内容不同返回 `409`。

```powershell
$headers = @{ Authorization = "Bearer $env:COLLAB_API_TOKEN" }
$body = @{
  idempotency_key = 'demo-001'
  client_id = 'eternal-agent'
  title = '生成交付说明'
  goal = '在分配的工作区生成一份交付说明'
  boundaries = @{
    must = @('只写分配的任务工作区')
    must_not = @('不访问凭据', '不修改系统设置')
  }
  acceptance = @{
    artifacts = @('delivery.md')
    text = 'delivery.md 存在'
  }
  budget = @{ wall_sec = 300; max_reworks = 1 }
} | ConvertTo-Json -Depth 8

$opened = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8765/v1/requests' `
  -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$opened
```

返回的 `request_id` 同时是当前版本的 `goal_id`。后台协调循环会自动规划和推进任务，无需调用方手动联系任何 agent。

## 产物内容检查（AIGC 水印 / 不可见字符）

验收不只看文件在不在。协调器在 `collect_result` 返回成功之后、`finish_task` 把任务记成成功之前，会扫描 `result.artifacts` 里每个已经存在的文件（绝对路径，或相对 `result.workspace` 的路径）。这条门禁不看后端：in-process、Antigravity、Windows TeleAgent 都走这里。Windows 控制器在 `review` 的 `pass` 上还有同一套检查；`fail` 返工不受阻挡，工人可以改掉水印再交。

判为污染的内容：

- 文本标记 `AI生成`（`AI` 和 `生成` 之间可以有空白，所以 `AI 生成` 也算）和 `人工智能生成`。不把英文 `AI generated` 当水印，避免英文正文误报。
- 不可见字符：U+200B、U+200C、U+200D、U+2060、U+FEFF（见下）、U+180E、U+2061–U+2064。

编码：只有文件第 0 字节是 UTF-16 BOM（`FF FE` 或 `FE FF`）时才按 UTF-16 解码；否则严格 UTF-8，允许开头的 UTF-8 BOM（`EF BB BF`）。解不开的当作二进制，不扫描，不算污染。

BOM：解码后**第一个字符**如果是那一个来自文件头 BOM 的 U+FEFF，是合法编码标记，不算污染。出现在其它位置的 U+FEFF，或第二个 U+FEFF，算污染。

大于 2 MiB 的文件只扫描开头 2 MiB，扫描结果带 `truncated: true`。

命中且未关闭检查时，任务失败：`result.ok` 变为 false，`error` 以 `artifact_contaminated:` 开头（后面是一行计数，例如 `CONTAMINATED hello.txt: AI生成x1, U+200Bx1926, U+200Dx1658`），并带上 `artifact_contamination`。

关闭检查：在 Goal 的 `acceptance` 里放布尔值 `allow_aigc_marks: true`，或写在该 Task 的 `inputs.allow_aigc_marks`。只有布尔 `true` 生效；字符串 `"true"` 不会关闭检查。服务会把这个选择抄进工人章程。Windows 章程上的同名布尔字段同样放行 `pass`（缺省即检查）。

TeleAgent `review` 若产物自带 `contamination.contaminated`，投影出的决策标题是 `TeleAgent review (CONTAMINATED)`，`details.summary` 是上面那行计数。人工对这条 review 提交 `pass` 而被工人控制器拒绝时，见下面「决定被拒绝」：HTTP `409`，`code` 为 `artifact_contaminated`。

## 后端能力（`GET /v1/capabilities`）

与其它 `/v1` 路由一样要 Bearer（若配置了 `COLLAB_API_TOKEN`）。响应没有密钥。`GET /health` 不要求登录，在原来的 `ok` / `api_version` 之外再给 `backend`（后端 id）和 `planner`（规划器 name）。

`GET /v1/capabilities` 的稳定字段：

| 字段 | 含义 |
| --- | --- |
| `api_version` | 与其它路由相同（`collab-app.v0.1`） |
| `backend.id` / `backend.kind` | 本进程工人后端。kind 是族：`inprocess`、`antigravity_cli`、`teleagent_windows`、`teleagent_linux` |
| `planner.name` / `decomposes` / `lead_review` | 本进程规划器。见下节 |
| `channels.permission` / `question` / `review` | 是否真有 TeleAgent 原生命令通道。agy 与 inprocess 都是 false：`list_pending_actions` 恒为空，`reply_permission` 为 501 |
| `external_inputs.max` | 8 |
| `external_inputs.enforcement` | `permission_gate`（硬拒绝，等人或组长决定）、`prompt_only`（只写进提示词）、`none`（不执行） |
| `isolation.os_sandbox` | 当前实现都是 false。没有容器 / seccomp / Landlock |
| `isolation.access_audit` | `decision_log`（监督后端的决定日志）、false、或 `unknown` |
| `isolation.prompt_constraints` | true 只表示合同文本里有约束，**不是**操作系统沙箱 |
| `skip_permissions` | bool 或 `unknown`。agy 上等于本后端环境里 `agy_auto_approve_enabled(charter=None)`（`AGY_AUTO_APPROVE` / `COLLAB_AGY_AUTO_APPROVE`） |
| `resume` | bool 或 `unknown`。监督后端为 true（同一 state 目录上 `Engine.tick` 能续跑；TeleAgent 实例变了会 fail-closed）。agy / inprocess 句柄只在本进程内存，为 false |
| `acceptance.artifact_presence` | 文件在不在。当前实现为 true |
| `acceptance.exact_content` | 能否核对「path must contain exactly BODY」。只有 agy 门禁为 true |
| `acceptance.lead_review` | 本进程规划器会审 **并且** 后端扛得住组长审查时才是 true |
| `acceptance.executable_checks` | 当前为 false |
| `progress.available` | 当前为 false |
| `usage.source` | agy 为 `worker_self_reported`（CLI JSON 自报）；其它为 `unknown` |
| `warnings` | 人话。agy 且 skip-permissions 时为：`backend runs with skip-permissions: no permission gate; pinned external inputs are prompt-only` |

`unknown` 不等于具备。默认实现（未覆盖的后端）全部是 false / `unknown`，不会假装有门禁。

### 调用方要求的能力

`POST /v1/requests` 可选 `required_capabilities`：字符串数组，只能是下面这些名字。未知名字 → **400** `invalid_request`，不建 Goal。

`permission_gate`、`question_channel`、`review_channel`、`external_input_enforcement`、`os_sandbox`、`access_audit`、`no_skip_permissions`、`lead_review`、`decomposition`。

满足条件（`"unknown"` 一律不算）：

| 名字 | 何时算有 |
| --- | --- |
| `permission_gate` | `channels.permission` 为 true **且** `skip_permissions` 为 false |
| `question_channel` / `review_channel` | 对应 channel 为 true |
| `external_input_enforcement` | `external_inputs.enforcement` 为 `permission_gate` |
| `os_sandbox` | `isolation.os_sandbox` 为 true |
| `access_audit` | `isolation.access_audit` 为 `decision_log` |
| `no_skip_permissions` | `skip_permissions` 为 false |
| `lead_review` | `planner.lead_review` 为 true |
| `decomposition` | `planner.decomposes` 为 true |

不满足 → **409** `code=capability_unavailable`，body 有 `missing`（按请求顺序）和 `capabilities`（同上快照）。检查发生在 `submit_goal` 之前：不落盘、不占幂等键、不派工。同一 idempotency key 随后用一份满足能力的请求仍可建单。

另外一条隐式规则：Goal 带了 `external_inputs`，而后端 `enforcement` 是 `prompt_only` **并且** `skip_permissions` 是 true 时，同样 **409** `capability_unavailable`，`missing` 为 `["external_input_enforcement"]`。除非请求写了 `acknowledge_prompt_only_inputs: true`（操作者承认降级）。承认之后 Goal 上记下警告，`GET /v1/requests/{id}` 的 `warnings` 能看到。这**不**等于满足显式的 `required_capabilities` 里的 `external_input_enforcement`。

`GET /v1/requests/{id}` 增加：

- `warnings`：与本 Goal 相关的能力警告、承认降级、以及「验收文本没有被独立核对」
- `capabilities_ref`：`{"backend": "<id>", "planner": "<name>"}`

### 验收审查状态（`review`）

工人结果上的 `review`：

```json
{"status": "not_requested|passed|failed|unsupported", "source": "agy_exact_content|artifact_review|lead|none", "evidence": "短说明"}
```

| `status` | 含义 |
| --- | --- |
| `not_requested` | 没有要求验收文本 / 审查 |
| `passed` | 真有检查并且过了。`source=agy_exact_content` 是「must contain exactly」字面核对；`artifact_review` 是本地产物审查 |
| `failed` | 检查没过。精确内容不符时任务失败，`acceptance_failed` 为 true，Goal 不会 completed |
| `unsupported` | 要求了验收文本，但没有审查者真的核过（agy + 不能精确核对的散文）。**不是通过**。任务仍可因文件存在而完成，但 `warnings` 含 `acceptance text was not independently verified` |

`force_lead_review` 在 agy 上又没有可核对标准时，门禁照旧 fail-closed，不把审查说成通过。文件都在但精确内容写错：Application API 与 `run_antigravity_job_via_public_api` 都不会成功。污染扫描仍在成功路径上，不因这道门禁取消。

## 规划器

| 启动参数 | `planner.name` | `decomposes` | `lead_review` |
| --- | --- | --- | --- |
| `--planner deterministic`（默认） | `deterministic.single_task` | false | false |
| `--planner lead` / `--planner grok` | `lead_adapter.plan_v1` | true | true |

确定性规划器每个 Goal **只生成一个 Task**，不拆解、不组长审查。验收默认是产物文件在不在；精确内容只在 agy 门禁认得出「must contain exactly」时才核。

组长规划器会拆成有依赖的多个 Task，并审查 permission / artifact review（Question 与 system_action 仍回外部 decision API）。客户端不能选规划器，它由 collab-service 启动参数决定。

敏感或分阶段的活不要指望一张单自动走完。调用方自己分单，后一张的 goal 里写上上一张 `request_id`：

1. **样例**：只要一份可审查的样例（例如 `--artifact sample.md`）。人看过再继续。
2. **实现 + 测试**：新开一单，要求实现和独立测试产物，不要在这一步安装。
3. **dry-run**：再开一单只做演练，不改系统。
4. **操作者批准**：人看过 dry-run 之后才开安装单。Hermes 不自行批准 decision。

## 查询和控制

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查。另含 `backend` id 与 `planner` name，无密钥 |
| `GET` | `/v1/capabilities` | 本进程能力快照（要登录，与其它 `/v1` 相同） |
| `POST` | `/v1/requests` | 提交高层目标。可选 `required_capabilities`、`acknowledge_prompt_only_inputs` |
| `GET` | `/v1/requests` | 列出请求摘要 |
| `GET` | `/v1/requests/{id}` | 查询 Goal、Task、待决定、失败信息、`warnings`、`capabilities_ref` |
| `GET` | `/v1/requests/{id}/events` | 查询持久化事件 |（与 status 对齐露出 pending_decisions）
| `GET` | `/v1/requests/{id}/report` | 获取交付报告 |
| `POST` | `/v1/requests/{id}/cancel` | 请求取消，body 如 `{"reason":"用户取消"}` |
| `POST` | `/v1/requests/{id}/retry` | 仅当终态 `failed` 且 `need_human=true`：doctor 探活后重试失败 Task（见下） |
| `GET` | `/v1/requests/{id}/decisions` | 列出该 Goal 待决 decisions（与 status 同形 public 行） |
| `GET` | `/v1/requests/{id}/decisions/{decision_id}` | 查询单条待决 decision（pending only，同形 public 行） |
| `POST` | `/v1/requests/{id}/decisions/{decision_id}` | 由外部授权者回答决定 |
| `POST` | `/v1/coordinator/tick` | 测试或诊断时手动推进一次 |

PowerShell 会自动对含中文的请求 ID 做 URL 编码；自行拼 URL 的客户端必须对 `{id}` 做 percent-encoding。

## 当前实机结果（2026-09-20）

真实 `grok.exe` 已通过本入口生成两项有依赖关系的计划；修复 Windows 上 Grok UTF-8 输出被系统 GBK 解码的问题后，规划和任务推进完成。

Windows TeleAgent 后端已从新入口真实创建多个 session，证明入口、控制器、本地 HTTP 和 session 派发已经连通。重新登录 GUI 并发送消息后，Chromium Local Storage 确实写入了新的当前记录；辅助内核仍返回 `HTTP 401 / invalid token` 且没有产物。按 LevelDB 当前记录读取、原始扫描和去除 token 类型前缀三种检查均未改变结果，因此反复登录或发送消息不是解决办法，也不应成为日常流程。

同一时间 GUI 自己的 `NewApi/chat-lite` 与 `chat-pro` 调用成功，说明账号和模型可用。剩余差异位于 GUI 主进程向模型内核交接认证状态的私有流程。GUI 内核的本地 API 密钥通过 stdin 注入，不存在于子进程环境；GUI 以管理员权限运行而入口进程为普通权限时，进程检查还会得到 Windows `Access denied (5)`。生产方案需要 TeleAgent 提供受支持的本地 broker/凭据交接接口，或让入口直接运行在能够取得该接口的同一可信宿主中。完成该项后仍需重跑普通文件任务，才可宣布真实交付闭环通过。

permission / question / system_action 经 `POST /v1/requests/{id}/decisions/{decision_id}` 回传到监督后端；Question 与 system_action 永不由组长自动代答/代批（`system_action_approval` 专类）。 回传成功后协调器立即 `process_goal`（响应含 `tick`），无需另调 tick。

### 决定被拒绝（HTTP 409）

工人控制器拒绝这次决定时，`409` 的 `error` 写明原因，而不是只给异常类型名：

- `artifact_contaminated`：review 的 `pass` 命中 AIGC 水印或不可见字符。`error` 为 `artifact contaminated: ` 加上那一行计数（例如 `CONTAMINATED label.txt: AI生成x1`），并说明应 `fail` 让工人重做，或重新开单时在 `acceptance` 里设 `allow_aigc_marks: true`。body 另有 `contamination`（每个文件只含 `aigc_marks`、`invisible`、`encoding` 计数，不含正文）和 `hint`（`allow_aigc_marks`）。
- `worker_decision_rejected`：其它控制器拒绝（固定文案的 `ValueError`）。`error` 为 `worker decision rejected: ` 加上截断并脱敏后的原因。
- `worker_decision_failed`：后端 `BackendError` 同样带上脱敏后的原因。意料外的异常类型仍只返回类型名，避免把内部细节漏出去。

## need_human（Windows 监督恢复失败）

Windows 监督后端在 TeleAgent 重启 / 端口或凭据实例变化 / 会话丢失 / 连续扫描失败 / 桌面 GUI 已被其他控制器占用（`desktop_session_busy`）时会 **fail-closed**：job 进入 `failed`，`error` 以 `need_human:` 开头（不含密钥）。

`need_human:` 原因码还包括：

- `budget_exceeded`：本控制器 store 里记录的 collab session 达到墙钟或步数预算。`failure_reason` 标明 `wall` 或 `steps`，以及观测值和上限（例如 `budget_exceeded wall wall_s=14401 max=14400`）。只 abort 该 session。GUI 上不属于本 store 的会话不检查、不中止。与 charter `timeout_sec` 的 `timed_out` 是两条路径。

调用方怎么看：

1. `GET /v1/requests/{id}` 顶层：
   - `need_human`: bool
   - `failure_reason`: 短原因摘要（已脱敏）
   - `failure`: 若为 need_human，含 `need_human` / `failure_reason` / `phase=worker` / `task_id`
   - `tasks[].result.need_human` / `tasks[].result.failure_reason` / `tasks[].result.error`
2. `GET /v1/requests/{id}/events`：历史里 `finish_task` 在 need_human 时带 `need_human=true`、`failure_reason`、`event_kind=need_human`，可按这些字段检索。（与 status 对齐露出 pending_decisions）

### decisions 不能续跑终态 need_human

`POST /v1/requests/{id}/decisions/{decision_id}` 只回答 **进行中** 工单的 TeleAgent 待决（permission / question 等，任务处于 `awaiting_decision`）。

恢复失败是 **终态** `failed`：此时没有可续的 pending decision，不能用 decisions 把已失败 Goal「续跑」回来。

### 受控重试（failed + need_human）

人工修好桌面 TeleAgent / 凭据后，调用方可以在 **同一 Goal id** 上重试，无需整单重提：

```http
POST /v1/requests/{id}/retry
Content-Type: application/json

{}
```

前置条件（任一不满足 → `409`）：

1. Goal `state=failed`
2. `need_human=true`（顶层 / `failure` / 失败 Task 的 `result`）
3. 连接探活通过：对 GUI TeleAgent 端口（优先 **4399 / 4397 / 4398**）跑 doctor；**不默认** stdin_wrap。探活失败示例：

```json
{
  "ok": false,
  "code": "connection_not_ready",
  "error": "connection not ready for retry: GUI TeleAgent credentials unavailable"
}
```

非 need_human 的普通失败：

```json
{ "ok": false, "code": "not_need_human", "error": "retry only allowed when need_human=true" }
```

成功时优先 **resume/observe** 仍存活的 GUI session；否则只把 **失败 Task** 重新入队（`failed→queued`），Goal 回到 `queued`/`running`，预算与自治度不变，历史追加 `retry_task`，**不擦除** 既有 `finish_task` / need_human 事件。

```json
{
  "ok": true,
  "request_id": "<goal_id>",
  "state": "running",
  "task_id": "<failed_task_id>",
  "mode": "redispatch"
}
```

`mode` 可能为 `resume`（续观察原 run）或 `redispatch`（仅失败 Task 有界重派）。

