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

默认挂载 `deterministic` 规划器与 `inprocess.local_v1` 后端。适合验证编排与状态机；会按验收清单写出占位产物，不会调用真实外部工具。`--planner grok` 让 Grok 担任规划组长；`--backend teleagent-windows` 把任务交给原 Windows 监督后端。二者同时启用时：Grok 组长可自动处理普通 permission 与 artifact review；Question 一律经外部 decision API 回传；system_action 本刀不投影到公共 decision（留在监督后端收件箱，待专刀），协调器不会把 system_action 交给组长或外部 decision 越权批准。

Windows 后端保留旧控制器的 session 级 `ask`、请求去重、同 session 恢复、独立产物验收、取消确认和不确定派发不重放。控制器继续使用自己的纯 ASCII UUID 工作区，避免含中文的 Goal ID 进入 TeleAgent HTTP 头。

同一个持久化目录只运行一个服务实例。运行句柄会在首次派工后立刻持久化；服务重启后会继续观察支持持久句柄的后端，无法恢复的后端会把任务明确标成失败，不会静默重派。

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

## 推荐入口（桌面 GUI）

生产与日常联调只走**已登录的桌面 TeleAgent**。先探活，再开服务；**不要**加 `--teleagent-stdin-wrap`。

```powershell
python bin/collab-service.py --check-gui
# 失败：先打开并登录桌面 TeleAgent，确认 doctor 绿（端口发现 4399/4397/4398，默认 :4397）

python bin/collab-service.py --persist .collab-app --port 8765 `
  --planner grok --backend teleagent-windows
```

也可用 `python -m win_collab doctor` 或 `.\windows\collab.ps1 doctor` 做同一探活。

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

## 查询和控制

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/v1/requests` | 提交高层目标 |
| `GET` | `/v1/requests` | 列出请求摘要 |
| `GET` | `/v1/requests/{id}` | 查询 Goal、Task、待决定和失败信息 |
| `GET` | `/v1/requests/{id}/events` | 查询持久化事件 |
| `GET` | `/v1/requests/{id}/report` | 获取交付报告 |
| `POST` | `/v1/requests/{id}/cancel` | 请求取消，body 如 `{"reason":"用户取消"}` |
| `POST` | `/v1/requests/{id}/retry` | 仅当终态 `failed` 且 `need_human=true`：doctor 探活后重试失败 Task（见下） |
| `POST` | `/v1/requests/{id}/decisions/{decision_id}` | 由外部授权者回答决定 |
| `POST` | `/v1/coordinator/tick` | 测试或诊断时手动推进一次 |

PowerShell 会自动对含中文的请求 ID 做 URL 编码；自行拼 URL 的客户端必须对 `{id}` 做 percent-encoding。

## 当前实机结果（2026-09-20）

真实 `grok.exe` 已通过本入口生成两项有依赖关系的计划；修复 Windows 上 Grok UTF-8 输出被系统 GBK 解码的问题后，规划和任务推进完成。

Windows TeleAgent 后端已从新入口真实创建多个 session，证明入口、控制器、本地 HTTP 和 session 派发已经连通。重新登录 GUI 并发送消息后，Chromium Local Storage 确实写入了新的当前记录；辅助内核仍返回 `HTTP 401 / invalid token` 且没有产物。按 LevelDB 当前记录读取、原始扫描和去除 token 类型前缀三种检查均未改变结果，因此反复登录或发送消息不是解决办法，也不应成为日常流程。

同一时间 GUI 自己的 `NewApi/chat-lite` 与 `chat-pro` 调用成功，说明账号和模型可用。剩余差异位于 GUI 主进程向模型内核交接认证状态的私有流程。GUI 内核的本地 API 密钥通过 stdin 注入，不存在于子进程环境；GUI 以管理员权限运行而入口进程为普通权限时，进程检查还会得到 Windows `Access denied (5)`。生产方案需要 TeleAgent 提供受支持的本地 broker/凭据交接接口，或让入口直接运行在能够取得该接口的同一可信宿主中。完成该项后仍需重跑普通文件任务，才可宣布真实交付闭环通过。

permission / question 经 `POST /v1/requests/{id}/decisions/{decision_id}` 回传到监督后端；Question 永不由组长自动代答。system_action 暂不投影（`backend_gate_unprojected`），待后续专刀。

## need_human（Windows 监督恢复失败）

Windows 监督后端在 TeleAgent 重启 / 端口或凭据实例变化 / 会话丢失 / 连续扫描失败时会 **fail-closed**：job 进入 `failed`，`error` 以 `need_human:` 开头（不含密钥）。

调用方怎么看：

1. `GET /v1/requests/{id}` 顶层：
   - `need_human`: bool
   - `failure_reason`: 短原因摘要（已脱敏）
   - `failure`: 若为 need_human，含 `need_human` / `failure_reason` / `phase=worker` / `task_id`
   - `tasks[].result.need_human` / `tasks[].result.failure_reason` / `tasks[].result.error`
2. `GET /v1/requests/{id}/events`：历史里 `finish_task` 在 need_human 时带 `need_human=true`、`failure_reason`、`event_kind=need_human`，可按这些字段检索。

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

