# teleagent-collab

把天翼星辰 **TeleAgent** 当成编排里的便宜工人：用本地 HTTP（`:4399`）下单、等完结、接权限弹窗；**组长/门禁可插拔**（烟测默认 Grok Build），不焊死某一家。

> GitHub About: `Scriptable TeleAgent worker loop: pluggable lead approval, evidence review, and portal-metering via queryID.`

## 解决什么

市面编程 agent 能干活，但不适合人一直坐着点审批。本仓把「投递 →（可选）审批 → 交货 → 验收」收成可脚本化回路，方便接到更大的开发 cluster（永续层发包、执行层施工）里。

## 已验证能力

- **工人**：TeleAgent 本地 HTTP（Basic + HMAC），不靠 GUI 点窗口开工
- **审批环**：`GET /permission`（按 sessionID 过滤）→ 硬规则/组长 → `once|reject`（禁自动 always；批准前 reconfirm）
- **适配层**：`teleagent_adapter`（Linux Basic+HMAC；Win 2.4.1 blocked）
- **验收环**：产物 + 统一 `run-evidence.txt`（命令 + stdout/stderr）；组长 `pass|fail`
- **等待策略**：会话 idle / 产物齐即收；墙钟只做死锁保险丝（避免「货已交仍 timeout」）
- **编制**：`call_lead` + `COLLAB_LEAD_BIN`，组长可换 Claude Code / Codex 等
- **计量**：HTTP 下单需带 `queryID: q_<uuid>`，门户积分按模型档服务端计算（如 **chat-pro**）；缺 `queryID` 时本地有账、门户不计

## 非目标

- 不是又一个编程 IDE / 不是重造 TeleAgent
- 不接陪伴人格记忆（与 memslice 等解耦）
- 不把密钥、session 日志、沙箱产物推进 Git

## 仓库结构

```
bin/run-job.py        # 永续入口：读章程 → glue.run_job → 落报告
bin/run-scheduler.py  # 并行调度 + 自适应审批扫描（scan ≠ lead）
jobs/examples/        # 样例章程（*.charter.yaml）
jobs/runs/            # 收件目录（status/report；gitignore）
docs/                 # 工人契约、适配层、backlog、发现笔记、永续骨架
src/                  # glue / scheduler / hard_rules / decision_packet
src/teleagent_adapter/# Linux local-v1 / Windows blocked / doctor（条4）
templates/            # 报告模板（脱敏）
```

## 快速使用

1. TeleAgent 桌面已登录，本地 `:4399` 在听。
2. 安装并登录可插拔组长（默认 Grok Build CLI）。
3. 设置环境变量后跑胶水：

```bash
export COLLAB_LEAD_BIN=/path/to/grok   # 或其它组长二进制
export COLLAB_LEAD_NAME=grok
# 门户积分可见时用 chat-pro；默认 chat-lite 可能不计门户分
export TELEAGENT_MODEL_ID=chat-pro
export TELEAGENT_PROVIDER_ID=NewApi

cd src
python3 cut3.py   # 或 glue.py / cut2.py（按脚本约定的工作目录）
```

**禁止** `--always-approve` / yolo / 全局 auto-approve。审批必须走 `call_lead`。

每次工人 prompt 会自动带 `queryID: q_<uuid4()>`（见 `src/glue.py`）。


## 喂法 / 触发表 / 硬规则 / 非每步思考

安全敏感活**不要**把工人 CoT 喂给 lead，也**不要**只塞 `tool+path`。在分叉上组 **决策包**（`worker_intent` + `blocker` + `charter_ref` + spine…），详见 [`docs/lead-feed.md`](docs/lead-feed.md)；咨询原文（脱敏）：[`docs/ask-lead-feed-out.txt`](docs/ask-lead-feed-out.txt)。

| 机制 | 说明 |
| --- | --- |
| **硬规则** | `src/hard_rules.py`：默认明显凭据路径 **reject**；`allow_secret_globs`/`allow_paths`（可选 `allow_keys`）显式授权 → 不 reject，glue once+日志；`~/.ssh`/cookie·profile/`gh/hosts`/`.netrc`/`always`+secret **永拒**；灰色问 lead |
| **触发表** | `plan_commit|rewrite`、`surface_switch`、`secret_adjacent`、`blocked_workaround`、`constraint_reinterp`、`permission`、`outbound_auth`、`job_end`（见 lead-feed） |
| **去重** | 同一 `(ping_reason, target_class, path_pattern)` 在 lead 回复前只 ping 一次 |
| **非每步** | 普通源码读写、测例、`ls`、已批计划下连续编辑 → `should_ping_lead` = False |
| **Lead 枚举** | `once|reject|deny_job|demand_safe_path`；后两者映射 API 为 `reject`；禁 always / yolo |

黄金回放：`python3 src/golden_replay_secret_env.py`（无白名单绝不 once/always；有白名单可 once+日志）；笔记 [`docs/golden-replay-secret-env.md`](docs/golden-replay-secret-env.md)。


## 并行调度 + 审批抽检

调度器定时/短轮询**只扫描**各 job 的 pending 栈；无请求就跳过。只有硬规则未处理、真正需要 lead 时才 `call_lead`（`max_parallel=3`，每单独立 `job_id` + workdir）。详见 [`docs/parallel-scheduler.md`](docs/parallel-scheduler.md)。

```bash
python3 bin/run-scheduler.py --smoke          # 3 路隔离 + 串行短轮询烟测
python3 bin/run-scheduler.py --dry-run --max-parallel 3 jobs/examples/hello.charter.yaml
```

## 永续 ↔ 工人（章程骨架）

永续层**只写包、开跑、收件**——用章程文件（`goal` / `must` / `must_not` / `allow_*` / `done_when|acceptance`）交接，**禁止**只靠长提示词口头指挥工人。

```bash
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml
python3 bin/run-job.py jobs/examples/hello.charter.yaml   # 真跑需 :4399 + 组长
```

报告落在 `jobs/runs/<name>-<utc>/{status.json,report.json,report.md}`。详见 [`docs/eternal-worker-skeleton.md`](docs/eternal-worker-skeleton.md)。

## 一句话

**TeleAgent 出力，可插拔组长把门，脚本把审批和验收跑完——给人只留开题和收件。**
