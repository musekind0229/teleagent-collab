---
name: teleagent-collab
description: "Delegate work to local collab-service workers (agy/antigravity pool) via bin/hermes-collab-request.py: open, wait, report. Use when the user asks to have 'the worker'/'collab'/'agy' do a task or produce a file."
version: 0.2.8
author: teleagent-collab
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [collab, delegation, workers, antigravity, agy, teleagent]
    category: autonomous-ai-agents
    requires_toolsets: [terminal]
---

# teleagent-collab：把活派给 collab worker

collab-service 是本机常驻的派工服务（Application API，默认 `http://127.0.0.1:8765`）。它负责拆解目标、挑账号、拉起 worker（antigravity / teleagent-windows，由**服务启动参数**决定），并验收产物。你（Hermes）只是**薄客户端**：开单 → 等待 → 取报告 → 转述结果。

## When to Use

交给 collab（而不是自己动手）：

- 用户说“让 worker / collab / agy / 那边的工人 做 X”“派个活”“派一单”。
- 需要在 worker 工作区产出文件（如 `hello.txt`、`delivery.md`）并由服务验收的任务。
- 用户要的是“有人干完并交付产物”，而不是你本地直接写。

不要交给 collab：纯问答、只读查询、改 Hermes 自身配置、用户明确要你本地直接做的事。

## Prerequisites（前提，不满足就停下告诉用户）

1. collab-service **已经在跑**（由用户/运维启动，例如
   `python bin/collab-service.py --persist <dir> --port 8765 --backend antigravity --agy-account-pool jobs/agy-account-pool.json`）。
   你**不要**自己启动、重启或停止 collab-service，不要自己拉 agy 进程。
2. 所有入口（Hermes、CLI、别的 agent）共用同一个账号池 `jobs/agy-account-pool.json`，由服务按单挑号。**不要**读、改、打印这个文件。
3. 仓库路径：环境变量 `COLLAB_REPO`；未设置时 Windows 默认 `C:\Users\Admin\src\teleagent-collab`。

## 连接配置（不要写明文 token）

脚本只读 `COLLAB_API_BASE` 和 `COLLAB_API_TOKEN`，不把 `.env` 里其它变量注入环境。查找顺序：进程环境 > `COLLAB_ENV_FILE` > `HERMES_HOME/.env` > Windows `%LOCALAPPDATA%\hermes\.env` / 其它系统 `~/.hermes/.env`。

脚本自己会读 Hermes `.env`，终端里不需要 `export`。Hermes 进程启动时也会把同一份 `.env` 载入环境。

| 变量 | 来源 | 默认 |
| --- | --- | --- |
| `COLLAB_API_BASE` | 进程环境 > `COLLAB_ENV_FILE` > `HERMES_HOME/.env` > Win `%LOCALAPPDATA%\hermes\.env` / 其它 `~/.hermes/.env` | `http://127.0.0.1:8765` |
| `COLLAB_API_TOKEN` | 同上；仅服务启用 bearer 时需要。脚本自己读 `.env`，不必 `export` | 空 |

- 从不在回复、命令行参数、日志里回显 `COLLAB_API_TOKEN` 的值；不要 `echo` / `Get-ChildItem env:` 打印它。
- 不要把 token 写进 skill、仓库、memory 或 profile。
- 401/403 时把 stdout JSON 的 `auth.token_source`（`env` / `dotenv` / `none`）告诉用户，不要回显 token，也不要提它的长度或前缀。

## Procedure

用 terminal 工具在仓库根目录执行（PowerShell 示例；bash 同理）。脚本每次向 stdout 打**一行 JSON**。

1. 探活（可选但推荐）：
   ```powershell
   cd $(if ($env:COLLAB_REPO) { $env:COLLAB_REPO } else { 'C:\Users\Admin\src\teleagent-collab' })
   python bin/hermes-collab-request.py status __ping__
   ```
   `code=transport_error` → 服务没起，停下告诉用户“collab-service 没在跑”，不要自己启动。
   （404 / not_found 说明服务在线。）
2. 开单：目标写清楚要什么产物；`--artifact` 写工作区内相对路径（可重复）。
   ```powershell
   python bin/hermes-collab-request.py open --goal "在工作区写 hello.txt，内容为 hello" --artifact hello.txt --title "hello"
   ```
   记下返回 JSON 里的 `request_id`（也叫 goal id）。工人若要读工作区以外的文件，开单加可重复的 `--external-input PATH`（解析为绝对路径并附上 SHA-256）；未钉住的外部路径会被静默拒绝，到不了 permission 决策。
3. 等待终态（阻塞轮询；按任务规模设 `--timeout`，hello 类 600 秒足够）：
   ```powershell
   python bin/hermes-collab-request.py wait <request_id> --timeout 600 --interval 5
   ```
   退出码：`0` = completed；`2` = failed / cancelled；`3` = 墙钟超时（服务端仍可能在跑）；`1` = HTTP/传输错误；`4` = 需要人拍板（立即返回，不再等超时）。
   如果终端工具自身有超时，可以改为循环调用 `status <request_id>`（每 5–10 秒），直到 `state` 为 `completed` / `failed` / `cancelled`，或出现待决策（`pending_decisions` 非空 / `awaiting_decision` / 任务 `status=awaiting_decision`）。后几种按下面「停下问用户」处理，不要干等到超时。
4. 取报告：
   ```powershell
   python bin/hermes-collab-request.py report <request_id>
   ```
5. 向用户汇报：`request_id`、最终 `state`、产物列表（report 里的 artifacts / 工作区路径）、必要时产物内容摘要。不要编造没看到的内容。

## 停下问用户（不要自己重试）

出现以下任一情况，**立即停止**，把 `request_id`、`state`、`failure` / `failure_reason` / `need_human` 原因原样（去掉任何密钥）告诉用户，等用户决定：

- `state=failed` 或 `cancelled`；
- 决策 `summary` 以 `CONTAMINATED` 开头，或失败结果的 `error` 以 `artifact_contaminated` 开头：这是失败/不安全的产物（AIGC 水印或不可见字符）。把这段原文告诉用户，不要批准。
- 任何层级出现 `need_human: true`（顶层、`failure`、`tasks[].result`），或 `error` 以 `need_human:` 开头；
- `pending_decisions` 非空（服务在等人拍板）；
- `wait` 退出码 `4`：已经停在等人拍板。把 `request_id`、`wait.reason`（`pending_decisions` / `awaiting_decision` / `task_awaiting_decision`）、`decision_ids`、每条 `decisions[].summary` 转述给用户。禁止自己批准或拒绝决策（不要代答 `POST …/decisions/…`）；
  TeleAgent 原生决策的 summary 来自 worker payload（review 的 artifacts/tools、permission 的 pattern 与 scope、question 题面）。
- 转述 permission 决策的 summary 时带上 scope（目录里实际有哪些文件）；如果 summary 里有 `NOT ONLY PINNED`，告诉用户该目录不只有钉住的输入文件。
- `wait` 退出码 3 超时，或 401/403/transport_error。401/403 只转告 `auth.token_source`，不要回显 token。

禁止：自行 `POST /v1/requests/{id}/retry`、重新 open 同一目标“再试一次”、换号、改账号池、重启服务、设置 `AGY_AUTO_APPROVE`、自己批准或拒绝决策。

`decide <request_id> <decision_id> --verdict <原话>` 只给人类提交者/操作者用；Hermes 仍禁止自行调用。仅当用户明确说出要提交的 verdict 时，才可按该原话执行 `decide`；若被拒绝（`code` 为 `artifact_contaminated` 或 `worker_decision_rejected`），把 `code` 和 `error` 原样转告。

## Pitfalls

- `--backend` 只是调用方标注，**不会**切换 worker；worker 后端由 collab-service 启动参数决定。
- `wait` 超时不代表失败：服务端可能仍在跑，用 `status` 再看，并告诉用户。
- 中文 goal id 已由脚本做 percent-encoding，直接传原值即可。
- PowerShell 下 JSON 可用 `| ConvertFrom-Json` 取字段。
- 输出默认 ASCII 转义（中文为 `\uXXXX`）。读中文字段用 `ConvertFrom-Json` / `json.loads` 解析，不要对原始 JSON 文本匹配中文。不要自己加 `--unicode`，除非确认终端是 UTF-8。

## Verification

- `wait` 返回 `state=completed` 且 `report` 中列出预期 artifact；
- 需要时在 report 给出的工作区里读产物内容确认。
