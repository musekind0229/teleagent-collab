# Hermes 最小派工客户端

薄封装：`bin/hermes-collab-request.py`。只做 Application API 的 HTTP 调用（open / ping / status / report / wait / pending / decide），**不**持有账号池、**不**拉起 agy 子进程、**不**把 Hermes 当账本。

工人后端（`teleagent-windows` / `antigravity` / `inprocess`）在 **`collab-service` 启动时**选定；本客户端的 `--backend` 仅作调用方标注（写入 `caller_backend_hint`），当前服务端会忽略未知字段。完整 API 见 [application-api.zh-CN.md](application-api.zh-CN.md)。

## 环境变量

`COLLAB_API_BASE` 与 `COLLAB_API_TOKEN` 只认这两个 key，不会把 `.env` 里其它变量注入进程环境。取第一个非空值（进程环境先 strip）：

进程环境 > `COLLAB_ENV_FILE`（指向 dotenv 文件）> `HERMES_HOME/.env` > Windows `%LOCALAPPDATA%\hermes\.env`（没有 `LOCALAPPDATA` 则跳过）/ 其它系统 `~/.hermes/.env`。

终端里直接跑脚本不必先 `export`：token 写在上述 `.env` 即可。Hermes 进程启动时也会把 `.env` 载入环境。

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `COLLAB_API_BASE` | 服务根 URL | `http://127.0.0.1:8765` |
| `COLLAB_API_TOKEN` | 有则发 `Authorization: Bearer …` | 空（无 Bearer） |
| `COLLAB_OUTPUT_FULL` | `1` / `true` / `yes` / `on` 时，`status` / `report` / `wait` 打原始载荷（等同该子命令的 `--full`） | 关（打 SUMMARY） |
| `COLLAB_JSON_UNICODE` | `1` / `true` / `yes` / `on` 时输出原始 UTF-8（等同 `--unicode`） | 关（纯 ASCII） |

HTTP 401/403 的 stdout JSON 带 `auth.token_source`（`env` / `dotenv` / `none`）和 `auth.env_file`（路径或 `null`），不含 token。排查看 `auth.token_source`，不要回显 token。密钥不要写进仓库、profile 或本文件示例。

## 子命令

```text
python bin/hermes-collab-request.py ping
python bin/hermes-collab-request.py open --goal "…" [--title …] [--backend antigravity|teleagent-windows]
python bin/hermes-collab-request.py open --goal "…" --require-capability NAME   # 可重复
python bin/hermes-collab-request.py open --goal "…" --external-input PATH --ack-prompt-only-inputs
python bin/hermes-collab-request.py open --goal "…" --external-input PATH   # 可重复，最多 8 个
python bin/hermes-collab-request.py status <request_id> [--full]
python bin/hermes-collab-request.py report <request_id> [--full]
python bin/hermes-collab-request.py wait <request_id> [--timeout 600] [--interval 2] [--full]
python bin/hermes-collab-request.py pending
python bin/hermes-collab-request.py decide <request_id> <decision_id> --verdict …
```

- 始终向 **stdout** 打一行 JSON（默认纯 ASCII，`\uXXXX`；见「输出编码 / PowerShell」）；失败、观察窗口到点、待决策时 JSON 仍打出，**exit ≠ 0**。
- `{id}` 会做 percent-encoding（含中文 Goal id）。
- `status` / `report` / `wait` 默认打下面的 SUMMARY。`--full`（写在子命令后）或 `COLLAB_OUTPUT_FULL=1` 才打服务端原始载荷。
- `open` 的 `--external-input` 先数个数，再读文件、算 SHA-256。多于 8 个时退出码 1，`code=too_many_external_inputs`，不发 HTTP，也不打开文件。服务端同样限制为 8（`src/framework/app_service.py` 的 `MAX_EXTERNAL_INPUTS`）。哈希按块流式计算，不把整个文件读进内存。
- `--require-capability NAME` 可重复，写入 `required_capabilities`。服务不满足时 HTTP 409，`code=capability_unavailable`，stdout 带 `missing`（以及能力快照），退出码 **1**（不是 `decide` 的 5）。未知名字是 400 `invalid_request`，同样退出码 1。两种都不会建 Goal。
- `--ack-prompt-only-inputs` 写入 `acknowledge_prompt_only_inputs: true`。只用于操作者承认「钉住的外部输入在 skip-permissions 后端上只是提示词」。它不满足显式的 `--require-capability external_input_enforcement`。

### `ping`

`GET /health`（无需登录；客户端若已有 token 仍会带上 Bearer）。成功退出码 0：

```json
{"ok": true, "api_version": "…", "base": "http://127.0.0.1:8765", "capabilities": null}
```

接着带认证请求 `GET /v1/capabilities`。返回 200 时，响应对象放在 `capabilities`（字段见 [application-api.zh-CN.md](application-api.zh-CN.md) 的「后端能力」）。路由还不存在（HTTP 404）时静默忽略，`capabilities` 为 `null`，探活仍算成功。传输失败与今天一样：退出码 1，`code=transport_error`。stdout 不含 token。

派敏感活之前先读 `capabilities`。`channels.permission` 为 false、`skip_permissions` 为 true，或 `external_inputs.enforcement` 为 `prompt_only`，都表示**没有** permission gate。没弹出 permission **不**表示访问安全。`isolation.prompt_constraints` 不是操作系统沙箱。私人或敏感数据应停下告诉用户；只有用户明确接受时才加 `--ack-prompt-only-inputs`。`review.status` 为 `unsupported` 时不要说审查已通过。

### `pending`

`GET /v1/requests`，只列出**非终态**请求，供新的 Hermes 回合找回还在等的单。退出码 0。每行只有：

| 字段 | 含义 |
| --- | --- |
| `request_id` | 请求 id（服务端行上的 `request_id`，否则 `goal_id`） |
| `state` | 非 `completed` / `failed` / `cancelled` |
| `awaiting_decision` | 是否有待决 |
| `updated_at` | 优先 `updated_at_iso`，否则 `updated_at` |

### `wait` 退出码

轮询 `GET /v1/requests/{id}`。`--timeout` 是**客户端观察窗口**（默认 600 秒），不是任务自己的墙钟预算。终态（`completed` / `failed` / `cancelled`）优先；遇到待人工决策**立即返回**，不再睡到超时。Hermes 侧建议切片 `--timeout 300`（低于宿主工具上限约 420 秒），对同一个 `request_id` 重复 `wait`，直到终态或 need_human。观察窗口到点**不是**任务失败，不要因此 re-open 或 retry。进程重启后用 `pending` 找回。TUI 没有推送，把 `request_id` 告诉用户即可。

| 退出码 | `wait.kind` | 含义 |
| --- | --- | --- |
| 0 | `completed` | `state=completed` |
| 1 | （无；传输/API 错误仍是原始错误 JSON） | HTTP / 传输 / 非法 JSON，或响应 `ok=false` |
| 2 | `task_failed` 或 `task_cancelled` | `state=failed` 或 `cancelled`。失败文本是目标自己的墙钟预算时，另有 `wait.task_timeout=true` |
| 3 | `observation_timeout` | 观察窗口到点。非终态时 `wait.task_still_running=true`。`wait.resume` 是下一条 `python bin/hermes-collab-request.py wait <id> --timeout <N>`。`wait.note` 写明这不是任务失败，禁止 re-open / retry |
| 4 | `need_human` | 需要人拍板，立即返回 |
| 5 | — | 仅 `decide`：服务拒绝（HTTP 409） |

`wait.task_timeout` 只在退出码 2、且失败/错误文本里已经出现服务端写好的墙钟标记时为 true。对照 `win_collab.budget_exceeded_reason`（`budget_exceeded wall` / `wall_s=`）以及章程 `timeout_sec` 记成的 `timed_out`、`deadline exhausted`、`wall clock`、`budget_exhausted`。步数预算（`budget_exceeded steps`）**不会**置这个标志。不要自己编一个百分比或超时。

exit 4 在原 status 上追加字段（`ok` 保持服务端原值）。默认打出的是 SUMMARY，其中 `wait` 仍是下面这个对象（并多一个 `kind`）；`--full` 才把整份原始 status 和这个 `wait` 一起打出。

```json
{
  "ok": true,
  "code": "need_human",
  "need_human": true,
  "state": "blocked",
  "wait": {
    "terminal": false,
    "need_human": true,
    "timed_out": false,
    "kind": "need_human",
    "reason": "pending_decisions",
    "state": "blocked",
    "decision_ids": ["dec-1"],
    "decisions": [
      {
        "decision_id": "dec-1",
        "kind": "system_action_approval",
        "title": "需要批准安装",
        "task_id": "task-1",
        "status": "pending",
        "reason": "",
        "summary": "需要批准安装"
      }
    ]
  }
}
```

`wait.reason` 只有三种：

- `pending_decisions`：status 的 `pending_decisions` 非空；
- `awaiting_decision`：`awaiting_decision` 为真，或 `pending_decision_count>0`（行可能还没挂在 status 上，会再 GET 一次 `/decisions` 补 `decision_ids`；这次失败则 `decisions` 为 `[]`）；
- `task_awaiting_decision`：某个 `tasks[].status=="awaiting_decision"`。没有决策行时 `decisions` 为 `[]`，并带 `wait.task_ids`。

`decisions[].summary`：依次取第一条非空文本——`title`（仅当非空且不等于 `kind`）、`details.summary`、`details.message`、`details.reason`、`details.question`、决策行 `reason`、`lead_error.message`、`title`、最后是 `kind`。压成单行并截断到 200 字。`decisions[].reason` 是决策行上的 `reason`（没有则为 `""`）。

### SUMMARY（`status` / `report` / `wait` 的默认输出）

键名稳定。不包含 goal 合同、`desired_outcome`、工人回复正文、stdout / stderr、产物 preview。服务端若带了 `warnings` 或 `capability_warnings`，合并成顶层 `warnings` 列表原样穿过（每条自由文本截断）。载荷里若有 `code`（例如 `need_human`）则保留。

| 键 | 含义 |
| --- | --- |
| `ok` | 服务端 `ok`；缺省视为 true |
| `request_id` | `request_id`，否则 `goal_id` |
| `state` | 目标状态 |
| `terminal` | `state` 是否为 `completed` / `failed` / `cancelled` |
| `need_human` | 顶层布尔；没有该键时再看 `failure` 和 task result |
| `failure_reason` | 失败原因，自由文本最多 300 字 |
| `failure_code` | 仅当有 `failure_code` / `error_class` / `failure.code` 时出现 |
| `pending_decisions` | 决策摘要：`awaiting`、`summary`、`kind`、`decision_id`（以及 title / task_id / status / reason） |
| `awaiting_lead_count` / `awaiting_human_count` | 服务端计数；没有则按行上的 `awaiting` 统计（缺 `awaiting` 的旧行算 human） |
| `tasks` | `{task_id, title, status, artifacts（名字）, workspace, error, review}`。`error` 最多 300 字，不取 stdout/stderr。`review` 是 `{status, source, evidence}`：服务端任务结果里有 `review` 就照抄（文本截断）；没有则 `status=not_requested`、`source=none`、`evidence=""`。`unsupported` 不是通过 |
| `artifacts` | `{task_id, path, size?}`。`size` 只在已知 `size` / `bytes` / `nbytes` 时出现 |
| `progress` | 没有真实进度字段时固定 `{"available": false, "phase": "unknown"}`。服务端若给了 `phase` / `percent` 等才照抄；**不发明百分比** |
| `usage` | 某条 task `result.usage` 存在时 `{"source": "worker_self_reported", "values": {...}}`（只有一条时 `values` 就是该对象；多条按 `task_id` 分开）。否则 `{"source": "unknown"}`。**不相加** |
| `warnings` | 仅当服务端发了 `warnings` 或 `capability_warnings` |
| `http_status` | 有则带上 |
| `truncated` | 任一自由文本被截断则为 true，否则 false |
| `full_hint` | 固定 `"rerun with --full"` |
| `wait` | 仅 `wait` 子命令：上面的观察窗口 / 终态 / need_human 块 |

自由文本（失败原因、任务标题、error、警告、路径、进度里的字符串）按字段截到 300 字；被截到才把 `truncated` 设为 true。决策 `summary` 仍是原来的单行 200 字。

### 一行示例

```powershell
$env:COLLAB_API_BASE = 'http://127.0.0.1:8765'
# $env:COLLAB_API_TOKEN = '本机随机值'   # 若服务启用了 token
python bin/hermes-collab-request.py open --goal '在工作区写 delivery.md' --artifact delivery.md --backend antigravity
```

## 输出编码 / PowerShell

默认一行**纯 ASCII JSON**：中文等非 ASCII 写成 `\uXXXX`。Windows PowerShell 5.1 里，直接赋值按 `[Console]::OutputEncoding` 解码，管道 / `Out-File` 则经 `$OutputEncoding`（默认 us-ascii）转码。纯 ASCII 在任意代码页、任意管道下都不会把中文变成 `?` 或乱码；Hermes 按 UTF-8 读子进程输出时也安全。

`ConvertFrom-Json` / `json.loads` 之后字段是正常中文。不要对原始 JSON 文本做字符串匹配中文。

```powershell
$r = python bin/hermes-collab-request.py status <id> | ConvertFrom-Json
# 或
$x = python bin/hermes-collab-request.py status <id>
$x | ConvertFrom-Json
```

想在终端里直接看中文，加全局参数 `--unicode`（放在子命令前，与 `--http-timeout` 同级），或设 `COLLAB_JSON_UNICODE=1`（`true` / `yes` / `on` 也可以）。Windows PowerShell 5.1 需先把输出改成 UTF-8，否则控制台会把 UTF-8 当 GBK 解成乱码：

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# 管道或 Out-File 时还要：
$OutputEncoding = [Text.Encoding]::UTF8
python bin/hermes-collab-request.py --unicode status <id>
```

## 本机 Hermes 怎么调

事实（Win）：`C:\Users\Admin\AppData\Local\hermes\bin\hermes.exe`（约 v0.21.3）。Hermes 通过 **terminal / code_execution** 跑本脚本。已提供 Hermes skill：[`integrations/hermes/skills/teleagent-collab/SKILL.md`](../integrations/hermes/skills/teleagent-collab/SKILL.md)，安装与加载依据见 [integrations/hermes/README.zh-CN.md](../integrations/hermes/README.zh-CN.md)。

推荐：

1. 先起好 `python bin/collab-service.py … --backend antigravity`（或 `teleagent-windows`），见应用入口文档。
2. 在 Hermes oneshot / 会话里让它执行等价命令，例如：

```text
hermes -z
# 或 oneshot：在 prompt 里要求「用 terminal 调用仓库里的 python bin/hermes-collab-request.py …」
```

提示词片段（可直接贴）：

```text
用仓库 C:\Users\Admin\src\teleagent-collab 下的
python bin/hermes-collab-request.py
派一单：ping → open → 记下 request_id → wait --timeout 300（同一 id 重复，直到终态或 need_human；观察窗口到点不是失败）→ report。
重启后用 pending 找回未结束的单。默认读 SUMMARY；要原始 JSON 再加 --full。
环境变量 COLLAB_API_BASE / COLLAB_API_TOKEN 已在 shell 中时不要回显 token。
```

PowerShell 等价（无 Hermes 时人工验）：

```powershell
cd C:\Users\Admin\src\teleagent-collab
python bin/hermes-collab-request.py --help
python bin/hermes-collab-request.py ping
$opened = python bin/hermes-collab-request.py open --goal 'hello' --artifact delivery.md | ConvertFrom-Json
python bin/hermes-collab-request.py wait $opened.request_id --timeout 300
python bin/hermes-collab-request.py report $opened.request_id
python bin/hermes-collab-request.py pending
```

## 单测

不依赖真 service：

```powershell
# 仓库根；PYTHONPATH=src
python -m test_hermes_collab_request
```

## 非目标（本刀不做）

- （已交付另刀）collab-service per-dispatch 换号见 [agy-account-pool.md](agy-account-pool.md)
- 观察切面大改、把 Hermes 做成状态库
- 把 `AGY_AUTO_APPROVE` 写进用户 profile
