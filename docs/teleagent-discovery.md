# TeleAgent 操控面发现

- 调查环境：OS / 版本 / TeleAgent 版本
- 内核判断：是 OpenCode 套壳 / 像但不确定 / 不是（证据）
- 二进制与路径
- 配置与 session / 日志位置
- CLI 命令表（有 / 无 / 改名 + 帮助摘要）
- 本地服务（端口、协议、鉴权类型）
- Session 模型
- 提权审批模型
- Win vs Linux 差异
- 踩坑
- 未验证清单

---

## 调查环境

| 项 | 值 | 证据 |
| --- | --- | --- |
| OS | Debian GNU/Linux 13 (trixie)，内核 6.12.94+，容器/KVM | `cat /etc/os-release`；`uname -a` |
| TeleAgent 包 | `teleagent` **2.5.0-1** amd64 | `dpkg -s teleagent` |
| 厂商 | 中电信人工智能科技（北京）有限公司；Homepage https://teleai.com.cn | 同上 |
| 应用版本 | appVersion 2.5.0；Electron 43.1.0；Chrome 150；Node 24.18.0 | 启动日志 SysInfo |
| 内核 runtime | super-agent-code `2.5.0-dev-2-5-0-20260903153958`；头 `Super-Agent-Code-Version: dev-2.5.0-da4ba5`；`/version`=`1.2.27` | runtimes.version.json；curl |
| 调查范围 | **仅 Linux 本机**；试验目录 `/workspace/teleagent/probe-sandbox/` | 约束 |

## 内核判断

**结论：是 OpenCode（anomalyco）套壳 / 改名发行，厂商模块为 `super-agent-code`。**

证据：

1. 二进制 strings 含 `https://github.com/anomalyco/opencode`、`OPENCODE_*`、`opencode.ai/docs`（文案产品名换成 TeleAgent）。
2. HTTP 路由与上游 OpenCode API 同构：`/session`、`/permission/:requestID/reply`、`/event` SSE、`x-opencode-directory`。
3. 工具 schema：`https://opencode.ai/custom-tool-sandbox.schema.json`。
4. Go 路径：`code.srdcloud.cn/AI-Cloud/super-agent-code/...`。
5. 用户目录有 `im-bridge-opencode-state.json`。

## 二进制与路径

| 角色 | 路径 | 说明 |
| --- | --- | --- |
| GUI Electron | `/opt/TeleAgent/teleagent` | 无有效 CLI 子命令；再启 `Another instance detected, quitting` |
| PATH | `/usr/bin/teleagent` → alternatives → `teleagent-launcher` | 拉起 GUI |
| 启动包装 | `/workspace/teleagent/start-teleagent.sh` | gnome-libsecret + `--no-sandbox` |
| 内核二进制（改名） | ``<userData>/…`` | 听 API；非上游 `opencode` 名 |
| Scheduler CLI | `/opt/TeleAgent/resources/scheduler/bin/teleai-agent-schedule` | 定时任务，非对话工人主入口 |
| 独立 `opencode` | **未找到** | `which opencode` 空 |

数据根：``<userData>/…``。活跃用户：`.../users/v1_public_2085603988295041024/`。

## 配置与 session / 日志位置

| 类型 | 路径 | 备注 |
| --- | --- | --- |
| 登录 token（加密） | `.../users/<uid>/app-auth/token.json` | **仅确认路径存在**；已脱敏 |
| Session DB | `.../users/<uid>/teleagent.db` | 表 session/message/part/todo/permission/project |
| Runtime 配置 | `GET :4399/config` | permission/provider/agent/small_model |
| 内核日志 | `.../log/super-agent-server-YYYY-MM-DD.log` | 含完整路由表 |
| Electron 日志 | `.../logs/main-2.5.0.log`；js-log | |
| Scheduler | `.../scheduler/scheduler.db` + daemon-state.json | |
| 默认 GUI 工作区 | `.../TeleAgent的工作空间/` | 禁止试验 |
| 试验目录 | `/workspace/teleagent/probe-sandbox/` | 本刀专用 |

## 进程列表（GUI 外）

- Electron main/renderer/...
- `runtimes/super-agent-code/bin/TeleAgent` → **主 HTTP**
- scheduler daemon → 8080
- im-service → 17802
- playwright-mcp 子进程
- **无**独立 `opencode serve` / bun

## CLI 命令表

**GUI `teleagent`**：无 run/serve/attach/acp/session/agent。

**内核 TeleAgent**：绝大多数子命令 `unknown command`；**仅确认** `export <sessionID>`（usage 字符串）。上游 `opencode run/serve/attach/acp` **未暴露**。

**teleai-agent-schedule**：daemon/add/list/get/update/delete/enable/disable/run（定时任务）。

## 本地服务（端口、协议、鉴权）

### 端口归属

| 端口 | 绑定 | 进程 | 归属 |
| --- | --- | --- | --- |
| **4399** | `*:4399` | TeleAgent (SAC) | **主工人 HTTP API**（Hertz） |
| **19876** | 127.0.0.1 | TeleAgent (SAC) | **仅 MCP OAuth callback**；普通 REST 404 |
| **8080** | 127.0.0.1 | scheduler | 定时任务控制面 |
| **17802** | 127.0.0.1 | im-service | IM；非工人主路径 |
| 4397 | 未监听 | — | env `OPENCODE_BASE_URL` 写 4397，本刀实听 **4399** |

### 4399 鉴权（已通）

1. HTTP Basic：用户名 `super-agent`；密码在子进程环境（`OPENCODE_SERVER_*` / `SUPER_AGENT_OPENCODE_*`，**不进报告**）。
2. 本地 HMAC：`X-SA-Sign-Version: local-v1` + Timestamp/Nonce/Signature；`HMAC-SHA256(SUPER_AGENT_LOCAL_SESSION_KEY, "local-v1\nMETHOD\npath\nts\nnonce")` → base64url。
3. 缺签：`{"code":"local_auth_missing"}`。

健康：`GET /global/health` healthy；`GET /version` → 1.2.27；`GET /openapi` 有摘要。完整路由见内核启动日志（含 `/session*`、`/permission*`、`/question*`、`/event`、`/mcp`、`/pty`、`/file*` 等）。

### 8080

Bearer `SCHEDULER_API_TOKEN` + 头 `X-Scheduler-Client: desktop-main`。验证：`GET /api/v1/health` ok；`/api/v1/jobs` 列表。

### 17802

`GET /health` 200；多数其它路径 unauthorized。

## Session 模型

- ID：`POST /session` → `ses_*`；落库 `teleagent.db`。
- 目录：body `directory` + header `x-opencode-directory`。本刀创建 `ses_f8e6e8bb0ffeSs1DKLG103og6S` @ `/workspace/teleagent/probe-sandbox`。
- 追加：`POST /session/:id/prompt_async`（204）或 `/message`；body 需 `parts`；建议显式 `"model":{"providerID":"NewApi","modelID":"chat-lite"}`（否则可能落到未登录 anthropic/groq 报错）。
- 状态：`GET /session/status` → busy 映射或 `{}`。
- 历史：`GET /session/:id/message`（finish/error/tool parts）。
- 用户示例会话 `ses_f8e7f6e8fffeJh5wFRCZIXVnQs` **只读未触碰**。
- 同 session 双写并发：**未验证**。

## 提权审批模型

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 列出 pending | **有** `GET /permission` | 200 `[]` |
| approve/deny | **有** `POST /permission/:id/reply`，`reply=once|always|reject` | strings + 路由 |
| 本刀 write/bash | 工作区内未出 pending，工具直接 completed | probe 消息 |
| 配置 deny | `/config.permission` 对若干 search 工具为 deny | 实测 |
| 是否必须点窗 | **未验证**（未见 pending 弹窗路径） |
| 批后自动续跑 | **未验证** |

## 一单生命周期（已跑通）

1. 启动：`POST /session` → `prompt_async` + NewApi/chat-lite  
2. 运行：`/session/status` = busy  
3. 结束：status `{}`；assistant `finish:"stop"`；失败看 `info.error`  
4. 产物：`/workspace/teleagent/probe-sandbox/hello.txt`（含目标行；另有 AI 水印/零宽前缀）  
5. 错误可读：是（如 provider not authenticated）

## Win vs Linux

**仅 Linux（Debian 13 容器）验证。** Windows DESKTOP-AD1AEGS 本刀不做。存在 `bash.windows.ts` / powershell 适配，跨系统是否同一 HTTP 面 → **未验证**。

## 踩坑

1. GUI 无脚本子命令；工人面是 **4399 HTTP**。  
2. 文档/env 4397 ≠ 实听 4399。  
3. 19876 不是主 API。  
4. 必须 Basic + local-v1 签名。  
5. 不指定 NewApi 易鉴权失败。  
6. Scheduler 缺 `X-Scheduler-Client` → 403。  
7. sessionKey 在进程内存，重启即换。  
8. 写文件可能带水印前缀。

## 未验证清单

- Windows 对照；高危提权 GUI 强制与否；reply 端到端与批后续跑  
- 同 session 并发；ACP/attach；`export` 输出格式；IM 写会话  
- yolo/全局 auto-approve（**故意未开**）；项目级 allow/deny 文件格式；`PUT /permission`；SSE 事件 schema；4397→4399 映射细节
