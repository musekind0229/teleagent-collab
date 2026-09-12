# 阶段 0：基线清单（2026-09-12）

对照设计稿 `/workspace/agent-exec-framework-v0.1.md` §13 阶段 0。本机调查，未升级部署、未改永续记忆。

## 1. 仓库与 tip

| 仓库 / 路径 | 设计稿曾写 | 本机核对 | 说明 |
| --- | --- | --- | --- |
| `teleagent-collab` `/workspace/teleagent-collab` | `2e18392`（稿注未 fetch） | **`3e4526ff3af471b2f19634be66cab31bcc86f43c`** = `origin/main` | `git fetch` 后 tip；含 live lead `application_id`/`context_summary` const 钉死 |
| `teleagent-windows-migration` | `5867eeb` | **本机未找到该仓** | `/workspace`、`/home/box` 深度搜索无独立 migration 树；Win 相关仅见 collab 内 `WindowsBlockedAdapter` |
| 设计全文 | — | `/workspace/agent-exec-framework-v0.1.md` | 唯一设计依据 |
| 本阶段交付 | — | `/workspace/agent-exec-framework-v0.1-phase01/` | 文档+schema，未并入 collab main |

## 2. 部署点

| 点 | 状态 | 证据 |
| --- | --- | --- |
| 本机 Linux TeleAgent | **SAC 在听** | `GET :4399/global/health` → **HTTP 401** `local_auth_missing`（需鉴权，非连接拒绝） |
| 本机版本 | 2.5.0-1 | `dpkg` / `/opt/TeleAgent` |
| 本机 collab 入口 | 可用 | `bin/run-job.py`；迷你真机曾 ok（`ses_f7dc3eab…` @ `3e4526f`） |
| `mde.museling.fans:2222`（devbox） | **停在登录前** | SSH OK；TeleAgent 2.5.0 已装；collab `/opt/collab/teleagent-collab` @ `3e4526f`；Xvfb+x11vnc `127.0.0.1:5900`；进程在、**无 :4399** |
| Cloud Agent | 不可用 | 计划无 Cloud Agents（历史确认） |
| 本机 Hermes | 源码在 | `/home/box/.hermes/hermes-agent` tip **`b6f42c66`**；**未**当作已选内核，未做 Kanban 真机接 TeleAgent |

## 3. 能力矩阵（未验证 = unsupported）

图例：`ok` 已有证据 · `partial` 有路径但不完整 · `blocked` 代码明确拒绝 · `unsupported` 本阶段无验证

| 能力 \ 组合 | Linux × TeleAgent 2.5 | Win × TeleAgent（collab WindowsBlocked） | Win × 独立 migration 实测分支 | Linux × Hermes Kanban | macOS × * |
| --- | --- | --- | --- | --- | --- |
| 建会话 / prompt | ok（本机 :4399） | blocked | unsupported（仓未在本机） | unsupported | unsupported |
| 权限弹权往返 | ok（迷你真机 once/reject） | blocked | unsupported | unsupported | unsupported |
| 硬规则 + lead 决策绑定 | ok（`3e4526f`） | n/a | unsupported | unsupported | unsupported |
| force_lead_review 收工 | ok | n/a | unsupported | unsupported | unsupported |
| Question API | partial（测例/文档） | blocked | unsupported | unsupported | unsupported |
| 取消确认 = 已停止 | partial（astra 语义；真机面窄） | blocked | unsupported | unsupported | unsupported |
| 系统安装真装 | blocked（受控假包 only） | blocked | unsupported | unsupported | unsupported |
| 外部 worker lane 接 TeleAgent | unsupported | unsupported | unsupported | **unsupported**（文档：external CLI lane 非铺好路径） | unsupported |
| 执行前拦截「全工具」 | partial（仅观察到的 permission 面） | blocked | unsupported | unsupported | unsupported |

**重要**：`WindowsBlockedAdapter` **不能**覆盖设计稿所述「已完成真实安装试验的 Windows 分支」——该分支证据不在本机；矩阵中必须分开两列，不得合并成「Win 全局 blocked」。

## 4. 工人层好不好用（本刀）

- **本机**：好用作为调查/跑单底座——HTTP 面在，401 表示鉴权缺失而非宕机；写文档未强制烧工人额度。
- **devbox**：装包与虚拟屏就绪，**登录未完成** → 不能当工人验收机。
- **Grok Build**：本刀未调用；按派单仅预留「小 schema/测试」场景。
- **Cloud Agent**：不用。

## 5. 可回退

- 本刀只新增 `/workspace/agent-exec-framework-v0.1-phase01/` 文档与 schema。
- **未改** `teleagent-collab` 代码、未推 main、未动远端部署。
- 旧入口 `run-job.py` / `run-scheduler.py` 继续可用。
