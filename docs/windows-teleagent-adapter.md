# Windows TeleAgent 适配器

> **Windows 真机未验收。** 本层按 Linux 工人 HTTP 契约实现并对齐单测（mock transport / 注入 HTTP）。
> 不要把 unittest 绿灯当成 live Win TeleAgent 已通。
> Live doctor / hello 由工坊在 **DESKTOP-TBB531F** 另验；未 live 前 `windows_live_verified=false`。

## 工厂

```python
from teleagent_adapter import get_adapter, WindowsLocalV1Adapter, WindowsBlockedAdapter

ad = get_adapter(platform="win32")       # WindowsLocalV1Adapter
ad = get_adapter(platform="windows")     # 同上
ad = get_adapter(platform="win32", blocked=True)  # 显式降级 → WindowsBlockedAdapter
```

`WindowsBlockedAdapter` 仍保留，供已知坏版本 / 策略阻断；**不再**是 `win*` 的默认唯一实现。

## 与 Linux 的同构面

假定 Win 官方包暴露与 Linux 相同的本地工人 HTTP（**未在真机证实**）：

| 项 | 约定 |
| --- | --- |
| 默认 URL | `http://127.0.0.1:4399`（发现失败时的回退） |
| 端口发现 | **4399→4397→4398**；`TELEAGENT_BASE_URL` / `TELEAGENT_PORT` 可覆盖，**以发现为准**。不要把默认 URL 写死成 4398。DESKTOP-TBB531F 曾见 **:4397**，重启后曾见 **:4398**（面在、需鉴权） |
| 鉴权 | HTTP Basic（用户 `super-agent`）+ `X-SA-*` `local-v1` HMAC |
| Session | `POST /session`，`directory` + 头 `x-opencode-directory`（**原样传 Win 路径**） |
| 投递 | `POST /session/:id/prompt_async` |
| 审批 | `GET /permission` → `POST /permission/:id/reply` body `{"reply":"once\|reject"}`（`always` 在适配层降为 `once`） |
| 提问 | `GET /question` / `POST /question/:id/reply\|reject` |
| 完成 | `GET /session/status`、`GET /session/:id/message` |

## 与 Linux 的差异要点

| 项 | Linux | Windows（本实现） |
| --- | --- | --- |
| 凭据来源 | GUI/SAC 子进程 `/proc/*/environ` | 代码顺序仍是 **本进程 env** → 其它 TeleAgent/SAC 进程 PEB/environ（Win32 `OpenProcess` + `NtQueryInformationProcess` + `ReadProcessMemory`）。**不能**再当作现行 GUI 启动的可靠来源（见下节 stdin 化）。用户名缺省 `super-agent`。**禁止** Credential Manager / 刮盘 / SeDebug |
| 端口 | glue 常用死写 `:4399` | 发现 **4399→4397→4398**；真机曾见 **4397** / **4398**；未监听时仍回 4399，由 doctor 报 `not_running` |
| 路径 | POSIX | 驱动器号 / 反斜杠原样进 session；硬规则把 `\` 归一成 `/` 再匹配 |
| 硬规则 | `~/.ssh`、浏览器 profile、gh hosts、`.netrc` | 另含 `%USERPROFILE%\.ssh`、`AppData\...\Login Data` / Cookies、`Microsoft\Credentials` / Vault / Protect、Firefox `logins.json` 等（eternal reject） |
| doctor | 分类见下 | **同一套分类**；仅显式 blocked stub 才报 `blocked`。extras：`windows_live_verified=false`、`creds_source=process_env\|foreign_process_environ\|stdin_wrap\|missing`、`creds_blocker`（见下）、端口 4399/4397/4398 是否在听、password/session_key **是否存在**（bool）。**不写 secret 值** |
| 真机 | Debian 上 :4399 已验证 | **未验收**（见下节 `windows_live_verified`） |

禁止：关鉴权、刮盘（含 OAuth `token.json`）、刮 Credential Manager、抬 `SeDebugPrivilege`、开公网诊断端口。

## 现行 TeleAgent：stdin 注入 SECRET_ENV_KEYS（PEB/environ 已废）

现行 Win TeleAgent（app.asar）把 `OPENCODE_SERVER_PASSWORD`、`SUPER_AGENT_LOCAL_SESSION_KEY` 等列入 **SECRET_ENV_KEYS**，经 **stdin env payload** 注入 Go 后端，**故意不写 OS environ**（防 `ps -E` / `/proc/*/environ` / PEB）。

因此：

- **PEB / environ 发现在现行 Win 构建上已废。** 不要假设 Linux `/proc/*/environ` 老路径在 Windows 上仍成立。
- DESKTOP-TBB531F：`OpenProcess` 对工人/主进程要 `PROCESS_VM_READ` 时常 `ACCESS_DENIED`（`QUERY_LIMITED` 可开）；仅 renderer 一类进程可读，且 environ 块内无密钥。
- 本适配器 **不** 改为刮盘、Credential Manager、关鉴权、或抬 SeDebug 来「补」凭据。

## 合法凭据通道

DESKTOP-TBB531F 已核实：GUI 工人 **:4398**；`SECRET_ENV_KEYS` 经 **stdin env payload**（`uint32 BE length + JSON`）注入 Go，故意不写 OS environ → PEB 已废。无官方 named pipe / `exportCredentials` / `registerClient`（本地 API 密码）。受控父进程用同一 stdin payload 拉起内核后，`/ready` 200，local-v1 签名后 `/session` `/config` 200。

**stdin_wrap 是与 GUI 并行的受控实例，不是刮 GUI 密钥。** 默认端口 **4401**，不要抢 GUI 的 4398。密钥用 `secrets` 生成，只留在父进程内存（默认不落盘），成功后注入**当前进程** `os.environ`（仅运行时），以便 glue/doctor 走现有 Basic + local-v1。`TELEAGENT_BASE_URL` 若尚未设，则设为 wrap 的 base。

优先级（`TELEAGENT_WIN_CREDS_CHANNEL`，默认 `auto`）：

| 序 | 通道 | 说明 |
| --- | --- | --- |
| 1 | 本进程 env | `OPENCODE_SERVER_*` + `SUPER_AGENT_LOCAL_SESSION_KEY`。显式注入或 wrap 成功后的运行时 env |
| 2 | （历史）其它进程 PEB/environ | 现行 GUI 下通常失败，保留但标不可靠。`TELEAGENT_WIN_SKIP_PEB` 可跳过 |
| 3 | **stdin_wrap（本刀）** | 受控父进程用与 GUI 相同的 stdin payload 拉起 `{USERPROFILE}/.local/share/TeleAgent/runtimes/super-agent-code/bin/TeleAgent.exe`（或 `TELEAGENT_KERNEL_BIN` / LOCALAPPDATA / HOME 变体）。父进程内存持有密钥 → doctor/glue 用 |

通道开关：`auto`（本进程缺凭据且 PEB 失败；**若 loopback 4399 / 4397 / 4398 任一在听则不 wrap**，fail-closed `MISSING_CREDS`。优先接现场 GUI 走 NewApi；Local Storage wrap 可能 40108）\| `stdin_wrap`（强制 wrap，即使 GUI 在听）\| `env`（只本进程 env）\| `off`（不 wrap）。

`auto` 下 wrap **仅**在 GUI 工人端口均未监听时发生。现场 GUI 已在听时不要并行 LS wrap：NewApi 登录态应以 GUI 为准，wrap 可能 40108。

stdin payload 至少含：`SERVER_PORT`、`OPENCODE_SERVER_USERNAME`（默认 `super-agent`）、`OPENCODE_SERVER_PASSWORD`、`SUPER_AGENT_LOCAL_SESSION_KEY`、`SUPER_AGENT_SERVER_URL=http://127.0.0.1:{port}`。子进程 **OS environ** 仅 `SystemRoot` / `PATH` / `TEMP` / `USERPROFILE` / `XDG_DATA_HOME` 等非密；SECRET 键禁止留在子进程 OS environ。`XDG_DATA_HOME` 建议 `{TEMP}/teleagent-collab-wrap/xdg`，减轻与 GUI 数据打架。找不到内核二进制 → `creds_blocker=stdin_wrap_bin_missing`（blocker，不要猜 Program Files GUI exe）。

禁止：Credential Manager、关鉴权、抬 `SeDebugPrivilege`、刮 OAuth `token.json` 落盘当 API 密码。海景房禁止。Antigravity 不动。永不 log/extras 打印 secret 值。

## 凭据发现顺序（含历史 PEB 兜底）

1. **本进程**环境变量：`OPENCODE_SERVER_PASSWORD`（别名 `SUPER_AGENT_OPENCODE_PASSWORD`）+ `SUPER_AGENT_LOCAL_SESSION_KEY`；用户名 `OPENCODE_SERVER_USERNAME` / `SUPER_AGENT_OPENCODE_USERNAME`，缺省 `super-agent`。
2. **其它进程 environ**（仅当本进程缺 password 或 session key，且通道不是 `env` / `stdin_wrap`、未设 `TELEAGENT_WIN_SKIP_PEB`）：枚举本机相关进程（`TeleAgent.exe`，以及镜像路径含 `TeleAgent` / `super-agent-code` / `opencode` 等），读其环境块中的同上键。仅当 password **与** session key 都在时才采用。**现行 GUI 下通常失败。**
3. **stdin_wrap**（通道 `stdin_wrap`，或 `auto` 且前两步失败 **且 4399/4397/4398 均未监听**）：受控并行内核，见上节。`auto` + GUI 已在听 + 无本进程/PEB 凭据 → 不 wrap，直接 `MISSING_CREDS`（fail-closed；优先现场 GUI / NewApi，LS wrap 可能 40108）。
4. 仍找不到 → `AdapterError(MISSING_CREDS)`。文案说明已扫其它进程 environ、非本进程 env；并点明现行 TeleAgent 可能经 stdin 注入 SECRET_ENV_KEYS、PEB/environ 可能无效。**不要**关鉴权 / 刮 CM / 抬 SeDebug。

HMAC session key 在进程内存，重启即换。官方数据根可能在 `%APPDATA%\TeleAgent` / `%LOCALAPPDATA%\TeleAgent`（含 OAuth `token.json`）——**只读路径备忘，本适配器不读盘、不刮 CM**。

非 Windows 平台：Win32 PEB 读取明确 raise；高层 finder no-op，不破坏 Linux。

## doctor extras：`creds_blocker`

Win doctor 在 extras 中写入（**永不写 secret 值**）：

| extras 键 | 含义 |
| --- | --- |
| `creds_source` | `process_env` / `foreign_process_environ` / `stdin_wrap` / `missing` |
| `password_present` / `session_key_present` | 是否存在（bool），不是值 |
| `ports` | `4399` / `4397` / `4398` 是否在听（GUI 发现端口；wrap 默认 **:4401** 不在此表） |
| `windows_live_verified` | **false**（未 live 前不得改 true） |
| `creds_blocker` | 本进程缺凭据且未拿到完整凭据时的更细原因；否则 `null` |

`creds_blocker` 码：

| 码 | 条件 |
| --- | --- |
| `openprocess_vm_read_denied` | 存在 TeleAgent/SAC 候选进程，且对工人/主进程的 PEB 读因 `OpenProcess` / `ReadProcessMemory` 失败（`ACCESS_DENIED` / `WindowsEnvironUnavailable` from OpenProcess）；**且**未能从任何可读 environ 块解析到完整凭据 |
| `environ_secrets_stripped` | 至少一个候选进程 **成功** 读到 environ 块，但块内 **没有** password+session_key（现行 stdin 化典型：renderer 可读但无密钥；或工人 environ 被掏空） |
| `stdin_wrap_bin_missing` | stdin_wrap 通道已尝试，但运行时内核 `TeleAgent.exe` 不在 `TELEAGENT_KERNEL_BIN` / `{USERPROFILE}/.local/share/TeleAgent/runtimes/super-agent-code/bin/`（及 LOCALAPPDATA/HOME 变体） |
| `stdin_wrap_ready_timeout` | wrap 已 spawn，等待 `http://127.0.0.1:{port}/ready` 超时（fail-closed） |
| `stdin_wrap_spawn_failed` | wrap spawn / 写 stdin payload 失败，或内核在 `/ready` 前退出，或未知进程占用 wrap 端口 |
| `gui_model_auth_missing` | `TELEAGENT_WIN_REUSE_GUI_MODEL=1` 或 auto 已发现 GUI 鉴权文件但读/加密失败；**fail-closed，不 spawn 半配置 wrap** |

PEB 两码都有时优先 `environ_secrets_stripped`（证明 environ 路径已空）。wrap 已尝试且失败时优先 wrap 三码 / `gui_model_auth_missing`。都无则 `creds_blocker` 为空，`creds_source` 仍为 `missing`。`missing_creds` 时 `details` 带 fail-closed 说明（stdin / 禁止关鉴权、刮 CM、SeDebug）。

## windows_live_verified

本刀代码仍实现发现路径（本进程 env → 其它进程 environ 作历史/注入兜底 → stdin_wrap 并行内核；端口 **4399→4397→4398**）。现行 GUI 启动下 PEB/environ 不可靠，见「合法凭据通道」。

| 项 | 状态 |
| --- | --- |
| 适配器 / doctor 代码 | 已合入；单测为 mock |
| DESKTOP-TBB531F 背景 | TeleAgent 在跑；工人 HTTP 曾见 **:4397** 返回 **401**（面在，需鉴权）；重启后曾见 **:4398**（:4397 / :4399 关闭）。当前 shell 无 `OPENCODE_SERVER_PASSWORD` / `SUPER_AGENT_LOCAL_SESSION_KEY` |
| live doctor / hello | **由工坊在 DESKTOP-TBB531F 另验** |
| `windows_live_verified` | **false**（doctor extras 如实；未 live 前不得改 true） |

## doctor 分类

`not_running` / `version_incompatible`（&lt; 2.5.0） / `missing_creds` / `auth_failed` / `api_incompatible` / `ok`

`blocked` 只在 `WindowsBlockedAdapter` 或 `simulated` 注入时出现。

## 测例（模拟）

```bash
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_adapter_contract -v
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_windows_process_environ -v
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_windows_stdin_wrap -v
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_windows_gui_model_reuse -v
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_adapter_contract test_hard_rules test_p3_lead_question_install
```

测例字符串只用 `sim-pass` / `sim-key` / `fake-gui-token` 一类假值。永不提交真实 token。

## 代码

| 文件 | 作用 |
| --- | --- |
| `src/teleagent_adapter/windows_local_v1.py` | Win 适配器 + 端口发现 4399→4397→4398 + 凭据编排 |
| `src/teleagent_adapter/windows_process_environ.py` | Win32 PEB 读其它进程 environ（可注入 enumerator / block）+ 凭据通道编排 |
| `src/teleagent_adapter/windows_stdin_wrap.py` | 合法凭据通道：受控父进程 stdin wrap（默认 :4401，与 GUI 并行） |
| `src/teleagent_adapter/windows_gui_model_reuse.py` | 把已登录 GUI 的 model token / device-meta 经 stdin 注入 wrap（AES-GCM；永不 log token） |
| `src/teleagent_adapter/windows_blocked.py` | 显式 blocked 降级 |
| `src/teleagent_adapter/linux_local_v1.py` | 共享 `LocalV1HttpAdapter` HTTP 实现 |
| `src/teleagent_adapter/doctor.py` | 跨平台分类 + Win extras |
| `src/hard_rules.py` | Win 密钥路径片段 |

## Windows 控制层（本机 Grok CLI）

工人层 HTTP 在 DESKTOP-TBB531F 曾见 **:4397**，重启 TeleAgent 后曾见 **:4398**（:4397 / :4399 关闭）。**以端口发现为准**，不要把默认 `TELEAGENT_BASE_URL` 写死成 4398。控制层（Grok lead）默认接本机 Grok CLI，不要再用 Linux 的 `/workspace/run-grok.sh` 或 `:4399` 当 Win 默认。文档示例仍可用 `:4397`。

```
set COLLAB_LEAD_ADAPTER=grok_cli
set COLLAB_LEAD_BIN=%USERPROFILE%\.grok\bin\grok.exe
set TELEAGENT_BASE_URL=http://127.0.0.1:4397
python bin/run-live-grok-lead.py
```

`COLLAB_LEAD_BIN` 未设或路径不存在时：PATH 上的 `grok` / `grok.exe`，再 `%USERPROFILE%\.grok\bin\grok.exe`，最后仅当文件存在才回退 `/workspace/run-grok.sh`。`TELEAGENT_BASE_URL` 未设时走发现（**4399→4397→4398**）；lead 脚本示例仍可用 `:4397`。详见 `docs/lead-adapter.md`。不把本段当成 `windows_live_verified=true`。

### DESKTOP-TBB531F notes (2026-09-17)
- Worker HTTP observed on **:4397** (4399 closed).
- Cred discovery: this-process env, then Win32 PEB environ of TeleAgent/SAC candidates (fixed NtQuery ProcessInformationClass shadowing).
- `windows_live_verified` stays false until workshop live doctor/hello succeeds.

### DESKTOP-TBB531F notes (2026-09-18)
- After TeleAgent restart, worker HTTP observed on **:4398** (401 face present; **:4397** and **:4399** closed).
- Discovery order is **4399 → 4397 → 4398**. Do not pin default `TELEAGENT_BASE_URL` to 4398.
- Creds: this-process env empty; `OpenProcess` QUERY_LIMITED ok, **VM_READ** on main / `super-agent-code` worker **ACCESS_DENIED**; renderer environ readable but no password/session_key. Current TeleAgent injects `SECRET_ENV_KEYS` via **stdin env payload** (not OS environ) → PEB/environ discovery is dead on this build. doctor extras may report `creds_blocker=openprocess_vm_read_denied` or `environ_secrets_stripped`. Do not scrape disk / Credential Manager, disable auth, or raise SeDebugPrivilege.
- `windows_live_verified` stays **false**.

### DESKTOP-TBB531F notes (2026-09-19)
- GUI worker still **:4398**; SECRET_ENV_KEYS via stdin env payload (PEB dead). No official named pipe / exportCredentials.
- Legal creds channel: **stdin_wrap** — controlled parent spawn of `runtimes/super-agent-code/bin/TeleAgent.exe` on **:4401** (parallel to GUI, not scraping GUI secrets). Workshop live doctor/hello after this push.
- `windows_live_verified` stays **false**.

- Fix (same day): `resolve_windows_local_v1_creds` no longer trusts process env when `TELEAGENT_WIN_CREDS_CHANNEL=stdin_wrap` or `TELEAGENT_CREDS_SOURCE=stdin_wrap` — fall through to `ensure_stdin_wrap` so doctor `/version` matches the live handle (avoids auth_failed from stale env after hard reset).
- GUI model reuse: stdin_wrap injects `SUPER_AGENT_AUTH_STATE` + `OPENCODE_CONFIG_CONTENT` from GUI Local Storage `opencowork-auth` + `device-meta.json` (AES-256-GCM). After inject, live children should use `TELEAGENT_WIN_CREDS_CHANNEL=env`. 已由本控制器记录的旧 wrap 可停止；未知进程占用端口时等待后 fail-closed，不按端口误杀进程。
- `auto` + GUI worker TCP already on **4399 / 4397 / 4398**: do **not** `ensure_stdin_wrap`. Prefer the live GUI for NewApi; LS wrap can 40108. No process/foreign creds → fail-closed `MISSING_CREDS`. Wrap only when those ports are down, or `TELEAGENT_WIN_CREDS_CHANNEL=stdin_wrap`.

## GUI model reuse (stdin_wrap)

wrap 的 XDG 与 GUI 隔离，未注入 model 鉴权时 live `create_session` / `prompt` 会成功但 **lead_calls=0**（内核没有 NewApi 登录态）。本通道经 **同一 uint32-BE JSON stdin** 注入 GUI 已登录态，**不是** Credential Manager / PEB / SeDebug。

注入键（均走 stdin payload；SECRET 键禁止进子进程 OS environ）：

| 键 | 内容 |
| --- | --- |
| `SUPER_AGENT_AUTH_STATE` | `encryptAuthState({token, deviceId, installId}, sessionKey)` 的 JSON 字符串 |
| `SUPER_AGENT_LOCAL_SESSION_KEY` | wrap 已生成的 sessionKey（解密用） |
| `OPENCODE_CONFIG_CONTENT` | 最小 OpenCode JSON：`provider.NewApi.options.baseURL`、`small_model=NewApi/chat-lite`、`enabled_providers` 含 `NewApi`；`agent` / `mcp` 为空对象 |
| `OPENCODE_CONFIG_DIR` / `TELEAGENT_CONFIG_DIR` | 仅当 `%USERPROFILE%\.config\TeleAgent` **目录存在** 时写入（路径，非密） |

`encryptAuthState` 对齐 GUI：AES-256-GCM；key=`SHA256(utf8 sessionKey)`；iv=12 随机字节；输出 `{version:v1, iv, tag, ciphertext}`，各字段 base64url **不带** `=`。

鉴权来源（可用环境变量覆盖，供测例）：

| 材料 | 默认路径 | 覆盖 |
| --- | --- | --- |
| token | `%USERPROFILE%\.local\share\TeleAgent\Local Storage\leveldb\*.ldb`（也扫 `*.log`），key 字节 `opencowork-auth`，JSON `{state:{token:...},...}`；**优先最新 mtime**；brace-match 完整 JSON | `TELEAGENT_GUI_LEVELDB_DIR` |
| deviceId + installId | `%APPDATA%\TeleAgent\app-auth\device-meta.json` | `TELEAGENT_GUI_DEVICE_META` |
| NewApi `baseURL` | `https://agent.teleai.com.cn/superCowork/sapi/api/v1` | （写死，与 GUI log 一致） |

`TELEAGENT_WIN_REUSE_GUI_MODEL`：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | GUI 两份鉴权文件都在则注入；**在但读/加密失败 → fail-closed**，不 spawn 半配置 wrap |
| `1` / `true` / `on` | 必须注入；缺文件或损坏 → `creds_blocker=gui_model_auth_missing` |
| `0` / `false` / `off` | 不复用 GUI model（仅 local-v1 Basic + HMAC wrap） |

**孤儿端口：** 只停止本控制器状态文件记录的旧 wrap，并等待端口不再 LISTENING（默认约 10s，测例可 hook）再 spawn。未知进程占用 wrap 端口时不终止它，直接等待后 fail-closed。否则新密钥打到旧内核会得到 `local_auth_signature_invalid`。超时仍占用 → `stdin_wrap_spawn_failed`。

**注入成功后：** 后续 live 子进程应设 `TELEAGENT_WIN_CREDS_CHANNEL=env`，走本进程已注入的 env，**不要再 `ensure_stdin_wrap`**（会杀 wrap）。永不 log / extras / git 写入 token 或 ciphertext。永不提交真实密钥。

禁止：Credential Manager、关鉴权、SeDebug、PEB scrape、打印真实 token、动 Linux `/proc`、agy、海景房。

