# Hermes 最小派工客户端

薄封装：`bin/hermes-collab-request.py`。只做 Application API 的 HTTP 调用（open / status / report / wait），**不**持有账号池、**不**拉起 agy 子进程、**不**把 Hermes 当账本。

工人后端（`teleagent-windows` / `antigravity` / `inprocess`）在 **`collab-service` 启动时**选定；本客户端的 `--backend` 仅作调用方标注（写入 `caller_backend_hint`），当前服务端会忽略未知字段。完整 API 见 [application-api.zh-CN.md](application-api.zh-CN.md)。

## 环境变量

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `COLLAB_API_BASE` | 服务根 URL | `http://127.0.0.1:8765` |
| `COLLAB_API_TOKEN` | 有则发 `Authorization: Bearer …` | 空（无 Bearer） |

密钥只进环境变量，不要写进仓库、profile 或本文件示例的真实值。

## 子命令

```text
python bin/hermes-collab-request.py open --goal "…" [--title …] [--backend antigravity|teleagent-windows]
python bin/hermes-collab-request.py status <request_id>
python bin/hermes-collab-request.py report <request_id>
python bin/hermes-collab-request.py wait <request_id> [--timeout 600] [--interval 2]
```

- 始终向 **stdout** 打一行 JSON；失败/超时时 JSON 仍打出，**exit ≠ 0**。
- `wait`：轮询至 `completed` / `failed` / `cancelled`。`completed` → 0；终态失败/取消 → 2；墙钟超时 → 3。
- `{id}` 会做 percent-encoding（含中文 Goal id）。

### 一行示例

```powershell
$env:COLLAB_API_BASE = 'http://127.0.0.1:8765'
# $env:COLLAB_API_TOKEN = '本机随机值'   # 若服务启用了 token
python bin/hermes-collab-request.py open --goal '在工作区写 delivery.md' --artifact delivery.md --backend antigravity
```

## 本机 Hermes 怎么调

事实（Win）：`C:\Users\Admin\AppData\Local\hermes\bin\hermes.exe`（约 v0.21.3）。Hermes 通过 **terminal / code_execution** 跑本脚本。已提供 Hermes skill：[`integrations/hermes/skills/teleagent-collab/SKILL.md`](../integrations/hermes/skills/teleagent-collab/SKILL.md)，安装与加载依据见 [integrations/hermes/README.zh-CN.md](../integrations/hermes/README.zh-CN.md)。

推荐：

1. 先起好 `python bin/collab-service.py … --backend antigravity`（或 `teleagent-windows`），见应用入口文档。
2. 在 Hermes oneshot / 会话里让它执行等价命令，例如：

```text
hermes -z
# 或 oneshot：在 prompt 里要求「用 terminal 调用仓库里的 python bin/hermes-collab-request.py …」
```

提示词片段（可直接贴）：

```text
用仓库 C:\Users\Admin\src\teleagent-collab 下的
python bin/hermes-collab-request.py
派一单：open → 记下 request_id → wait（或轮询 status）→ report。
环境变量 COLLAB_API_BASE / COLLAB_API_TOKEN 已在 shell 中时不要回显 token。
```

PowerShell 等价（无 Hermes 时人工验）：

```powershell
cd C:\Users\Admin\src\teleagent-collab
python bin/hermes-collab-request.py --help
$opened = python bin/hermes-collab-request.py open --goal 'hello' --artifact delivery.md | ConvertFrom-Json
python bin/hermes-collab-request.py wait $opened.request_id --timeout 120
python bin/hermes-collab-request.py report $opened.request_id
```

## 单测

不依赖真 service：

```powershell
# 仓库根；PYTHONPATH=src
python -m test_hermes_collab_request
```

## 非目标（本刀不做）

- （已交付另刀）collab-service per-dispatch 换号见 [agy-account-pool.md](agy-account-pool.md)
- 观察切面大改、把 Hermes 做成状态库
- 把 `AGY_AUTO_APPROVE` 写进用户 profile
