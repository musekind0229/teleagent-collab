# TeleAgent 适配层（条4）

统一工人侧对 TeleAgent 的操控面，隔离 Linux / Windows 差异。调度器与 `glue` 尽量经本层调用 TA。

## 布局

| 文件 | 作用 |
| --- | --- |
| `src/teleagent_adapter/base.py` | Protocol / ABC：`create_session` / `prompt` / `list_permissions` / `list_questions` / `reply_permission` / `session_status` / `cancel`；以及 creds refresh / reconnect / resume 规则 |
| `src/teleagent_adapter/linux_local_v1.py` | Linux：HTTP Basic + `local-v1` HMAC → `http://127.0.0.1:4399`（可从 glue 抽） |
| `src/teleagent_adapter/windows_blocked.py` | Windows TeleAgent **2.4.1**：无受支持认证入口 → `AdapterStatus.blocked` |
| `src/teleagent_adapter/doctor.py` | 诊断：`not_running` / `version_incompatible` / `missing_creds` / `auth_failed` / `api_incompatible`（Win → blocked） |
| `src/teleagent_adapter/test_adapter_contract.py` | **模拟 (simulated)** 契约单测，不连真机 |

工厂：`get_adapter(platform=...)` — `win*` → `WindowsBlockedAdapter`；否则 `LinuxLocalV1Adapter`。

## 鉴权（Linux only）

- Basic：`OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD`
- HMAC：`X-SA-Sign-Version: local-v1` + Timestamp/Nonce/Signature；密钥 `SUPER_AGENT_LOCAL_SESSION_KEY`
- 凭据来源：GUI/SAC 子进程 `/proc/*/environ`（仅内存；禁止写入 git/报告）
- **禁止**：关鉴权、改安装包、开公网诊断端口、IPv6 部署当 workaround

## 规则摘要

### creds refresh
`AUTH_FAILED` / 401/403 / `local_auth_missing` 或显式 `refresh_creds()` 时重读 environ；禁止每次请求轮询刷新。

### reconnect
连接失败时：固定回 `127.0.0.1:4399`、refresh 一次、原调用最多重试一次。

### resume
已有 `session_id` → `resume` 校验存在后继续 `prompt` / 审批；**禁止**另建重复 session。权限 / 提问 / status / message **严格按 sessionID 过滤**。

### 回复边界
默认 `once`；适配层将 `always` 降为 `once`。工人侧另有 hard-rule / lead 决策。

## Windows 阻塞说明

Win TeleAgent **2.4.1** 没有文档化的工人自动化认证入口（无可用的 Basic + local-v1 等价物）。`WindowsBlockedAdapter` 对所有工人操作抛 `AdapterError(BLOCKED)`。请使用 Linux local-v1。不要用关鉴权 / 刮 GUI token / 开公网端口等方式绕过。

## 测例

```bash
cd /workspace/teleagent-collab/src
python3 -m teleagent_adapter.test_adapter_contract   # 全部 simulated
python3 -m test_scheduler
python3 test_hard_rules.py
```

真机（可选，需 :4399 与 creds）：`doctor(adapter=get_adapter())` — 本仓库 CI/验收以模拟测例为准。

## 与 glue / scheduler

- `glue.get_ta_adapter()` / `glue.call()` → `LinuxLocalV1Adapter.call`
- `scheduler.ta_call` 优先走同一 adapter
- dry/smoke **不**要求 TeleAgent 在跑
