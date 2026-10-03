# 任务：doctor 区分 Win 凭据失败原因 + 文档写清 stdin 化

Repo：`/workspace/teleagent-collab`（musekind0229/teleagent-collab）。改完测绿后 **push origin/main**。

## 背景（DESKTOP-TBB531F 已核实）
- 工人 HTTP `:4398` 面在（401）；doctor → `missing_creds`。
- OpenProcess：QUERY_LIMITED 可开；**VM_READ** 对主进程 / `super-agent-code` 工人 **ACCESS_DENIED**；仅 renderer 可读，且 environ 无密钥。
- 现行 TeleAgent（app.asar）把 `OPENCODE_SERVER_PASSWORD`、`SUPER_AGENT_LOCAL_SESSION_KEY` 等列入 **SECRET_ENV_KEYS**，经 **stdin env payload** 注入 Go 后端，**故意不写 OS environ**（防 `ps -E` / `/proc/*/environ` / PEB）。故 PEB/environ 发现在现行 Win TeleAgent 上已废。
- 硬禁：刮盘 OAuth/`token.json`、Credential Manager、关鉴权、抬 SeDebugPrivilege。海景房禁止。本刀**只**改诊断 extras + 文案 + 文档 + 单测，不跑 live，不改凭据发现主策略为“刮盘”。

## 要做

### 1. `src/teleagent_adapter/windows_process_environ.py`
在 `probe_windows_creds_presence`（或等价诊断路径）里，当本进程缺凭据且 foreign 扫描失败时，区分：

| 诊断码（建议放 extras / presence 字段） | 条件（mock 友好） |
| --- | --- |
| `openprocess_vm_read_denied` | 存在 TeleAgent/SAC 候选进程，且对工人/主进程的 PEB 读因 OpenProcess/ReadProcessMemory 失败（ACCESS_DENIED / WindowsEnvironUnavailable from OpenProcess）；**且**未能从任何可读 environ 块解析到完整凭据 |
| `environ_secrets_stripped` | 至少一个候选进程 **成功**读到 environ 块，但块内**没有** password+session_key（现行 stdin 化典型：renderer 可读但无密钥；或工人 environ 被掏空） |

优先级建议：若两种迹象都有，`creds_blocker`（或同名字段）取更具体者——**优先** `environ_secrets_stripped`（证明 environ 路径已空），否则 `openprocess_vm_read_denied`。都无则保持 `missing`。

要求：
- **永不**把 secret 值写入 extras / logs / 异常消息。
- 可给 `WindowsCredsPresence` 增加可选字段如 `blocker: str | None`、`openprocess_denied_count: int`、`environ_readable_without_secrets: int`（命名清晰即可）。
- `MISSING_CREDS_MESSAGE`（或 doctor details）fail-closed 文案须点明：现行 TeleAgent 可能经 stdin 注入 SECRET_ENV_KEYS、PEB/environ 发现可能无效；禁止关鉴权 / 刮 CM / SeDebug。
- 注入点保持可测：enumerator / environ_reader / 或对 OpenProcess 失败可模拟。

### 2. `src/teleagent_adapter/doctor.py`
Win 路径把上述诊断写入 `report.extras`，例如：
- `creds_blocker`: `openprocess_vm_read_denied` | `environ_secrets_stripped` | `null`/缺省
- 保留现有 `creds_source` / `password_present` / `session_key_present` / `ports`
- `missing_creds` 时 `details` 带简短 fail-closed 说明（无 secret）

### 3. 文档 `docs/windows-teleagent-adapter.md`
写清：
1. 现行 TeleAgent 经 **stdin env payload** 注入 `SECRET_ENV_KEYS`（含 `OPENCODE_SERVER_PASSWORD`、`SUPER_AGENT_LOCAL_SESSION_KEY`），故意不写 OS environ → **PEB/environ 发现在现行 Win 构建上已废**（Linux `/proc` 老路径不可假设仍成立）。
2. doctor extras 新增 `creds_blocker` 两码含义。
3. **禁止**：刮盘、Credential Manager、关鉴权、抬 SeDebug。
4. 端口发现仍 4399→4397→4398；`windows_live_verified` 仍 false。
5. 凭据顺序可保留「本进程 env → 其它进程 environ」作为历史/注入兜底，但须标注：**不能**再当作现行 GUI 启动的可靠来源。

### 4. 单测
- mock：候选存在 + reader 全抛 OpenProcess 失败 → blocker=`openprocess_vm_read_denied`
- mock：可读 environ 但无密钥键 → blocker=`environ_secrets_stripped`
- 本进程有完整凭据 → blocker 空 / source=process_env
- 不写真实 secret；现有测保持绿

### 5. 验收
```bash
cd /workspace/teleagent-collab
PYTHONPATH=src python3 -m unittest \
  teleagent_adapter.test_adapter_contract \
  teleagent_adapter.test_windows_process_environ -q
git push origin main
```
回报：SHA、文件清单、`creds_blocker` 两码一句话。

不要改 Antigravity；不要动海景房；不要实现 SeDebug / CM / 读 token.json。
