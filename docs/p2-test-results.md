# P2 测试结果表（条5 + 条6）

日期：2026-09-07（Asia/Singapore）

## 环境

| 项 | 值 |
| --- | --- |
| OS | Linux 6.12.94+（box） |
| TeleAgent 应用 | 2.5.0（`/opt/TeleAgent`） |
| SAC HTTP | `http://127.0.0.1:4399` |
| SAC `/version` | 1.2.27 |
| Lead（真机） | `InProcessLeadAdapter`（decision_fn） |
| Lead（模拟） | fake_lead / dry_run |

## 结果

| 用例 | 模式 | 结果 | 说明 |
| --- | --- | --- | --- |
| task_kind file vs system_install 校验 | 模拟 | PASS | `validate_auth_fields` |
| user_gate 阻止 lead 代批 | 模拟 | PASS | sudo → needs_user |
| install_roots / network_allow 机械检查 | 模拟 | PASS | |
| 隔离能力文档分述 | 模拟 | PASS | mechanical vs prompt |
| 样例章程 load | 模拟 | PASS | hello / file-task / system-install-sample |
| state_store 持久化与不重发决定 | 模拟 | PASS | |
| cancel_requested vs cancelled | 模拟 | PASS | 兄弟任务不受影响 |
| timeout 仅本任务 | 模拟 | PASS | |
| 会话 claim 防劫持 | 模拟 | PASS | |
| 重启不重派 / 不重发决定 | 模拟 | PASS | `restore_from_store` |
| 跨会话误审批过滤 | 模拟 | PASS | session 绑定 |
| 多路径隔离 smoke | 模拟 | PASS | |
| 回写失败不 mark handled | 模拟 | PASS | POST 500 |
| 缺产物不成成功 | 模拟 | PASS | |
| 调度器 user_gate reject | 模拟 | PASS | |
| P0 / P1 / scheduler 回归 | 模拟 | PASS | 全绿 |
| 创建→权限→inprocess 组长批→继续→验收 | **实机** | PASS | `TestLiveTeleAgentPath`：`:4399` 建会话、prompt、权限 once、产出 `live-ok.txt` |

## 剩余阻塞

- 外部 **Grok CLI** 真机 lead 本轮未强制（用 inprocess 跑通审批环）；Grok/Claude/Codex 真机回归集仍在 backlog。
- 未做真实 RustDesk/系统包安装（章程仅为授权样例；**不做 IPv6 服务器部署**）。
- Question API / Windows local / SSE 等仍挂后续 backlog。

## 命令

```bash
cd src
python3 -m test_p2_auth_recovery
python3 -m test_p1_completion_lead
python3 -m test_scheduler
python3 -m test_p0_security
python3 ../bin/run-scheduler.py --smoke
python3 ../bin/run-job.py --dry-run ../jobs/examples/hello.charter.yaml
```
