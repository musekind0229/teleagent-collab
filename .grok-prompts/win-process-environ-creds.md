# 任务：Windows TeleAgent 从本机进程 environ 发现 local-v1 凭据

Repo：`musekind0229/teleagent-collab`（cwd 已是 checkout）。改完测绿，commit 并 **push origin/main**。

## 背景（已核实 DESKTOP-TBB531F）
- TeleAgent 在跑；工人 HTTP **:4397** 返回 **401**（面在，需鉴权）。
- 当前 shell **没有** `OPENCODE_SERVER_PASSWORD` / `SUPER_AGENT_LOCAL_SESSION_KEY`。
- `windows_local_v1.default_find_creds_windows` 只读 **本进程** `os.environ` → 真机必 `missing_creds`。
- Linux 对标：`linux_local_v1.default_find_creds` 扫 `/proc/*/environ`。
- **禁止**：Credential Manager 刮取、关鉴权、开公网端口、把 token/密码写入 git/报告/测例。

## 要实现
1. **Win 进程 environ 读取**（对标 Linux `/proc/*/environ`）：
   - 在 `src/teleagent_adapter/` 增加可测模块（如 `windows_process_environ.py`）：枚举本机相关进程（TeleAgent.exe、SAC/runtime 路径含 `TeleAgent` / `super-agent-code` / `opencode` 等），用 Win32（ctypes：`OpenProcess` + `NtQueryInformationProcess` PEB/`RTL_USER_PROCESS_PARAMETERS.Environment` + `ReadProcessMemory`，或等价安全 API）读目标进程环境块。
   - 解析 NUL 分隔 `KEY=VALUE`，提取：
     - `OPENCODE_SERVER_USERNAME`（可缺省 `super-agent`）
     - `OPENCODE_SERVER_PASSWORD`（及已有 SUPER_AGENT_OPENCODE_* 别名）
     - `SUPER_AGENT_LOCAL_SESSION_KEY`
   - 仅当 password + session key 都在时返回 `(user, pw, key)`。
   - 找不到 → 清晰 `AdapterError(MISSING_CREDS, ...)`，文案说明已扫其它进程 environ、非本进程 env；**不要**建议关鉴权。
   - 非 Windows 平台：函数应 no-op / 明确 raise，不破坏 Linux。
   - 可选：若发现官方落盘位置（只读、无刮 CM），可作 fallback，并在文档写清路径；**优先**进程 environ。

2. **接到 `default_find_creds_windows` / doctor**：
   - 顺序：本进程 env →（失败）扫 TeleAgent/SAC 进程 environ。
   - doctor extras：可报告 `creds_source=process_env|foreign_process_environ|missing`、探测到的端口（4397/4399）、**不要**写入 secret 值；可只报 key **是否存在**（bool）。
   - 端口发现已支持 4399→4397；保持；文档强调真机可能是 **4397**。

3. **测例**（mock，不依赖真机、不写真实 secret）：
   - 注入假 environ 块 / fake enumerator：能解析出 creds。
   - 缺 key → MISSING_CREDS。
   - 本进程有 creds 时仍优先本进程。
   - 现有 `test_adapter_contract` / doctor 测保持绿；新增测文件可。

4. **文档** `docs/windows-teleagent-adapter.md`：
   - 真机端口 **4397**（先探 4399 再 4397）。
   - 凭据发现：本进程 env → 读 TeleAgent/SAC **其它进程** environ（对标 Linux `/proc`）；禁止 CM / 关鉴权。
   - 增加 `windows_live_verified` 小节：本刀代码已具备发现路径；**live doctor/hello 由工坊在 DESKTOP-TBB531F 另验**，未 live 前保持 `windows_live_verified=false`（或 doctor extras 如实）。

5. **不要**动 `teleagent_adapter` 以外的大重构；不要改 Antigravity 账号池；不要提交 oauth/token。

## 验收（本仓）
```bash
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_adapter_contract -v
# + 你新增的 windows environ 测
git push origin main
```
回报：SHA、文件清单、默认 skip-permissions 无关本刀可写 N/A、测例结果。

## 约束
- 测例字符串只用 `sim-pass` / `sim-key` 之类假值。
- 报告/日志/commit message 禁止真实 secret。
