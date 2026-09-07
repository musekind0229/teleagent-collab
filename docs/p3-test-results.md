# P3 测试结果表

日期：2026-09-07（Asia/Singapore）

## 环境

| 项 | 值 |
| --- | --- |
| OS | Linux 6.12.94+（box） |
| TeleAgent 应用 | 2.5.0（`/opt/TeleAgent`） |
| SAC HTTP | `http://127.0.0.1:4399` |
| SAC `/version` | 1.2.27 |
| Lead（真机） | `GrokCliLeadAdapter`（`/workspace/run-grok.sh` → `~/.grok/bin/grok`） |
| Lead（模拟） | stubs / inprocess / fake transport |
| Question API | `GET /question` 存在；reply `{"answers":[][]string}`；reject `POST .../reject` |

## 结果

| 用例 | 模式 | 结果 | 说明 |
| --- | --- | --- | --- |
| Claude Code stub 不假 PASS | 模拟 | PASS | `call_failed`；`validate` → LeadDecisionError |
| Codex CLI stub 不假 PASS | 模拟 | PASS | 同上；`codex` 别名仍走 inprocess |
| Grok factory | 模拟 | PASS | |
| Question probe + session 绑定 | 模拟 | PASS | 错 session 拒答 |
| Question need_human 默认 | 模拟 | PASS | 无 auto/lead 不发明答案 |
| Question 404 gap | 模拟 | PASS | doctor/docs 可标缺口 |
| 受控假包装填 + 授权门控 | 模拟 | PASS | 仅 workdir 内 `install_roots` |
| 危险真装 apt/sudo 跳过 | 模拟 | PASS | `status=blocked` |
| user_gate package_manager | 模拟 | PASS | |
| controlled-fake-install 章程 load | 模拟 | PASS | |
| Win 仍 blocked（含 question） | 模拟 | PASS | |
| adapter contract + reply_question | 模拟 | PASS | |
| P0/P1/P2/scheduler 回归 | 模拟 | PASS | |
| 真机 Question `GET /question` | **实机** | PASS | 空列表；probe available |
| 创建→权限→**Grok CLI** 批→继续→验收 | **实机** | PASS | 见下方；本跑 worker 未弹权（approved=0），Grok `force_lead_review` verdict=pass |

## Live Grok 行

| 项 | 值 |
| --- | --- |
| 命令 | `python3 bin/run-live-grok-lead.py --timeout 180` |
| 时间 | 2026-09-07 ~17:26 SGT |
| session_id | `ses_f84cf4371ffeJC9QYAaoS752Lm` |
| 结果 | **PASS**（ok=true） |
| lead_bin | `/workspace/run-grok.sh` |
| doctor | ok；question_api available |
| 权限批 | 本跑无 pending（worker 直接写完）；脚本路径已覆盖 lead_perm |
| 验收 | Grok review `verdict=pass`（GROK_LIVE_OK） |
| 失败原因 | — |
| 备注 | 登录/配额正常；未关鉴权 |

## 剩余阻塞

- Claude Code / Codex CLI **真机** lead：骨架 stub 已就位，标 **待办**；禁止假 PASS。
- Question：list/reply/reject + session 绑定已接入；人机问答默认 `need_human`（完整 lead/人编排仍可加深）。
- 真实系统包装填（apt/RustDesk 等）：**明确 blocked**；仅假包/可回滚产物。
- Windows TeleAgent 2.4.1：**仍 blocked**。
- **不做 IPv6 服务器部署**；不关鉴权。

## 命令

```bash
cd src
python3 -m test_p3_lead_question_install
python3 -m teleagent_adapter.test_adapter_contract
python3 -m test_p2_auth_recovery
python3 -m test_p1_completion_lead
python3 -m test_p0_security
python3 -m test_scheduler
# 真机 Grok lead（需 :4399 + grok 登录）
python3 ../bin/run-live-grok-lead.py --timeout 180
```
