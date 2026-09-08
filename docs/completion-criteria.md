# 完成判定与验收（条2）

## 原则

1. **全部产物齐全**才算候选成功；「存在任意产物」不算。
2. **缺失 / 错误 / 超时 / 取消**一律非成功（含墙钟 timeout 时磁盘上已有部分或全部产物）。
3. `force_lead_review` 在**串行**（`glue.run_job`）与**并行**（`scheduler.refresh_job_status`）中都生效：未获 lead `verdict=pass` 不得标 DONE。
4. 提交验收时必须带齐：执行结果、错误、必要工具/审批记录、产物清单（含 hash/mtime）。
5. 组长批准前 **再确认产物未变**（`confirm_artifacts_for_lead_approve`）。
6. **返工次数 + 总墙钟**受 `ReworkBudget` 约束；返工/重启**不得**延长或重置墙钟。

## 模块

| 符号 | 文件 | 作用 |
| --- | --- | --- |
| `snapshot_artifacts` / `artifacts_all_present` | `src/completion.py` | 清单与「全部存在」判定 |
| `ReworkBudget` | 同上 | `max_reworks` + 固定 `wall_deadline` |
| `build_acceptance_packet` | 同上 | 验收提交包 |
| `confirm_artifacts_for_lead_approve` | 同上 | 批准前指纹门闩 |
| `is_success_allowed` | 同上 | 统一成功门 |

## 已删除的宽松分支

- `glue`: `or True`、early-accept（hard_rule + 任意产物）、timeout+产物仍 `ok`
- `scheduler`: timeout 时有产物 → DONE
- `cut2`: `(fin == "stop" or arts)`
- `cut3`: redo 时 `deadline = max(...)` 延长墙钟；fuse 上 lead pass 但产物不齐仍成功

## 测例

```bash
cd /workspace/teleagent-collab/src
python3 -m test_p1_completion_lead   # 全部 simulated
python3 -m test_scheduler
python3 -m test_p0_security
```

真机：需 TeleAgent `:4399` + lead 适配器；本仓库 P1 验收以模拟测例为准。

## Astra P1 完成判定加固

- `/session/status` 非 2xx / 无效 body（含纯 error 对象）**不得**当 idle。
- 看本轮 assistant `finish`/`error`；无 `force_lead_review` 时文件存在也不足以 DONE（需 `finish` in stop/complete）。
- 验收包使用真实 execution_result / error / 工具证据；生产路径禁止替组长补 `application_id`（仅 dry_run 可 stitch）。
- Linux adapter：默认仅 loopback；`urlopen` 禁用环境代理，拒绝非 loopback 重定向。
