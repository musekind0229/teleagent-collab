# agy 账号池（外围串行调度）

给 `antigravity.cli_v1` 工人的**外围**账号池：每个账号一个 `$HOME` 目录（里面有已登录的 `.gemini/` 文件凭据），**一次 spawn 钉死一个 HOME**。不是 CLI 官方 `--profile`，也不改 `teleagent_adapter` / glue。

工人本体说明见 [antigravity-worker-adapter.md](./antigravity-worker-adapter.md)。

## 怎么配

1. 每个账号准备一份 HOME（完整 home 或至少含 `.gemini/`），预先用 `agy` 登录。文件凭据在 `<account.home>/.gemini/antigravity-cli/antigravity-oauth-token`（及 `.gemini/**`）。
2. 复制示例 `jobs/examples/agy_account_pool.example.json` 到**不会进 git** 的路径（例如 `jobs/agy-account-pool.json`），把 `home` 改成绝对路径。
3. 派工：

```bash
COLLAB_AGY_ACCOUNT_POOL=/path/to/agy-account-pool.json \
  python3 bin/run-job.py --backend antigravity jobs/examples/hello.charter.yaml

python3 bin/run-job.py --backend antigravity \
  --agy-account-pool /path/to/agy-account-pool.json \
  jobs/examples/hello.charter.yaml
```

选中账号后注入的环境（一次 spawn 固定）：

| 变量 | 值 |
| --- | --- |
| `HOME` | `<account.home>` |
| `USERPROFILE` | 仅 Windows：与 `HOME` 相同 |
| `GEMINI_FORCE_FILE_STORAGE` | `true`（前向兼容；agy **1.2.11 不认**） |
| `SSH_CONNECTION` | `203.0.113.1 50000 203.0.113.2 22`（RFC5737 TEST-NET 伪值） |
| `SSH_CLIENT` | `203.0.113.1 50000 22` |
| `SSH_TTY` | `windows-agy-pool` |
| `AGY_PROFILE` | 池里的 `id`（CLI 不识别，只给编排元数据） |
| `HTTP_PROXY` / `HTTPS_PROXY` | 仅当显式配置了代理 |

Win 上 agy **1.2.11** 走文件凭据的真实触发是子进程里的伪 `SSH_*`（agy 打出 `Using file-based token storage because SSH session detected`）。这是社区「检测 SSH → 文件凭据」做法（oaustegard `agy_auth_broker` / auth-internals、agy-switcher LINUX.md），**不是**官方多账号 API。`SSH_*` **只注入本次 spawn 的子进程 environ**，不写 PowerShell profile、系统环境或 `.bashrc`。

并发 `agy --print` 可能把 `antigravity-oauth-token` 写成尾部多余 `}`（google-antigravity/antigravity-cli#24）。本池换号保持**单次 spawn**，并清 `gemini:antigravity` 槽，避免钥匙串阴影。该 token 与 `.gemini/**` 已 gitignore；日志和异常禁止打印 token 内容。若二进制日后识别 `GEMINI_FORCE_ENCRYPTED_FILE_STORAGE` 可再评估；1.2.11 不依赖。

保留已有 `AGY_BIN` / `AGY_MODEL` / `AGY_AUTO_APPROVE`。`--dangerously-skip-permissions` **默认仍关闭**。

## Windows 串行换号

`gemini:antigravity` 钥匙串是机器级单槽。换号顺序：

1. `cmdkey /delete:gemini:antigravity`（槽不存在也继续；非 Windows 直接返回）
2. `HOME` 与 `USERPROFILE` 设为该账号 `home`，并注入上表伪 `SSH_*`（只进子进程）
3. 配置了代理才注入 `HTTP_PROXY` / `HTTPS_PROXY`
4. 再 spawn

代理来源：账号上的 `http_proxy` / `https_proxy`，否则池 JSON 顶层同名字段，否则 `COLLAB_AGY_HTTP_PROXY` / `COLLAB_AGY_HTTPS_PROXY`。只配了 HTTP 时镜像到 `HTTPS_PROXY`。库内不写死代理地址。

## 池 JSON

每条账号：`id`（或 `name`）、`home`（绝对路径；Windows 需带盘符）、`state`。可选 `email_mask` / `notes` / `cooldown_until`，以及 `http_proxy` / `https_proxy`。池顶也可以放这两个代理字段。

`state` ∈ `available` | `exhausted` | `cooldown` | `unavailable`。

示例里的 `/path/to/profiles/homeA` 是假路径，**不要**指向真实 oauth。

## 状态机

调度**只从 `available` 选**（池文件顺序，先到先得）。可选派工前探测（`--print` / `agy models`；测试注入 mock；实网需 `COLLAB_AGY_POOL_PRECHECK=1`）：

| 分类 | 调度 |
| --- | --- |
| `eligibility_blocked` | `unavailable`，**不派**，试下一个 |
| `auth_invalid` | `unavailable`，不派，试下一个 |
| `quota_exhausted`（含模型 `503` / `No capacity`） | `exhausted`（≠ eligibility） |
| `rate_limit` | `cooldown`（到期可回到 available） |
| `ok` | 选中 |
| `ordinary_task_failure` | 任务失败，不把账号标坏 |

剧本：A 可派 → 把 A 标 `exhausted` → 下一单选 C。B 因 eligibility 为 `unavailable`，**永不被选**。

`cooldown_until` 过期后，加载/选择时会把该号提回 `available`。

## 禁止事项

- **API key ≠ Pro**。`GEMINI_API_KEY` + `modelProvider=gemini` 不是 Google AI Pro 订阅额度池。
- **不要并发**打同一钥匙串 / 同一 HOME。本调度是串行的；并发写 token 可能在文件尾多一个 `}`。
- **不要提交 oauth**、access/id/refresh token、完整凭据、真实池状态（含真实 HOME 时）。token 在 `<account.home>/.gemini/antigravity-cli/antigravity-oauth-token`；日志和异常禁止打印其内容。
- **不要 mid-run 换 HOME**。一次 `Popen` 钉死 `HOME`；换号只发生在下一次 spawn。
- 不要默认打开 `--dangerously-skip-permissions`。`reply_permission` 仍 unsupported。
- 不要把 `eligibility_blocked` 当成未登录或额度耗尽；地域/产品不合格时重登同一账号无意义。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `COLLAB_AGY_ACCOUNT_POOL` | 池 JSON 路径（CLI `--agy-account-pool` 优先） |
| `COLLAB_AGY_POOL_PRECHECK=1` | 派工前对候选跑 live `agy models`（默认关；单测用 mock） |
| `COLLAB_AGY_POOL_LIVE=1` | 打开可选 live 单测（默认 skip） |
| `COLLAB_AGY_HTTP_PROXY` | 可选。写入 `HTTP_PROXY`；未另配 HTTPS 时镜像 |
| `COLLAB_AGY_HTTPS_PROXY` | 可选。写入 `HTTPS_PROXY` |
