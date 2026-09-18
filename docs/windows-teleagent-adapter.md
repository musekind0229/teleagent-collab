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
| 端口发现 | **先 4399，再 4397**；`TELEAGENT_BASE_URL` / `TELEAGENT_PORT` 可覆盖。真机工人 HTTP 可能是 **:4397**（DESKTOP-TBB531F 面在、需鉴权） |
| 鉴权 | HTTP Basic（用户 `super-agent`）+ `X-SA-*` `local-v1` HMAC |
| Session | `POST /session`，`directory` + 头 `x-opencode-directory`（**原样传 Win 路径**） |
| 投递 | `POST /session/:id/prompt_async` |
| 审批 | `GET /permission` → `POST /permission/:id/reply` body `{"reply":"once\|reject"}`（`always` 在适配层降为 `once`） |
| 提问 | `GET /question` / `POST /question/:id/reply\|reject` |
| 完成 | `GET /session/status`、`GET /session/:id/message` |

## 与 Linux 的差异要点

| 项 | Linux | Windows（本实现） |
| --- | --- | --- |
| 凭据来源 | GUI/SAC 子进程 `/proc/*/environ` | **本进程 env** → 失败则读 TeleAgent/SAC/**其它进程** environ（Win32 PEB：`OpenProcess` + `NtQueryInformationProcess` + `ReadProcessMemory`，对标 Linux `/proc`）。用户名缺省 `super-agent`。**禁止** Credential Manager 刮取 |
| 端口 | glue 常用死写 `:4399` | 发现 4399→4397；真机可能是 **4397**；未监听时仍回 4399，由 doctor 报 `not_running` |
| 路径 | POSIX | 驱动器号 / 反斜杠原样进 session；硬规则把 `\` 归一成 `/` 再匹配 |
| 硬规则 | `~/.ssh`、浏览器 profile、gh hosts、`.netrc` | 另含 `%USERPROFILE%\.ssh`、`AppData\...\Login Data` / Cookies、`Microsoft\Credentials` / Vault / Protect、Firefox `logins.json` 等（eternal reject） |
| doctor | 分类见下 | **同一套分类**；仅显式 blocked stub 才报 `blocked`。extras：`windows_live_verified=false`、`creds_source=process_env\|foreign_process_environ\|missing`、端口 4397/4399 是否在听、password/session_key **是否存在**（bool）。**不写 secret 值** |
| 真机 | Debian 上 :4399 已验证 | **未验收**（见下节 `windows_live_verified`） |

禁止：关鉴权、刮 GUI token、开公网诊断端口、把 Credential Manager 当凭据采集面。

## 凭据发现顺序

1. **本进程**环境变量：`OPENCODE_SERVER_PASSWORD`（别名 `SUPER_AGENT_OPENCODE_PASSWORD`）+ `SUPER_AGENT_LOCAL_SESSION_KEY`；用户名 `OPENCODE_SERVER_USERNAME` / `SUPER_AGENT_OPENCODE_USERNAME`，缺省 `super-agent`。
2. **其它进程 environ**（仅当本进程缺 password 或 session key）：枚举本机相关进程（`TeleAgent.exe`，以及镜像路径含 `TeleAgent` / `super-agent-code` / `opencode` 等），读其环境块中的同上键。仅当 password **与** session key 都在时才采用。
3. 仍找不到 → `AdapterError(MISSING_CREDS)`，文案说明已扫其它进程 environ、非本进程 env。**不要**关鉴权。

HMAC session key 在进程内存，重启即换。官方数据根可能在 `%APPDATA%\TeleAgent` / `%LOCALAPPDATA%\TeleAgent`（含 OAuth `token.json`）——**只读路径备忘，本适配器不读盘、不刮 CM**。

非 Windows 平台：Win32 PEB 读取明确 raise；高层 finder no-op，不破坏 Linux。

## windows_live_verified

本刀代码已具备发现路径（本进程 env → 其它进程 environ；端口 4399→4397）。

| 项 | 状态 |
| --- | --- |
| 适配器 / doctor 代码 | 已合入；单测为 mock |
| DESKTOP-TBB531F 背景 | TeleAgent 在跑；工人 HTTP **:4397** 返回 **401**（面在，需鉴权）；当前 shell 无 `OPENCODE_SERVER_PASSWORD` / `SUPER_AGENT_LOCAL_SESSION_KEY` |
| live doctor / hello | **由工坊在 DESKTOP-TBB531F 另验** |
| `windows_live_verified` | **false**（doctor extras 如实；未 live 前不得改 true） |

## doctor 分类

`not_running` / `version_incompatible`（&lt; 2.5.0） / `missing_creds` / `auth_failed` / `api_incompatible` / `ok`

`blocked` 只在 `WindowsBlockedAdapter` 或 `simulated` 注入时出现。

## 测例（模拟）

```bash
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_adapter_contract -v
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_windows_process_environ -v
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_adapter_contract test_hard_rules test_p3_lead_question_install
```

测例字符串只用 `sim-pass` / `sim-key` 一类假值。

## 代码

| 文件 | 作用 |
| --- | --- |
| `src/teleagent_adapter/windows_local_v1.py` | Win 适配器 + 端口发现 4399→4397 + 凭据编排 |
| `src/teleagent_adapter/windows_process_environ.py` | Win32 PEB 读其它进程 environ（可注入 enumerator / block） |
| `src/teleagent_adapter/windows_blocked.py` | 显式 blocked 降级 |
| `src/teleagent_adapter/linux_local_v1.py` | 共享 `LocalV1HttpAdapter` HTTP 实现 |
| `src/teleagent_adapter/doctor.py` | 跨平台分类 + Win extras |
| `src/hard_rules.py` | Win 密钥路径片段 |

## Windows 控制层（本机 Grok CLI）

工人层（本适配器）在 DESKTOP-TBB531F 已通：HTTP **:4397** + PEB 凭据。控制层（Grok lead）默认接本机 Grok CLI，不要再用 Linux 的 `/workspace/run-grok.sh` 或 `:4399` 当 Win 默认。

```
set COLLAB_LEAD_ADAPTER=grok_cli
set COLLAB_LEAD_BIN=%USERPROFILE%\.grok\bin\grok.exe
set TELEAGENT_BASE_URL=http://127.0.0.1:4397
python bin/run-live-grok-lead.py
```

`COLLAB_LEAD_BIN` 未设或路径不存在时：PATH 上的 `grok` / `grok.exe`，再 `%USERPROFILE%\.grok\bin\grok.exe`，最后仅当文件存在才回退 `/workspace/run-grok.sh`。`TELEAGENT_BASE_URL` 未设时 Win 默认 `http://127.0.0.1:4397`。详见 `docs/lead-adapter.md`。不把本段当成 `windows_live_verified=true`。

### DESKTOP-TBB531F notes (2026-09-17)
- Worker HTTP observed on **:4397** (4399 closed).
- Cred discovery: this-process env, then Win32 PEB environ of TeleAgent/SAC candidates (fixed NtQuery ProcessInformationClass shadowing).
- `windows_live_verified` stays false until workshop live doctor/hello succeeds.

