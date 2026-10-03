# 任务：Windows 合法凭据通道 = 受控父进程 stdin wrap

Repo：`/workspace/teleagent-collab`（musekind0229/teleagent-collab）。**复用**现有 `WindowsLocalV1` + Grok lead，**不要**推倒重写适配层。改完测绿 **push origin/main**。

## 已核实（DESKTOP-TBB531F）
- GUI 工人 :4398；`SECRET_ENV_KEYS` 经 **stdin env payload**（`uint32 BE length + JSON`）注入 Go，故意不写 OS environ → PEB 已废。
- **无**官方 named pipe / exportCredentials / registerClient（本地 API 密码）。
- **已探针成功**：受控父进程 spawn  
  `%USERPROFILE%\.local\share\TeleAgent\runtimes\super-agent-code\bin\TeleAgent.exe`  
  stdin payload 含已知 `OPENCODE_SERVER_PASSWORD` + `SUPER_AGENT_LOCAL_SESSION_KEY` + `SERVER_PORT` → `/ready` 200，local-v1 签名后 `/session` `/config` 200。
- 禁：Credential Manager、关鉴权、SeDebug、刮 OAuth token 落盘当 API 密码。海景房禁止。Antigravity 不动。

## 凭据通道优先级（写进 docs）
1. 本进程 env（`OPENCODE_SERVER_*` + `SUPER_AGENT_LOCAL_SESSION_KEY`）
2. （历史）其它进程 PEB/environ — 现行 GUI 下通常失败，保留但标不可靠
3. **stdin_wrap（本刀）**：受控父进程用与 GUI 相同的 stdin payload 拉起内核，父进程内存持有密钥 → doctor/glue 用

## 实现（最小）

### `src/teleagent_adapter/windows_stdin_wrap.py`（新）
- `build_env_payload(env: dict) -> bytes`：与 asar `buildEnvPayload` 一致：`struct.pack(">I", len) + json.dumps(env).encode()`；禁止把 SECRET 键留在子进程 OS environ（OS env 仅 SystemRoot/PATH/TEMP/USERPROFILE/XDG_DATA_HOME 等非密）。
- `resolve_kernel_bin()`：  
  - env `TELEAGENT_KERNEL_BIN`  
  - 默认 `{USERPROFILE}/.local/share/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe`（兼 `LOCALAPPDATA`/`HOME` 变体若存在）  
  - 找不到 → 清晰错误（blocker），不要猜 Program Files GUI exe。
- `ensure_stdin_wrap(*, port: int | None = None) -> WrapHandle`：  
  - 若已有本通道存活（pid 文件 + `/ready`）则复用；否则 spawn。  
  - 端口：`TELEAGENT_WRAP_PORT` 或默认 **4401**（**不要**抢 GUI 的 4398）。  
  - stdin JSON 至少含：`SERVER_PORT`、`OPENCODE_SERVER_USERNAME`（默认 `super-agent`）、`OPENCODE_SERVER_PASSWORD`、`SUPER_AGENT_LOCAL_SESSION_KEY`、`SUPER_AGENT_SERVER_URL=http://127.0.0.1:{port}`；密钥用 `secrets` 生成，**只留在父进程内存**，默认不落盘。  
  - 建议设 `XDG_DATA_HOME` 到 collab 专用目录（如 `{TEMP}/teleagent-collab-wrap/xdg`）减轻与 GUI 数据打架。  
  - 等待 `/ready`（超时 fail-closed）。  
  - 返回：base_url、username、password、session_key、pid、source=`stdin_wrap`（测试可注入 spawn/ready）。
- `stop_stdin_wrap()` 可选，供测试清理。
- **永不** log/extras 打印 secret 值。

### 接到 `windows_local_v1` / `resolve_windows_local_v1_creds`
- 通道开关：`TELEAGENT_WIN_CREDS_CHANNEL` = `auto`（默认）| `stdin_wrap` | `env` | `off`  
  - `auto`：本进程缺凭据且 PEB 失败（或跳过 PEB）→ 尝试 stdin_wrap  
  - `stdin_wrap`：强制 wrap  
  - `env`：只本进程 env  
  - `off`：不 wrap  
- 成功后：`creds_source=stdin_wrap`；并把密钥注入**当前进程** `os.environ`（仅运行时），以便后续 glue/doctor 走现有 Basic+local-v1。  
- 同时设 `TELEAGENT_BASE_URL` 为 wrap 的 base（若尚未设）。检查仓库里实际用的 env 名保持一致。

### doctor extras
- `creds_source` 可含 `stdin_wrap`
- 失败时 `creds_blocker` 可增：`stdin_wrap_bin_missing` / `stdin_wrap_ready_timeout` / `stdin_wrap_spawn_failed`
- 保留既有 `openprocess_vm_read_denied` / `environ_secrets_stripped`

### 文档 `docs/windows-teleagent-adapter.md`
- 新节「合法凭据通道」：IPC 无结果；stdin_wrap 机制；端口 4401；优先级表；禁项。
- 标明：wrap 是与 GUI **并行**的受控实例，不是刮 GUI 密钥。

### 单测（mock subprocess / ready）
- payload 长度头正确；SECRET 键不在传入子进程的 OS env 字典里  
- ensure 成功 → presence.source=`stdin_wrap`  
- bin 缺失 → blocker  
- 现有测保持绿  

### 验收
```bash
cd /workspace/teleagent-collab
PYTHONPATH=src python3 -m unittest \
  teleagent_adapter.test_adapter_contract \
  teleagent_adapter.test_windows_process_environ \
  teleagent_adapter.test_windows_stdin_wrap -q
git push origin main
```
回报：SHA、文件清单、通道一句话。

不要实现 SeDebug/CM/读 token.json；不要改 Antigravity；不要动海景房。真机 live 留给工坊在推送后做。
