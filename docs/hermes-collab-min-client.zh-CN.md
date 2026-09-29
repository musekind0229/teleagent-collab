# Hermes 最小派工客户端

薄封装：`bin/hermes-collab-request.py`。只做 Application API 的 HTTP 调用（open / status / report / wait），**不**持有账号池、**不**拉起 agy 子进程、**不**把 Hermes 当账本。

工人后端（`teleagent-windows` / `antigravity` / `inprocess`）在 **`collab-service` 启动时**选定；本客户端的 `--backend` 仅作调用方标注（写入 `caller_backend_hint`），当前服务端会忽略未知字段。完整 API 见 [application-api.zh-CN.md](application-api.zh-CN.md)。

## 环境变量

`COLLAB_API_BASE` 与 `COLLAB_API_TOKEN` 只认这两个 key，不会把 `.env` 里其它变量注入进程环境。取第一个非空值（进程环境先 strip）：

进程环境 > `COLLAB_ENV_FILE`（指向 dotenv 文件）> `HERMES_HOME/.env` > Windows `%LOCALAPPDATA%\hermes\.env`（没有 `LOCALAPPDATA` 则跳过）/ 其它系统 `~/.hermes/.env`。

终端里直接跑脚本不必先 `export`：token 写在上述 `.env` 即可。Hermes 进程启动时也会把 `.env` 载入环境。

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `COLLAB_API_BASE` | 服务根 URL | `http://127.0.0.1:8765` |
| `COLLAB_API_TOKEN` | 有则发 `Authorization: Bearer …` | 空（无 Bearer） |

HTTP 401/403 的 stdout JSON 带 `auth.token_source`（`env` / `dotenv` / `none`）和 `auth.env_file`（路径或 `null`），不含 token。排查看 `auth.token_source`，不要回显 token。密钥不要写进仓库、profile 或本文件示例。

## 子命令

```text
python bin/hermes-collab-request.py open --goal "…" [--title …] [--backend antigravity|teleagent-windows]
python bin/hermes-collab-request.py status <request_id>
python bin/hermes-collab-request.py report <request_id>
python bin/hermes-collab-request.py wait <request_id> [--timeout 600] [--interval 2]
```

- 始终向 **stdout** 打一行 JSON（默认纯 ASCII，`\uXXXX`；见「输出编码 / PowerShell」）；失败、超时、待决策时 JSON 仍打出，**exit ≠ 0**。
- `{id}` 会做 percent-encoding（含中文 Goal id）。

### `wait` 退出码

轮询 `GET /v1/requests/{id}`。终态（`completed` / `failed` / `cancelled`）优先；遇到待人工决策**立即返回**，不再睡到超时。

| 退出码 | 含义 |
| --- | --- |
| 0 | `state=completed` |
| 1 | HTTP / 传输 / 非法 JSON，或响应 `ok=false` |
| 2 | `state=failed` 或 `cancelled` |
| 3 | 墙钟超时（服务端仍可能在跑） |
| 4 | 需要人拍板（`need_human`），立即返回 |

exit 4 在原 status 上追加字段（`ok` 保持服务端原值）：

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
派一单：open → 记下 request_id → wait（或轮询 status）→ report。
环境变量 COLLAB_API_BASE / COLLAB_API_TOKEN 已在 shell 中时不要回显 token。
```

PowerShell 等价（无 Hermes 时人工验）：

```powershell
cd C:\Users\Admin\src\teleagent-collab
python bin/hermes-collab-request.py --help
$opened = python bin/hermes-collab-request.py open --goal 'hello' --artifact delivery.md | ConvertFrom-Json
python bin/hermes-collab-request.py wait $opened.request_id --timeout 120
python bin/hermes-collab-request.py report $opened.request_id
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
