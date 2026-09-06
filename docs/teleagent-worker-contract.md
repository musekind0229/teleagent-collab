# TeleAgent Worker Contract v0.1

> 范围：**仅 Linux 验证**（Debian 13 容器）。Windows 未测。  
> 密钥：报告不写 token/密码全文；运行时从 GUI 子进程环境读取本地口令与 sessionKey。

## 投递

- 启动一单的推荐方式（按优先级，仅已验证）：
  1. **本地 HTTP** `http://127.0.0.1:4399`（GUI 已登录且 SAC 进程在跑时）
  2. 无稳定「一键 CLI run」——GUI/`TeleAgent` 二进制均无 `run` 子命令（`export` 除外）
  3. Scheduler HTTP `:8080` 仅定时任务，不作为对话工人主入口
- 入参：
  - `workspace`：绝对路径；创建 session 时写入 `directory` + 头 `x-opencode-directory`
  - `instruction`：自然语言
  - `session_id?`：省略则 `POST /session`；有则 resume 到该 id
  - `permission_policy`：可读 `GET /config` 的 `permission`；运行时用 `GET/POST /permission*`；**禁止**开全局 yolo
  - `model`：建议固定 `NewApi` + `chat-lite|chat-pro|chat-flash`
- 示例请求（脱敏）：

```http
POST /session HTTP/1.1
Host: 127.0.0.1:4399
Authorization: Basic <super-agent:LOCAL_PASSWORD>
X-SA-Sign-Version: local-v1
X-SA-Timestamp: <ms>
X-SA-Nonce: <hex>
X-SA-Signature: <hmac-base64url>
Content-Type: application/json
x-opencode-directory: /workspace/teleagent/probe-sandbox

{"title":"job-1","directory":"/workspace/teleagent/probe-sandbox"}
```

```http
POST /session/{session_id}/prompt_async
...同上鉴权...
x-opencode-directory: /workspace/teleagent/probe-sandbox

{"parts":[{"type":"text","text":"<instruction>"}],"model":{"providerID":"NewApi","modelID":"chat-lite"}}
```

鉴权算法：`payload = "local-v1\n" + METHOD + "\n" + path+query + "\n" + ts + "\n" + nonce`；`HMAC-SHA256(SUPER_AGENT_LOCAL_SESSION_KEY, payload)` → base64url。

## 会话

- 新建：`POST /session` → `id=ses_...`（已验证）
- resume：对已有 id `prompt_async` / `message`（已验证追加）
- 指向「另一个对话」：使用对方 `session_id`；列表 `GET /session`
- 并发：同一 session 双写 → **未验证**（建议编排侧单写者）

## 审批

- pending 列出：`GET /permission`（已验证可达；本刀空闲为 `[]`）
- approve / deny：`POST /permission/{requestID}/reply`，body `{"reply":"once"|"always"|"reject"}`（路由+strings 已证实；**端到端未在本刀触发 pending**）
- 批准后是否自动续跑：上游语义倾向自动续；**未验证**。若卡住可再 `prompt_async` 发「继续」
- 本刀观察：工作区内 `write`/`bash echo` **未出现 pending**（可能默认允许）；若干 search 工具在 `/config.permission` 为 deny
- **禁止**为通单开启全局 auto-approve / yolo

## 完成

- 判定：
  - `need_approve`：`GET /permission` 非空 → state=`need_approve`
  - 运行中：`GET /session/status` 含该 sid 且 `type=busy`
  - 成功：status 无 busy，且最新 assistant `finish=="stop"`（或业务约定的完成标记），`error` 空
  - 失败：assistant `finish=="error"` 或 `info.error` 非空 → `fail`
  - 超时：编排侧墙钟超时 → `timeout`
  - `need_human`：出现 `/question` pending 或无法脚本处理的交互 → **部分未验证**
- 建议 status.json（原生不自带，由 wrapper 写）：

```json
{
  "ok": false,
  "state": "ok | fail | need_approve | need_human | timeout",
  "session_id": "",
  "exit_code": null,
  "artifacts": [],
  "log_path": "`<userData>/…`
  "pending_permissions": [],
  "error": ""
}
```

- 日志：上述 `log/super-agent-server-*.log`；消息真相源 `GET /session/:id/message` 与 `teleagent.db`
- 产物：默认写在 session `directory`（本刀证据：`/workspace/teleagent/probe-sandbox/hello.txt`）

## 包装脚本需求（若原生不够）

原生已能：建 session、投递指令、轮询 status/permission、读消息/错误。  
仍需 wrapper：

1. 从运行中进程环境安全读取本地 Basic 密码与 sessionKey（不落盘明文到仓库）
2. 计算 local-v1 签名并调用 4399
3. 轮询并写出 `status.json`；发现 pending 时中断给审核方（默认倾向 `reject` 除非编排明确 once）
4. 收集 artifacts（工作区 diff / 约定文件列表）
5. 端口发现：优先探测 4399；勿死写 4397

详见 `teleagent-min-wrapper.md`。

## 不保证

- 无 GUI 时独立冷启动完整登录云厂商链路（本刀假定 GUI 已登录并已拉起 SAC）
- Windows 行为
- 「必须点窗才能批」的所有工具类别
- 上游 `opencode` CLI 子命令（run/serve/attach/acp）在本发行版可用
- 全局 auto-approve
- IM（17802）作为工人投递面
- token.json 可被脚本直接解密使用（加密；且禁止写入报告）
