# Windows TeleAgent 适配器

> **Windows 真机未验收。** 本层按 Linux 工人 HTTP 契约实现并对齐单测（mock transport / 注入 HTTP）。
> 不要把 unittest 绿灯当成 live Win TeleAgent 已通。

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
| 默认 URL | `http://127.0.0.1:4399` |
| 端口发现 | 先 4399，再 4397；`TELEAGENT_BASE_URL` / `TELEAGENT_PORT` 可覆盖 |
| 鉴权 | HTTP Basic（用户 `super-agent`）+ `X-SA-*` `local-v1` HMAC |
| Session | `POST /session`，`directory` + 头 `x-opencode-directory`（**原样传 Win 路径**） |
| 投递 | `POST /session/:id/prompt_async` |
| 审批 | `GET /permission` → `POST /permission/:id/reply` body `{"reply":"once\|reject"}`（`always` 在适配层降为 `once`） |
| 提问 | `GET /question` / `POST /question/:id/reply\|reject` |
| 完成 | `GET /session/status`、`GET /session/:id/message` |

## 与 Linux 的差异要点

| 项 | Linux | Windows（本实现） |
| --- | --- | --- |
| 凭据来源 | GUI/SAC 子进程 `/proc/*/environ` | 进程环境变量 `OPENCODE_SERVER_PASSWORD` + `SUPER_AGENT_LOCAL_SESSION_KEY`（用户名缺省 `super-agent`）。**未**实现 Win32 读其它进程 environ / Credential Manager 刮取 |
| 端口 | glue 常用死写 `:4399` | 发现 4399→4397；未监听时仍回 4399，由 doctor 报 `not_running` |
| 路径 | POSIX | 驱动器号 / 反斜杠原样进 session；硬规则把 `\` 归一成 `/` 再匹配 |
| 硬规则 | `~/.ssh`、浏览器 profile、gh hosts、`.netrc` | 另含 `%USERPROFILE%\.ssh`、`AppData\...\Login Data` / Cookies、`Microsoft\Credentials` / Vault / Protect、Firefox `logins.json` 等（eternal reject） |
| doctor | 分类见下 | **同一套分类**；仅显式 blocked stub 才报 `blocked`。extras 带 `windows_live_verified=false` |
| 真机 | Debian 上 :4399 已验证 | **未验收** |

禁止：关鉴权、刮 GUI token、开公网诊断端口、把 Credential Manager 当凭据采集面。

## doctor 分类

`not_running` / `version_incompatible`（&lt; 2.5.0） / `missing_creds` / `auth_failed` / `api_incompatible` / `ok`

`blocked` 只在 `WindowsBlockedAdapter` 或 `simulated` 注入时出现。

## 测例（模拟）

```bash
PYTHONPATH=src python3 -m unittest teleagent_adapter.test_adapter_contract test_hard_rules test_p3_lead_question_install
PYTHONPATH=src python3 -m test_hard_rules
```

## 代码

| 文件 | 作用 |
| --- | --- |
| `src/teleagent_adapter/windows_local_v1.py` | Win 适配器 + 端口发现 + env 凭据 |
| `src/teleagent_adapter/windows_blocked.py` | 显式 blocked 降级 |
| `src/teleagent_adapter/linux_local_v1.py` | 共享 `LocalV1HttpAdapter` HTTP 实现 |
| `src/teleagent_adapter/doctor.py` | 跨平台分类 |
| `src/hard_rules.py` | Win 密钥路径片段 |
