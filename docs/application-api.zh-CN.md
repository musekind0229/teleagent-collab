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

默认组合是 `deterministic` 规划器和 `inprocess.local_v1` 工人。它用于验证入口与状态机，会按验收清单生成占位产物，不会完成真实开发任务。`--planner grok` 让 Grok 生成任务图；`--backend teleagent-windows` 把任务交给原 Windows 监督控制器。两者同时启用时，Grok 还会自动处理普通 permission 和 artifact review；Question、系统动作或组长调用失败仍进入外部 decision 接口。

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

启用完整的 Grok 组长和 Windows TeleAgent 工人桥接：

```powershell
python bin/collab-service.py --persist .collab-app --port 8765 `
  --planner grok --backend teleagent-windows
```

如果 GUI TeleAgent 没有向当前进程暴露本地 API 凭据，可显式启用受控辅助内核。服务退出时会停止自己创建的辅助内核。该选项目前只验证了本地 API 和 session 派发；GUI 模型授权复用尚未通过实机验收，不能作为生产入口：

```powershell
python bin/collab-service.py --persist .collab-app --port 8765 `
  --planner grok --backend teleagent-windows --teleagent-stdin-wrap
```

启动时输出当前监听地址、规划器、工人后端和持久化目录。默认只监听 `127.0.0.1`。

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
| `POST` | `/v1/requests/{id}/decisions/{decision_id}` | 由外部授权者回答决定 |
| `POST` | `/v1/coordinator/tick` | 测试或诊断时手动推进一次 |

PowerShell 会自动对含中文的请求 ID 做 URL 编码；自行拼 URL 的客户端必须对 `{id}` 做 percent-encoding。

## 当前实机结果（2026-09-20）

真实 `grok.exe` 已通过本入口生成两项有依赖关系的计划；修复 Windows 上 Grok UTF-8 输出被系统 GBK 解码的问题后，规划和任务推进完成。

Windows TeleAgent 后端已从新入口真实创建多个 session，证明入口、控制器、本地 HTTP 和 session 派发已经连通。重新登录 GUI 并发送消息后，Chromium Local Storage 确实写入了新的当前记录；辅助内核仍返回 `HTTP 401 / invalid token` 且没有产物。按 LevelDB 当前记录读取、原始扫描和去除 token 类型前缀三种检查均未改变结果，因此反复登录或发送消息不是解决办法，也不应成为日常流程。

同一时间 GUI 自己的 `NewApi/chat-lite` 与 `chat-pro` 调用成功，说明账号和模型可用。剩余差异位于 GUI 主进程向模型内核交接认证状态的私有流程。GUI 内核的本地 API 密钥通过 stdin 注入，不存在于子进程环境；GUI 以管理员权限运行而入口进程为普通权限时，进程检查还会得到 Windows `Access denied (5)`。生产方案需要 TeleAgent 提供受支持的本地 broker/凭据交接接口，或让入口直接运行在能够取得该接口的同一可信宿主中。完成该项后仍需重跑普通文件任务，才可宣布真实交付闭环通过。

后续还需把 Question 的自动处理策略和两阶段系统动作投影成更专门的公共类型；当前两者会留给外部 decision API，避免组长越权处理。
