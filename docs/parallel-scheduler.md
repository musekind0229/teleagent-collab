# 并行调度 + 审批抽检（扫描 ≠ 叫组长）

永续层可以一次塞多张章程；调度器按 `max_parallel`（默认 **3**）开工，每单独立 `job_id` + 独立工作目录，审批环用**自适应轮询**，并且严格区分：

| 动作 | 做什么 | 何时 `call_lead` |
| --- | --- | --- |
| **扫描 (scan)** | `GET /permission`（或 dry 模拟栈），按 `sessionID`/`job_id` 归到各槽 | **永不**。无 pending → 记 `empty_scans++` 直接跳过 |
| **硬规则 / 白名单** | `hard_rule_decision`；命中 reject 或 `_hard_rule_allowlisted` → 秒回 API | **不**叫 lead |
| **普通工作区 R/W** | `should_ping_lead == False` 且路径在本 job workdir（canonicalize/`is_path_within`） | **仍叫 lead**（已删除无条件 once） |
| **真正需要 lead** | 硬规则未处理 + 触发表命中 + `PingDeduper` 允许 | 才 `call_lead`（事件触发；禁每步思考推组长） |

**禁止** yolo / always-approve；秘密类即使 lead 说 `always` 也会被压成 once/reject。

## 轮询节奏

| 场景 | 间隔 | 行为 |
| --- | --- | --- |
| 多路空闲（无 busy、近 5s 无 pending） | **10～30s**（`COLLAB_IDLE_SCAN_*`） | 扫一轮；若收齐多单 pending，可一批调度，但仍 **按 job_id 分别决策**（多次短 `call_lead`，不串上下文） |
| 单路 busy / 刚弹出 pending | **1～3s**（`COLLAB_BUSY_POLL_*`） | **串行弹权**：出一条批一条；硬规则/白名单秒回；**禁止**等攒齐再回 |

实现：`choose_poll_interval(...)` + `dispatch_pending_batch`（每 job 每 tick 最多处理 1 条 pending）。

## 隔离

- 工作目录：`jobs/workspaces/<job_id>/`（可配 `--workspaces-dir`）
- 收件：`jobs/runs/<job_id>/{status.json,report.json}`
- 每单独立 `PingDeduper`、独立 `handled_perm_ids`，避免串文件 / 串审批上下文
- `glue.run_job(..., workspace=...)` 亦可单跑隔离

## 入口

```bash
# 内置烟测：3 路并行隔离 + 串行短轮询（不调 TeleAgent）
python3 bin/run-scheduler.py --smoke

# 多章程 dry（仍写独立 workdir / 报告）
python3 bin/run-scheduler.py --dry-run --max-parallel 3 \
  jobs/examples/hello.charter.yaml \
  jobs/examples/hello.charter.yaml

# 真跑
export COLLAB_LEAD_BIN=/workspace/run-grok.sh
python3 bin/run-scheduler.py --max-parallel 3 jobs/examples/hello.charter.yaml
```

单跑章程入口仍兼容：`bin/run-job.py`（可选 `--workspace`）。

## 怎么验

```bash
python3 bin/run-scheduler.py --smoke
# 期望: parallel_isolation_ok=true, serial_short_poll_ok=true
# stats.lead_calls：隔离烟测 ≥1（普通 R/W 也走 lead）；串行烟测应为 2（两条灰色，分两次短 call_lead）
# busy_interval_sample ∈ [1, 3]

python3 src/scheduler.py          # 同烟测直接跑
python3 src/test_scheduler.py     # unittest 包装
```

## 已知限制

- 真跑依赖 TeleAgent `:4399` 与 `COLLAB_LEAD_BIN`；dry/smoke 不连网。
- 全局 `GET /permission` 按 `sessionID` 过滤；若上游不带 session 字段，该条会被跳过（记 notes）。
- 多路「一批调度」= 同一 tick 内对各 job **各处理一条**；不是把多 job 上下文拼进一次 lead prompt。
- 验收环 `force_lead_review` 的完整 redo 路径仍以单 job `glue.run_job` 为主；调度器真跑侧重开工 + 审批扫描 + 产物齐即收。
- `jobs/workspaces/` 默认 gitignore，勿把沙箱产物推进 Git。

## 代码

- `src/scheduler.py` — `ParallelScheduler`、烟测 `smoke_parallel_isolation` / `smoke_serial_short_poll`
- `bin/run-scheduler.py` — 永续入口
- 复用：`hard_rules.py`、`decision_packet.py`（`PingDeduper` 按 request id）、`teleagent_adapter`、`glue.call_lead`（live）
