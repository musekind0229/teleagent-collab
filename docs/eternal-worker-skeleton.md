# 永续 ↔ 工人：最小机械骨架（刀2）

永续层（eternal / 编排）与 TeleAgent 工人之间用 **任务包（章程 charter）** 交接，不用长提示词口头指挥。

## 永续层只做三件事

1. **写包**：落盘章程文件（YAML/JSON），字段见下。
2. **开跑**：调用入口 `bin/run-job.py <charter>`（内部读包 → `glue.run_job`）。
3. **收件**：到 `jobs/runs/<name>-<utc>/` 取 `status.json`、`report.json`、`report.md`。

**禁止**只靠长提示词口头指挥工人（例如在 chat 里贴一大段自然语言当唯一契约）。章程才是 must / must_not / 白名单 / 完成标准的机械源；入口脚本从章程拼 instruction，并把同一 charter 传给 `run_job(charter=…)` 供硬规则与决策包使用。

## 章程字段（最少）

| 字段 | 说明 |
| --- | --- |
| `goal` | 人要什么 |
| `must` | 必须遵守 |
| `must_not` | 禁止 |
| `allow_secret_globs` / `allow_paths` / `allow_keys` | 秘密路径白名单（至少一个字段出现；无授权用 `[]`） |
| `done_when` 和/或 `acceptance` | 完成标准（产物路径或验收文案） |

可选：`name`、`instruction`（补充细节，不能替代上表）、`timeout_sec`、`force_lead_review`、`allowed_surfaces`。

样例：[`jobs/examples/hello.charter.yaml`](../jobs/examples/hello.charter.yaml)。

## 怎么跑

```bash
# 骨架自检（不调 TeleAgent）
python3 bin/run-job.py --dry-run jobs/examples/hello.charter.yaml

# 真跑（需 :4399 已登录 + COLLAB_LEAD_BIN）
export COLLAB_LEAD_BIN=/workspace/run-grok.sh   # 或其它组长
export COLLAB_LEAD_NAME=grok
python3 bin/run-job.py jobs/examples/hello.charter.yaml
```

成功时 stdout 打印 `out_dir` / `status` / `report_*` 路径；退出码 0=ok，1=fail，2=章程非法。

## 与硬规则的关系

本刀**不改** `hard_rules.py` 逻辑。章程里的 `allow_*` 原样交给现有 `glue.run_job(..., charter=…)`；刀3另做硬规则演进。

## 并行调度（可选）

多章程并行时用 `bin/run-scheduler.py`（`max_parallel`、独立 workdir、扫描 ≠ call_lead）。见 [`parallel-scheduler.md`](parallel-scheduler.md)。

