# TeleAgent 最小 Wrapper 说明

原生 HTTP 已能跑完一单（本刀 probe-sandbox 验证），但仍缺：签名封装、status.json、pending 中断、密钥生命周期。此文件只描述输入输出与伪代码步骤，**不实现大项目**。

## 何时需要

- 编排系统不能直接持有 Electron 子进程环境里的本地口令 / sessionKey
- 需要统一的 `status.json` 给上游状态机
- 需要在 `need_approve` 时暂停并交给审核 agent/人（默认不要 auto-approve）

## 输入

| 字段 | 含义 |
| --- | --- |
| `workspace` | 绝对路径（仅允许约定沙箱，如 probe 或任务专用目录） |
| `instruction` | 用户任务文本 |
| `session_id` | 可选；空则新建 |
| `model` | 默认 `NewApi/chat-lite` |
| `timeout_sec` | 墙钟超时 |
| `on_permission` | `pause`（默认）\| `reject`\| `once`（慎用） |
| `base_url` | 默认 `http://127.0.0.1:4399`（先探测；勿假定 4397） |

## 输出

- 工作区产物（模型写入的文件）
- `status.json`（形状见 contract）
- 可选：`messages.jsonl` 快照（脱敏）

## 伪代码步骤

```
1. assert GUI/SAC 在跑：ss 看 :4399；读 SAC/im-service 子进程 environ
   取 OPENCODE_SERVER_USERNAME/PASSWORD、SUPER_AGENT_LOCAL_SESSION_KEY
   （只进内存；禁止写进 git/报告）

2. def signed(method, path, body=None):
     构造 Basic + X-SA-* HMAC(local-v1)
     curl/HTTP 调用 base_url+path

3. if not session_id:
     session_id = signed("POST","/session", {title, directory: workspace},
                         headers={x-opencode-directory: workspace}).id

4. signed("POST", f"/session/{session_id}/prompt_async",
          {parts:[{type:text,text:instruction}],
           model:{providerID:NewApi, modelID:chat-lite}},
          headers={x-opencode-directory: workspace})
   # 期望 204

5. loop until timeout:
     st = signed("GET","/session/status")
     pending = signed("GET","/permission")
     if pending:
       write status.json state=need_approve, pending_permissions=pending
       if on_permission==reject:
         for p in pending: signed("POST", f"/permission/{p.id}/reply", {reply:reject})
       else:
         return  # 交给审核方；审核方同样调 reply once|always|reject
     if session not busy:
       msgs = signed("GET", f"/session/{session_id}/message")
       last_assistant = last role=assistant
       if last_assistant.error or finish==error:
         write status.json state=fail, error=...
         return
       if finish==stop (or 业务完成约定):
         artifacts = scan workspace (约定文件名 / git status)
         write status.json state=ok, ok=true, artifacts=...
         return
     sleep 1~2s

6. timeout → status.json state=timeout
```

## 原生 vs 必须包一层

| 能力 | 原生 | Wrapper |
| --- | --- | --- |
| 建 session / 发消息 / 读消息 | 是（4399） | 调用 |
| 本地鉴权签名 | 算法已公开于 asar | **必须**封装 |
| status.json | 无 | **必须**写 |
| pending 中断编排 | API 有 | **必须**轮询+暂停 |
| CLI `teleagent run` | 无 | 不要假造；用 HTTP |
| 云登录 token 解密 | 加密 token.json | **不要做**；依赖已登录 GUI |
| 全局 auto-approve | 禁止开启 | 禁止开启 |

## 安全约束（继承 brief）

- 试验目录隔离；不碰用户正忙会话/「TeleAgent的工作空间」任务
- 不把 token/密码写入报告或仓库
- 不卸载/升级/换号；不改用户项目代码
- 提权默认 reject 或 pause，不做 yolo

## 参考探针

本刀留下的只读/试验脚本：`/workspace/teleagent/probe-sandbox/probe_api.py`（从进程 environ 取钥并签名；可作 wrapper 雏形，非正式产品）。
