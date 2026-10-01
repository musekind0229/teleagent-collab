---
name: teleagent-collab
description: "Delegate work to local collab-service workers (agy/antigravity pool) via bin/hermes-collab-request.py: open, wait, report. Use when the user asks to have 'the worker'/'collab'/'agy' do a task or produce a file."
version: 0.2.16
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
   python bin/hermes-collab-request.py ping
   ```
   `code=transport_error` → 服务没起，停下告诉用户“collab-service 没在跑”，不要自己启动。
   成功时 stdout 有 `ok`、`api_version`、`base` 和 `capabilities`（服务实现了 `GET /v1/capabilities` 时是能力快照；路由 404 时该字段为 `null`）。派敏感活之前先读 `capabilities`，见「后端能力边界」。不要用 `status __ping__`。
2. 开单：目标写清楚要什么产物；`--artifact` 写工作区内相对路径（可重复）。需要某项能力时加可重复的 `--require-capability NAME`。只有用户明确接受「外部输入只是提示词」时才加 `--ack-prompt-only-inputs`。带 `--external-input` 的单，提交响应和 status 的 `warnings` 都会有 `pinned external inputs are prompt-only…`，即使没开 skip-permissions 也有；照原样告诉用户。
   ```powershell
   python bin/hermes-collab-request.py open --goal "在工作区写 hello.txt，内容为 hello" --artifact hello.txt --title "hello"
   ```
   记下返回 JSON 里的 `request_id`（也叫 goal id）。工人若要读工作区以外的文件，开单加可重复的 `--external-input PATH`（最多 8 个；解析为绝对路径并附上 SHA-256）；超过 8 个客户端直接退出码 1，`code=too_many_external_inputs`，不会发 HTTP。未钉住的外部路径会被静默拒绝，到不了 permission 决策。
3. 等待：用观察窗口切片，不要一次等到宿主工具上限（约 420 秒）。
   ```powershell
   python bin/hermes-collab-request.py wait <request_id> --timeout 300 --interval 5
   ```
   `--timeout` 是**客户端观察窗口**，不是任务的墙钟预算。到点退出码 `3`，`wait.kind=observation_timeout`，非终态时 `wait.task_still_running=true`，`wait.resume` 是下一条同一 `request_id` 的 wait 命令。这**不是**任务失败，禁止因此 re-open 或 retry。用同一个 `request_id` 重复 `wait --timeout 300`，直到终态或 need_human。
   退出码：`0` = completed（`wait.kind=completed`）；`2` = failed / cancelled（`wait.kind=task_failed` 或 `task_cancelled`；若失败文本是目标自己的墙钟预算，`wait.task_timeout=true`）；`3` = 观察窗口到点（见上）；`1` = HTTP/传输错误；`4` = 需要人拍板（`wait.kind=need_human`，立即返回）。
   换了一个 Hermes 回合或进程重启后，用 `pending` 找回还在跑的单（`request_id`、`state`、`awaiting_decision`、`updated_at`）：
   ```powershell
   python bin/hermes-collab-request.py pending
   ```
   TUI 没有推送通知。把 `request_id` 告诉用户，并说明他们可以过一会儿再来问结果。
4. 取报告：
   ```powershell
   python bin/hermes-collab-request.py report <request_id>
   ```
5. 向用户汇报：`request_id`、最终 `state`、产物列表（report 里的 artifacts / 工作区路径）、必要时产物内容摘要。不要编造没看到的内容。SUMMARY 里的 `warnings` 和每条任务的 `review` 要照实说：`review.status=unsupported` 不是审查通过。`status` / `report` / `wait` 默认是简明 SUMMARY；只有用户明确要原始 JSON 时才加 `--full`（或 `COLLAB_OUTPUT_FULL=1`）。

## 很多文件、活动 SQLite、增长中的 JSONL

- 很多文件用 `--input-manifest FILE`。清单里写绝对 `root`、相对 `include`、`max_files`、`max_total_bytes`。默认**不**递归：模式里的 `**` 会被拒绝。只有清单显式 `"recursive": true` 才向下展开，并且仍然受文件数和总字节上限（最多 256 个、256 MiB）。列出目录不等于授权递归读取。
- 正在写入的 SQLite 用 `--sqlite-snapshot DB`（可重复，和 `--external-input` 合计算进最多 8 个钉）。客户端用 backup API 做一致快照，只钉快照文件。不要把 db 和 `-wal` / `-shm` 一起钉成 `--external-input`。
- 还在追加的 JSONL 用 `--file-snapshot FILE`。只复制打开时量到的前缀，并丢掉末尾不完整的一行。
- 任务失败里的 `hash_changed: <相对路径>` 表示开单哈希之后、派工复制之前源变了。把原文告诉用户，不要扩大读取范围再重试。哈希不是权限隔离：工人只该看到工作区里 `inputs/manifest/` 下的副本，以及明确钉住的快照文件。

## 后端能力边界

敏感工作之前读 `ping` 的 `capabilities`，不要凭感觉。

- `channels.permission` 为 false，或 `skip_permissions` 为 true，或 `external_inputs.enforcement` 为 `prompt_only`：这个后端**没有** permission gate。「没有出现 permission 提示」**不**表示访问是安全的。
- `isolation.prompt_constraints` / 合同里的 must、must_not **不是**操作系统沙箱。`isolation.os_sandbox` 当前为 false。
- 任务会碰到私人或敏感数据时停下，把上述字段告诉用户。只有用户/操作者明确接受降级时才继续，开单加 `--ack-prompt-only-inputs`。不要自己设 `AGY_AUTO_APPROVE`。
- `--require-capability NAME` 可重复（例如 `permission_gate`、`no_skip_permissions`）。服务不满足则退出码 1，`code=capability_unavailable`，stdout 有 `missing`。不要改口重试把要求拿掉，除非用户同意。
- 任务 `review.status` 为 `unsupported` 时，**不要**说审查已通过。验收文本没有被独立核对；文件在不在是另一件事。`passed` 才是真有检查并且过了，`failed` 是没过。

## 进度与预算

- 要看慢但还活着、在等决定、还是没心跳：`python bin/hermes-collab-request.py progress <request_id>`。它只给 `state`、`phase`、心跳/进度年龄（秒）和最近几条事件。没有进度就是 `unknown`。**不要编百分比**。
- `phase=planning` 表示组长还在出计划，没有工人在跑。慢组长不会卡住别的单。
- `phase` 还可能是 `preparing`（拷输入、起进程）、`executing`、`delivering`/`finalizing`（工人已退出，在收结果）、`testing`（核产物/精确内容）、`reviewing`（等审查）。`phase_age_sec` 是这一段已经多久。status 的 `phase_timeline`（摘要里是 `phases`）是每段的起止时间和结果，照原样转述，不要说成百分比。
- `state=stale` 表示**很久没有真实活动**（agy 没有新输出、工作区和它的会话文件都没变），不是进程没了。告诉用户；不要自动取消或重开。
- 一次性后端 `progress.available` 为 false。agy 的活动心跳（输出、工作区、会话文件的 mtime）和产物清单（文件名、大小、mtime，没有正文）是真的。`subagent_observability` 只有 `false` 或 `unknown`，不会假装是 0。
- 预算旗标：`--wall-sec`、`--max-tokens`、`--max-tool-calls`、`--no-progress-sec`、`--on-no-progress checkpoint|fail`、`--budget-report-only`。这个后端标成 unsupported 的字段会被拒绝（退出码 1，`code=capability_unavailable`，`missing` 里有 `budget:<字段>`），除非用户明确要求 `--budget-report-only`。不要为了过提交自己拿掉限制。
- `post_hoc` 表示跑完才核对，中途停不了。用量是工人自报，**不是账单**，不要把它说成费用。
- 检查点决策（`kind=checkpoint`，摘要像 `checkpoint: no progress for Ns` 或 `budget exceeded: continue or stop`）就是停下来问用户。不要自动 `continue`，不要静默加预算或再 open 一次。只有用户明确说出 verdict 时才 `decide`。`continue` 算一次 rework，计入 `max_reworks`。`stop` 留下已有文件。

## planner 能力与分阶段交付

- 服务默认的 `deterministic` planner 只生成 **一个** task，不拆解；验收默认是产物文件存在。
- Same-goal independent tasks may run in parallel up to `capabilities.concurrency.effective`; supervised TeleAgent desktops stay serial.
- `--planner lead` / `--planner grok` 由 **collab-service 启动参数** 决定：组长会拆解并审查。客户端不能选择 planner。
- 复杂工作拆成多张单，按阶段推进：源覆盖核对 → 一份可审查的样例 → 实现 + 独立测试 → dry-run → 操作者批准后再安装。
- 约束写在 `--must` / `--must-not` / `--acceptance-text`，不要把长约束塞进 `--goal`。
- `state=completed` 之后读真实产物；completed 不等于业务验收通过。status 里的 `acceptance_status` 把 `execution`、`artifacts`、`independent_checks`、`technical_review`、`business_acceptance`（服务从不做，恒为 `not_performed`）、`deployed` 分开写；照原样转述，不要合并成「已验收」。
- agy 后端没有组长审查通道：`--require-capability lead_review` 在 agy 上会被拒（409），即使 planner 是组长。
- `acceptance` 里只认 `artifacts`、`text`、`allow_aigc_marks`；其他键会被 400 拒绝，不会被悄悄丢掉。
- 后续修订是一张 **新** request，并在 goal 里写上上一张 `request_id`。失败的单不要静默重新 open。

## 停下问用户（不要自己重试）

出现以下任一情况，**立即停止**，把 `request_id`、`state`、`failure` / `failure_reason` / `need_human` 原因原样（去掉任何密钥）告诉用户，等用户决定：

- `state=failed` 或 `cancelled`；转述 `primary_failure` 的 `stage`（`planning` / `worker` / `budget` 等）、`source`、`lead_status`（组长 timeout / call_failed / error）。`candidate_available: true` 只表示文件还在（`candidate_artifacts`），**不是**成功也没被审查；不要因此把失败说成完成，也不要自动重派。转述 `failed_phase` 和 `outcome`：`finalizing` + `candidate_produced` 是「有候选文件，CLI 收尾失败」，`no_output` 是「什么都没产出」，两者不要混说。`acceptance_status.artifacts` 是 `candidates_only` / `incomplete` / `none` / `not_started` 时照原样说，并列出 `candidate_artifacts` 与 `missing_artifacts`。
- 原因里有 `belonged to a previous service process` 表示服务重启过、旧工人已被停掉，结果没收回来；文件只是候选。告诉用户，由用户决定是否新开一单。
- 决策 `summary` 以 `CONTAMINATED` 开头，或失败结果的 `error` 以 `artifact_contaminated` 开头：这是失败/不安全的产物（AIGC 水印或不可见字符）。把这段原文告诉用户，不要批准。
- 任何层级出现 `need_human: true`（顶层、`failure`、`tasks[].result`），或 `error` 以 `need_human:` 开头；
- `pending_decisions` 非空（服务在等人拍板）；
- `wait` 退出码 `4`：已经停在等人拍板。把 `request_id`、`wait.reason`（`pending_decisions` / `awaiting_decision` / `task_awaiting_decision`）、`decision_ids`、每条 `decisions[].summary` 转述给用户。禁止自己批准或拒绝决策（不要代答 `POST …/decisions/…`）；
  TeleAgent 原生决策的 summary 来自 worker payload（review 的 artifacts/tools、permission 的 pattern 与 scope、question 题面）。
- `pending_decisions` 里 `awaiting: "lead"` 的行正在由组长裁决，不要就这些行问用户；`wait` 会继续轮询。退出码 `4` 现在表示确实需要人拍板。
- 转述 permission 决策的 summary 时带上 scope（目录里实际有哪些文件）；如果 summary 里有 `NOT ONLY PINNED`，告诉用户该目录不只有钉住的输入文件。
- 401/403/transport_error。401/403 只转告 `auth.token_source`，不要回显 token。

`wait` 退出码 3（`observation_timeout`）不在上面这份「停下」清单里：它不是任务失败。按 `wait.resume` 用同一 `request_id` 再 wait，并把 `request_id` 告诉用户（TUI 没有推送，他们可以过一会儿再来问）。不要因此 re-open 或 retry。

禁止：自行 `POST /v1/requests/{id}/retry`、因为观察窗口到期而重新 open 同一目标、把失败的单静默再派一次、换号、改账号池、重启服务、设置 `AGY_AUTO_APPROVE`、自己批准或拒绝决策。

`decide <request_id> <decision_id> --verdict <原话>` 只给人类提交者/操作者用；Hermes 仍禁止自行调用。仅当用户明确说出要提交的 verdict 时，才可按该原话执行 `decide`；若被拒绝（`code` 为 `artifact_contaminated` 或 `worker_decision_rejected`），把 `code` 和 `error` 原样转告。

## Pitfalls

- `--backend` 只是调用方标注，**不会**切换 worker；worker 后端由 collab-service 启动参数决定。能力以 `ping` 的 `capabilities` 为准。
- 没有弹出 permission 提示，不代表访问安全。先看 `channels.permission`、`skip_permissions`、`external_inputs.enforcement`。
- `wait` 的 `--timeout` 是观察窗口，到点（退出码 3）不代表失败：服务端可能仍在跑。按 `wait.resume` 对同一 `request_id` 再 wait，或重启后用 `pending` 找回；不要因此开新单。把 `request_id` 告诉用户。
- 中文 goal id 已由脚本做 percent-encoding，直接传原值即可。
- PowerShell 下 JSON 可用 `| ConvertFrom-Json` 取字段。
- 输出默认 ASCII 转义（中文为 `\uXXXX`）。读中文字段用 `ConvertFrom-Json` / `json.loads` 解析，不要对原始 JSON 文本匹配中文。不要自己加 `--unicode`，除非确认终端是 UTF-8。

## Verification

- `wait` 返回 `state=completed` 且 `report` 中列出预期 artifact；
- 需要时在 report 给出的工作区里读产物内容确认。核对产物用文件读取工具（read_file）读文件，不要用 `python -c`（Hermes 单次查询模式会拦截 `python -c`）。
- 默认只看 SUMMARY。用户要原始载荷时再加 `--full`。
