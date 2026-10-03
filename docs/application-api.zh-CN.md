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

同一个持久化目录只运行一个服务实例。运行句柄会在首次派工后立刻持久化；服务重启后会继续观察支持持久句柄的后端，无法恢复的后端会把任务明确标成失败，不会静默重派。同一 Goal 里没有依赖关系的 Task 可以并行，上限是 `GET /v1/capabilities` 的 `concurrency.effective`（服务默认每 Goal 2、全局 4，再与后端 `max_runs` 取最小）。监督桌面 TeleAgent 因 `desktop_session_lock` 仍是串行。名额不够时任务继续排队，`scheduler.waiting_reason` 为 `capacity`，不会记成业务失败。当前进程实际具备哪些能力，以 `GET /v1/capabilities` 为准，不要从「没有弹出 permission」推断访问是安全的。

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

启动时输出当前监听地址、规划器、工人后端、持久化目录，以及 `max_parallel_per_goal` / `max_parallel_global`。默认只监听 `127.0.0.1`。启用 wrap 时 stderr 会打一条 diagnostic-only 警告。

并行上限（都是整数，且必须 `>= 1`）：

| 启动参数 | 默认 | 含义 |
| --- | --- | --- |
| `--max-parallel-per-goal` | 2 | 同一个 Goal 里同时处于 in-flight 的 Task 数 |
| `--max-parallel-global` | 4 | 全部 Goal 加在一起的 in-flight 数 |
| `--stale-after` | 120 | 任务仍在跑时，心跳早于这么多秒就算 `progress.state=stale`。未知进度保持 unknown，不会被当成超时 |

有效容量是这二者与后端 `capabilities().concurrency.max_runs` 里**已知数字**的最小值。`max_runs` 为 `unknown` 时不把上限再压低。后端自己声明的原因会进 `limited_by`：in-process 大约 8（`inprocess`）；agy 等于账号池条数，池子不可用或未配置时是 1（`agy_account_pool`）；Windows / Linux 监督桌面是 1（`desktop_session_lock`，同一桌面会话锁，实质串行）。容量不够只是排队，不是失败。

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
| `input_manifest.max_files` | 256 |
| `input_manifest.max_total_bytes` | 268435456（256 MiB） |
| `input_manifest.staging` | `copy_into_workspace`（派工时拷进任务工作区，不是把 root 授给工人） |
| `snapshots.sqlite` | `client_backup_api`（客户端 `Connection.backup()`，服务端不读活动 WAL） |
| `snapshots.file` | `client_prefix_copy`（客户端按打开时的长度做前缀拷贝） |
| `isolation.os_sandbox` | 当前实现都是 false。没有容器 / seccomp / Landlock |
| `isolation.access_audit` | `decision_log`（监督后端的决定日志）、false、或 `unknown` |
| `isolation.prompt_constraints` | true 只表示合同文本里有约束，**不是**操作系统沙箱 |
| `skip_permissions` | bool 或 `unknown`。agy 上等于本后端环境里 `agy_auto_approve_enabled(charter=None)`（`AGY_AUTO_APPROVE` / `COLLAB_AGY_AUTO_APPROVE`） |
| `resume` | bool 或 `unknown`。监督后端为 true（同一 state 目录上 `Engine.tick` 能续跑；TeleAgent 实例变了会 fail-closed）。agy / inprocess 句柄只在本进程内存，为 false |
| `acceptance.artifact_presence` | 文件在不在。当前实现为 true |
| `acceptance.exact_content` | 能否核对「path must contain exactly BODY」。只有 agy 门禁为 true |

多 Task 的 Goal 上，Goal 级「PATH must contain exactly BODY」只门禁 expected_artifacts 里有 PATH 的那个 Task；没有 Task 声明 PATH 时落到汇点 Task（没人依赖的那些），不会静默丢掉。普通文字验收仍然发给每个 Task。
| `acceptance.lead_review` | 本进程规划器会审 **并且** 后端扛得住组长审查时才是 true |
| `acceptance.executable_checks` | 当前为 false |
| `progress.available` | 有真实、便宜的观察才是 true。agy 是**活动心跳**（`heartbeat=runner_activity`，2026-10-02 起）：取 stdout/stderr 增长、工作区文件 mtime、该 run 的 HOME（Windows 为 USERPROFILE）下 `.gemini/antigravity-cli/{conversations,log,brain}` 的 mtime 中最新的一个；只 stat，不读内容。进程还活着**不再**刷新心跳，所以卡住不动的 agy 超过 `--stale-after` 会显示 `state=stale`。会话文件必须能归属到这个 run 才算心跳（2026-10-02 第二版）：① 该 run 的进程树正打开着它（Linux 读 `/proc/<pid>/fd`，归属 `open_handle`），按会话 uuid / 日志文件认领，之后句柄关了也算；② 退一步：run 启动后只新出现了一个会话、本服务没有别的 run 共用这个 HOME、也没有别的进程开着它（归属 `new_file`）。其余一律不用，原因写在 `progress.heartbeat_signals.session`（`{used, attribution, reason}`，客户端 `progress` 里是 `session_signal`），例如 `the only new session file is held open by another process`、`ambiguous: 2 runs of this service share this HOME`。`conversation_summaries.db` 这类共享文件从不算。2026-10-03 起再加两条：③ `log_start_time`：没有 fd 扫描（Windows、macOS）时，`log/cli-YYYYMMDD_HHMMSS.log` 的文件名（agy 本地启动时间）落在本 run 启动 ±2 秒内、且只有这一个，就归给它；有两个以上则写 `ambiguous: N agy logs started within 2s of this run`。`new_file` 只认 run 启动后 30 秒内**创建**的会话（有创建时间就用创建时间，否则用第一次看到的时间），晚出现的写 `no session file created within 30s of this run's start…`。run 结束时拿 agy 报的 `conversation_id` 核对认领，`heartbeat_signals.session.verified` 为 `match` / `mismatch`（mismatch 记 warning）。④ `tool_cpu`（仅 Linux `/proc`）：agy **之下**的工具子进程（不含 agy 本身）两次轮询之间用了至少 50ms CPU，算一次活动，`heartbeat_signals.tool_cpu = {used, processes}`，客户端 `progress` 里是 `tool_cpu_signal`；忙但不出声的编译/测试不会再被判 stale，`sleep` 这种不吃 CPU 的仍会 stale。`recent_events` 里的 `last_activity output|workspace|session|tool_cpu|none` 说明最近一次活动来自哪里。in-process 看自己的状态机（`heartbeat` 为 false，不每拍刷新）。监督后端看作业状态（`heartbeat=engine`）。没有这些信号的一次性后端是 false，并且 **`percent` 永远是 false**，不编百分比 |
| `progress.subagent_observability` | `false` 或 `"unknown"`。不会假装观察到 0 个子代理 |
| `progress.artifact_checkpoint` | 能否给出产物清单（名字、大小、mtime，无正文） |
| `metering.live_usage` / `usage_at_end` / `tool_calls` | 是否有跑中用量、结束时用量、工具调用次数。没有就不要当成有 |
| `metering.fields` / `metering.source` | 会原样转述的字段名。`source` 是 `worker_self_reported` 或 `none`。这些数字不是账单 |
| `budget_enforcement.wall_sec` | 协调器总能执行墙钟，值为 `enforced` |
| `budget_enforcement.max_tokens` | `enforced_live`（跑中能停）、`post_hoc`（只在结束时对账，停不了中途）、`unsupported` |
| `budget_enforcement.max_tool_calls` | 同上三档。agy 没有工具次数，是 `unsupported`。agy 的 token 只在 CLI JSON 结束时出现，是 `post_hoc` |
| `budget_enforcement.no_progress_sec` | `enforced`（用进度里的 `last_progress_at`，没有则用 run 开始时间）或 `unsupported` |
| `usage.source` | agy 为 `worker_self_reported`（CLI JSON 自报）；其它为 `unknown` |
| `warnings` | 人话。agy 恒有 `pinned external inputs are prompt-only: the worker is told which files it may read, but nothing enforces it (no OS sandbox, no access audit)`（与是否 skip-permissions 无关）；skip-permissions 时再加 `backend runs with skip-permissions: no permission gate; pinned external inputs are prompt-only`。带外部输入的 Goal 在提交响应 `warnings` 和 status `warnings` 里出现同一句；不带外部输入的 Goal 不显示 prompt-only 那句 |
| `concurrency.max_parallel_per_goal` | 服务参数，默认 2 |
| `concurrency.max_parallel_global` | 服务参数，默认 4 |
| `concurrency.backend_max_runs` | 后端声明的 `max_runs`（整数或 `unknown`）。与后端文档里的 `concurrency.max_runs` 不是同一个对象：这里是给调用方的合成结果 |
| `concurrency.effective` | 上述已知数字上限的最小值。同一 Goal 的独立 Task 最多并行到这个数 |
| `concurrency.limited_by` | 哪些上限等于 `effective`。后端若声明了 `limited_by`（如 `desktop_session_lock`、`agy_account_pool`、`inprocess`），用那些名字；否则是 `backend_max_runs` |

`unknown` 不等于具备。默认实现（未覆盖的后端）全部是 false / `unknown`，不会假装有门禁。`unknown` 的 `max_runs` 不参与 `effective` 的最小值。

`GET /v1/requests/{id}` 另有派生的 `scheduler`（不落盘）：`running`（本 Goal 的 in-flight 数）、`queued_ready`（依赖已满足、仍在排队的 Task 数）、`capacity`（扣掉其它 Goal 正在占用的名额之后，本 Goal 还能用的上限）、`waiting_reason`。`waiting_reason` 为空表示没有就绪任务，或下一拍可以派工。`global_approval` 是全局决策挡住了派工（某个 Task 自己的 question / permission 不挡其它就绪 Task）。`capacity` 是名额用完，任务继续排队，Goal 保持 `running`，`failure` 不会因此被写成失败。`workdir_claim` 是就绪任务的工作目录都被占用：同一目录（含其它 Goal）同时只跑一个 Task，后来的等，不失败。依赖没完成的不会进 `queued_ready`。失败的依赖不会把下游派出去（下游保持排队）。取消会取消该 Goal 上每一个还在跑的 run。墙钟 `budget.wall_sec` 在多个 run 同时活跃时仍然生效：超时则这些 run 都停，Goal 失败，原因是 wall / budget（投影为 `worker_timeout`），失败对象带上 `elapsed`、`last_progress_at` 和产物清单（名字、大小、mtime）。这不是容量不够。

`GET /v1/requests/{id}` 的 `progress` 是协调器从各 Task 快照派生的，不额外打后端。`available` 为 false 时 `state` 与 `phase` 都是 `unknown`，没有 `percent`。`state` 只取：`executing`、`waiting_decision`、`waiting_capacity`、`stale`、`idle`、`delivering`、`done`、`unknown`。心跳早于 `--stale-after`（默认 120 秒，相等不算）且任务仍在跑，才是 `stale`。没有快照的 running 任务是 `unknown`，不会被改成 `executing` 或 `stale`。`budget_status` 按字段记下 `limit` / `used` / `source` / `exceeded` / `enforced`。用量旁的 `usage_report.note` 是 `not a bill`。

### 预算与检查点

`budget` 缺省仍是 `{wall_sec: 300, max_reworks: 1}`。给出的话必须是对象（`null` 也是 400 `invalid_request`）。已知字段类型不对同样 400：`wall_sec` 非负数字（0 可以），`max_reworks` / `max_tokens` / `max_tool_calls` 非负整数，`no_progress_sec` 必须大于 0，`on_no_progress` 为 `checkpoint`（默认）或 `fail`，`budget_mode` 为 `enforce`（默认）或 `report_only`。布尔值不是数字。不认识的历史键（如 `max_attempts`）会留下，不因此 400。

某字段的 `budget_enforcement` 是 `unsupported` 时，提交直接 409 `capability_unavailable`，`missing` 形如 `budget:max_tokens`，并且**不会**建 Goal、不占幂等键。`budget_mode=report_only` 则接受，并在 Goal 上警告 `budget <field> is report-only on this backend`。`post_hoc` 可以提交，警告里有 `checked after the run; cannot stop mid-run`。

执行时（每一拍，多个 run 一起看）：

- 实时用量超过 `max_tokens`（或工具次数）：取消这些 run，任务失败，`error` 正好是 `budget_exceeded max_tokens`（或 `max_tool_calls`）。不自动加预算、不静默重试。
- 只在结束时才有用量、并且超了：产物留下。Goal 记 `budget_status`（`exceeded: true`，`enforced: post_hoc`），任务结果 `budget_exceeded: true`，并打开检查点决策，摘要 `budget exceeded: continue or stop`。依赖这个任务的下游**不会**开工。
- `no_progress_sec`：`on_no_progress=checkpoint`（默认）干净取消 run，留下工作区和部分产物，决策摘要 `checkpoint: no progress for Ns`，不自动重试。`fail` 则失败，`error` 正好是 `no_progress_timeout`。`report_only` 只记状态，不取消。
- 已经在等决策的任务，这一拍不再因为没进度被杀掉。

检查点的 verdict 只有 `continue` 或 `stop`，不是授权扩张。`continue` 必须是人通过现有 decisions API 明确给出的；它把该任务重新排队，历史记一次 `retry_task`（计入 `max_reworks`），同一条决策再提交不会再记一次。`stop` 让任务失败，不删已经写下的文件。不会偷偷加预算或再跑一圈。

### 多文件清单与一致快照

`POST /v1/requests` 可选 `input_manifest`。这是客户端已经展开并哈希过的**显式文件列表**，不是目录授权。服务端不按 glob 再扫一遍，也不会因为 root 存在就递归读取。

```json
{
  "root": "/绝对目录",
  "entries": [{"relative": "logs/a.jsonl", "sha256": "<64 hex>", "size": 12}],
  "max_files": 256,
  "max_total_bytes": 268435456
}
```

校验（不通过则 **400** `invalid_input_manifest`，不建 Goal）：`root` 必须是绝对路径；`entries[].relative` 走和产物一样的相对路径规则（拒绝 `..`、绝对路径、盘符、凭据式名字）；`sha256` 为 64 位十六进制；`size` 为非负整数；条目数 ≤ `max_files` ≤ 256；字节合计 ≤ `max_total_bytes` ≤ 256 MiB；相对名唯一。未知字段拒绝。

这里的哈希是**哈希当时的内容**。派工时（工人进程启动之前）服务端把每个条目拷进该任务工作区的 `inputs/manifest/<relative>`，拷贝过程中重新流式计算 sha256。对不上、或字节数对不上，任务失败，错误为 `hash_changed: <relative>`，**不**启动工人，也**不**改去读别的路径。链接、越出 root 的解析结果同样在拷贝时拒绝。

工人合同里的 `input_files` 只增加这些工作区内的相对路径（提示词会列出它们）。这不是把 manifest root 加进外部读取权限。哈希不能当成隔离。

活动 SQLite 与还在增长的 JSONL 由**客户端**做一致快照后再钉成 `external_inputs`（仍受最多 8 个的限制），不要把 db 和 `-wal`/`-shm` 当成一对外部输入。快照条目可以带 `metadata`：`kind` 为 `sqlite_snapshot` 或 `file_snapshot`，`source` 只有文件名，`taken_at` 为 UTC `YYYY-MM-DDTHH:MM:SSZ`。工人合同仍只抄 `path` 和 `sha256`。

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

### 失败投影（`state=failed`）

`state=failed` 且**没有**待决 decision 时，顶层多三个字段，把失败 Task 抬上来。`awaiting_decision` / `pending_decisions` 非空不是失败，这三项不出现。客户端观察窗口到点（`wait.kind=observation_timeout`）也不是任务失败，服务端不因此改 Goal 状态。

`need_human` 的语义不变。`failure` 在已经写下时原样返回（need_human 时含 `need_human` / `failure_reason` / `phase=worker` / `task_id`）。普通任务失败的 `failure` 仍常常是 `null`。不要把普通任务失败读成 need_human，也不要靠这些新字段去 `POST …/retry`（那条只给 need_human）。

| 字段 | 含义 |
| --- | --- |
| `failure` | 与今天相同。已设置时原样返回，不改写成下面的短记录 |
| `failure_reason` | 一行摘要，已脱敏。need_human 时仍是原来的短原因；否则取计划顺序里**第一个失败 Task** 的错误。不会把多个 Task 的原因拼成一句 |
| `primary_failure` | 那个失败 Task 的短记录 |
| `failures` | **每一个**失败 Task 的同样短记录，按计划顺序。多 Task 失败时有几条算几条，不合成一个原因 |

`primary_failure` 与 `failures[]` 只有这些键（不含 stdout / stderr，也不复制 Goal 合同）：

| 键 | 含义 |
| --- | --- |
| `task_id` / `run_id` / `title` | 任务标识。派发还没绑上 run 时 `run_id` 为空串 |
| `error` | 脱敏后的一行错误，最多 300 字 |
| `source` | `worker_timeout`（`error` 就是 `timeout`，或墙钟/预算标记，**不是** `budget_exceeded steps`）、`spawn`（`start_run` 抛错，结果带 `error_source: "spawn"`）、`contract_render`、`acceptance`、`contamination`、`cancelled`、`oom`（Linux：worker 以 SIGKILL 结束且本服务 cgroup 的 `memory.events` 里 `oom_kill` 计数增加，`error` 以 `killed by OOM (cgroup memory limit)` 开头；计数是整个服务 cgroup 共享的，并发多个 run 时不能精确到哪一个 run）、`task_failed` |
| `missing_artifacts` | 该次结果里列出的缺失产物名字；没有则 `[]` |
| `retryable` | 布尔。保守，普通失败为 false |
| `next_step` | 短的安全提示，不含密钥和产物正文。`worker_timeout`：`raise budget.wall_sec or split the task; open a NEW request citing this request_id`。`spawn`：`check collab-service --ready` |

任务失败的短记录另有 `failed_phase`（`planning` / `preparing` / `executing` / `finalizing` / `testing` / `reviewing`；墙钟或无进展超时记为 `executing`）、`outcome`（`candidate_produced` / `other_output` / `no_output`）与一行 `outcome_summary`，用来区分「有候选、CLI 收尾失败」（`finalizing` + `candidate_produced`）、「工作区里有别的文件，但没有要的产物」（`other_output`，2026-10-03 起；依赖交接和清单拷进来的输入文件不算）和「什么都没产出」。工人没报错、但验收知道缺哪些文件时，顶层 `failure_reason` 是 `required artifacts missing: delivery.md`，不再是空泛的 `task failed`（#12）。Goal 级规划失败为 `failed_phase=planning`、`outcome=no_output`。

任务失败的短记录另有（2026-10-02 起）：`stage`（任务失败为 `worker`）、`candidate_available` / `candidate_artifacts`（期望产物里哪些作为普通文件仍在受信任务目录里；只做 stat，不读正文，跳过 symlink 和越界路径）、`review_status`。**文件还在不等于执行成功，也没被审查**；run 仍是 failed，不会被改写。

Goal 在任何 Task 结果之前就失败（规划失败、预算/协调失败）时，`failures` 只有一条 Goal 级短记录：`task_id` / `run_id` 为空串（不编造），`stage` 取 `failure.phase`（如 `planning`、`budget`），`source` 为 `planner` 或 `coordination`，可选 `code`（如 `lead_unavailable`、`invalid_plan`、`stale_plan`）与 `lead_status`（组长适配器的 `timeout` / `call_failed` / `error`）。`failure_reason` 同时填上，不再是空串。规划失败时没有任何工人被启动，也不会自动重试。

**顶层 `artifacts`（2026-10-03 起）**：status 原始载荷（`--full`）顶层多一个 `artifacts: [{task_id, path}]`，每个 Task 的产物各列一次，`path` 相对该 Task 工作区、用 `/` 分隔（与客户端摘要同一种写法）；工作区外的绝对路径原样保留。`tasks[].result.artifacts` 不变。

### 验收分层（`acceptance_status`）

每次 status 都带 `acceptance_status`，把几件事分开，不合并成「已验收」：

| 键 | 取值 |
| --- | --- |
| `execution` | `succeeded` / `failed` / `timeout` / `cancelled` / `in_progress` |
| `artifacts` | `complete` / `incomplete`（有产出但还缺）/ `candidates_only`（期望文件都在，但所在 Task 失败，未被接受）/ `none`（什么都没产出）/ `not_started`（还没有 Task 就失败，如规划失败）/ `pending`（还在跑）。另给 `delivered_artifacts`（成功 Task 交付的）、`candidate_artifacts`（失败/取消 Task 留下的文件，只 stat）、`missing_artifacts`（含从没跑到的 Task 的期望产物）、`artifacts_by_task` |
| `independent_checks` | `passed` / `failed` / `not_run`（目前只有 agy「must contain exactly」字面核对算） |
| `technical_review` | `via_lead_gate`（后端把结果交组长审）/ `not_concluded` / `unsupported`（要了散文验收但没人核）/ `not_available`（后端没有组长审查通道，例如 agy） |
| `business_acceptance` | 恒为 `not_performed`：服务从不做业务验收 |
| `deployed` | 恒为 `not_tracked` |

`acceptance` 对象只接受 `artifacts`、`text`、`allow_aigc_marks`；其他键 400，不会在派工前被悄悄丢掉。`required_capabilities` 里的 `lead_review` 需要 planner 能审 **且** 后端把结果交给它（`capabilities.acceptance.lead_review`）；agy 上会 409。

### 阶段时间线（`phase_timeline`，2026-10-02 起）

status 带 `phase_timeline`：Goal 的 `planning` 加每个 Task 的 `preparing`（依赖交接、清单拷贝、合同渲染、选账号、起进程）→ `executing` → `finalizing`（进程已退出，收 CLI JSON、用量、产物）→ `testing`（产物在不在、精确内容核对、污染扫描）→ `reviewing`（TeleAgent / 组长审查 decision）。每段有 `started_at`、`ended_at`、`duration_sec`、`outcome`（`ok` / `failed` / `cancelled` / `checkpoint` / `discarded`，失败带脱敏 `detail`）。`progress.phase` / `progress.state` 在这些协调器阶段进行中时显示 `preparing` / `delivering`（finalizing）/ `testing` / `reviewing`，并带 `phase_started_at`；客户端 `progress` 多一个 `phase_age_sec`，摘要多一个 `phases` 列表。`finalizing` / `testing` 通常只有几毫秒，主要靠时间线事后看。

规划不再占着协调锁：组长规划在单独线程里跑，第一拍最多等 1 秒，慢的就留到之后的拍子取结果，其它 Goal 照常推进。规划结束时 Goal 已被取消或已有 Task，结果丢弃（`outcome=discarded`）。组长计划必须交付 Goal 验收里列出的每个产物，漏了就是 `invalid_plan`（`lead plan does not deliver goal acceptance artifacts: missing b.txt; plan delivers a.txt`），不派工人。比较前两边都规范化：`./a.txt` 与 `a.txt`、`sub\b.txt` 与 `sub/b.txt` 视为同一个。

**取消时停掉组长**：规划中的 Goal 被取消（或在规划中变成 failed、服务收到 SIGTERM/Ctrl-C）时，立刻停掉规划线程正在等的组长进程树，不等组长超时：组长在自己的进程组里启动，POSIX 先 SIGTERM 整个进程组、1.5 秒后 SIGKILL；Windows 用 `taskkill /F /T /PID`。停完在事件（`GET /v1/requests/{id}/events`）里记一条 `planner_stopped`（`reason`、`lead_processes[{pid, method, result}]`、`planner_thread`、`planning_sec`），status 里也有 `planner_stopped`。**服务被 `kill -9` 时的组长（2026-10-03 起）**：每个组长进程（规划、permission / review 决策都算）启动时记进 `<persist>/lead-runs.json`（pid、进程组、启动时间令牌、所属服务进程；不记 argv / prompt），调用结束就删掉。下次启动时把上一个已经死掉的服务留下的组长整组停掉，stderr 打一行 `lead orphan from previous service: run=lead_… pid=N outcome=killed`；pid 已被别的进程复用（令牌不同）时不动它（`pid_reused_left_alone`）。

**决策调用也走进程组（2026-10-03 起）**：permission / review 决策（没有 Goal 取消范围的调用）和规划一样走 `lead_adapter/cancel.py` 的进程组路径，grok、deepseek、codex 三个适配器都是。超时时停掉整个组长进程树，而不是只杀直接子进程；以前组长派生的孙进程会在超时后继续跑。

### 服务重启与 agy 孤儿进程（2026-10-02 起）

agy 后端把每个 worker 的 pid、进程组、启动时间令牌（防 pid 复用）、所属服务进程记在 `<persist>/agy-runs.json`（不记 argv / prompt / 环境）。服务收到 SIGTERM / Ctrl-C（Windows 还有 SIGBREAK）时先停掉还活着的 worker；被 `kill -9` 等方式直接杀掉时，下次启动会把上一个服务留下的 worker 整棵停掉：POSIX 杀 worker 的进程组、会话和后代，外加轮询时记下的子进程（agy 的 shell 工具命令在自己的进程组里，只杀 worker 的组会漏掉它；子进程按启动时间令牌校验后才会动；观测事件里有 `child_processes N`）；Windows 用 `taskkill /F /T /PID`。worker 正常退出但留下还在跑的工具子进程时，收结果时也会把它们停掉。pid 已被别的进程复用时不动它。这些 Task 的失败原因写明是哪种情况，例如 `backend resume failed: BackendError: agy run agy_… (pid N) belonged to a previous service process; its worker process tree was stopped when the service restarted. …`；工作区里已有的文件只是候选。仍然不会自动重派。

**Windows Job Object（2026-10-03 起，未在 Win 实测）**：Windows 上组长和 agy worker 启动后放进一个带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object，句柄由服务进程持有：服务无论怎么死，系统关句柄时会杀掉 Job 里的全部进程。主动停止时先 `TerminateJobObject`，再照旧 `taskkill /F /T /PID` 兜底。任一步失败都静默退回 taskkill。已知缺口：`Popen` 返回到加入 Job 之间派生的子进程不在 Job 里（Popen 不能挂起启动），所以 taskkill 仍然保留。只在 Linux 上用 mock 测过 kernel32 调用顺序。

`start_run` 抛错时，任务结果的 `error` 是 `backend dispatch failed: <异常类型>: <脱敏后的短原因>`（原因最多 300 字）。异常信息为空时仍只写类型名，不留一个空的冒号。

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

### 组长技术评审与角色边界（#15，2026-10-03 现状）

现状：agy 后端**没有**组长技术评审通道。agy 的 worker 结果不会交给组长审，`acceptance_status.technical_review` 恒为 `not_available`，`--require-capability lead_review` 在 agy 上 409。组长（`--planner lead` / `grok`）在 agy 上只做两件事：出计划，以及裁决后端交上来的 permission / review decision。agy 本身不发 permission 请求（`channels.permission=false`），所以实际只剩出计划。

边界：

- 服务只做机械验收：产物在不在、agy「must contain exactly」字面核对、AIGC 污染扫描。这些都不是技术评审，也不是业务验收（`business_acceptance` 恒为 `not_performed`）。
- 技术评审（读代码/产物、跑独立测试、判断是否可合入）由调用方或人做。按「样例 → 实现 + 测试 → dry-run → 操作者批准」分单推进，每一单的结论由人或调用方在下一单里引用。
- 本轮没有加 agy 上的组长评审开关：#15 最新评论要求先暂停功能扩展、先把最短链路验通，老板也还没拍板。需要时会做成默认关闭的开关。

## 查询和控制

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查。另含 `backend` id 与 `planner` name，无密钥 |
| `GET` | `/v1/capabilities` | 本进程能力快照（要登录，与其它 `/v1` 相同） |
| `POST` | `/v1/requests` | 提交高层目标。可选 `required_capabilities`、`acknowledge_prompt_only_inputs` |
| `GET` | `/v1/requests` | 列出请求摘要 |
| `GET` | `/v1/requests/{id}` | 查询 Goal、Task、待决定、失败信息、`warnings`、`capabilities_ref`、`scheduler` |
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
   - `failure_reason`: 短原因摘要（已脱敏）。need_human 时仍是这条原因；非 need_human 的任务失败另见「失败投影」
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

