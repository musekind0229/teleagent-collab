# Antigravity（agy CLI）工人适配

角色：**工人**（`ExecutionBackend`），不是 Lead。  
不接办公机 / 海景房。Hermes 不是账本。不改 `teleagent_adapter` 主路径。

## 是什么

`antigravity.cli_v1` 把已登录的 **agy CLI** 接到编排工人面：在 job `directory` 下起一次非交互 `agy --print=`，用 JSON 结果 + 产物文件交货。

工厂名（写死）：**`antigravity.cli_v1`**。别名：`antigravity` / `agy` / `agy.cli_v1`。

```python
from execution_backend import get_execution_backend

be = get_execution_backend("antigravity.cli_v1")
# 或 "antigravity" / "agy" / "agy.cli_v1"
```

```bash
python3 bin/run-job.py --backend antigravity jobs/examples/hello.charter.yaml
python3 bin/run-job.py --backend agy jobs/examples/hello.charter.yaml
COLLAB_EXECUTION_BACKEND=antigravity.cli_v1 python3 bin/run-job.py jobs/examples/hello.charter.yaml
```

## 登录前提

箱子上要有 **agy**（PATH 或 `AGY_BIN`），并且 **已经登录**。本适配只 spawn CLI，不负责 `agy login`、不代管凭据。

默认 bin 解析：`AGY_BIN` → PATH 上的 `agy` → `/home/box/.local/bin/agy` → `agy`。  
默认模型：`AGY_MODEL` 或 `gemini-3.8-flash-low`（与已验过的非交互形态一致）。

## 已验过的非交互形态

```bash
agy --output-format=json --model=gemini-3.8-flash-low --dangerously-skip-permissions --print='…'
```

成功 JSON 含：`conversation_id` / `status` / `response` / `usage`。

**坑：`--print` 必须带 `=`。** 写成 `--print '…'` 或把 `--print` 与 prompt 拆成两个 argv 时，agy 会把**下一个 flag** 当成 prompt（例如把 `--dangerously-skip-permissions` 吃掉）。本适配始终使用 `--print=<prompt>`。

上面这条「验过形态」带 skip-permissions，**只说明 CLI 能跑**，**不是**本工人的默认合同。

## 默认 skip-permissions = 否

`--dangerously-skip-permissions` **默认关闭**。禁止把它当成 `reply_permission` 的自动批准，去绕过工人合同。

仅当章程或环境**显式**打开时才带该 flag：

| 开关 | 默认 | 打开方式 |
| --- | --- | --- |
| skip-permissions | **否** | 章程 `agy_auto_approve: true`，或环境 `AGY_AUTO_APPROVE=true` / `COLLAB_AGY_AUTO_APPROVE=true` |

`list_pending_actions` 返回空列表；`reply_permission` → **501 unsupported**（与 `inprocess.local_v1` 一致）。即使 `agy_auto_approve=true`，也**不会**把 skip-permissions 映射成 once/approve。

`cancel`：能杀 agy 进程组就杀；杀不了则 unsupported。

## 与 inprocess / TeleAgent 的分工

| 工人 | 入口 | 干什么 | 不干什么 |
| --- | --- | --- | --- |
| **TeleAgent**（默认） | `glue.run_job` / `:4399` | 已验证的本地 HTTP 工人 | 不是 agy |
| **inprocess.local_v1** | `--backend inprocess` | 进程内写产物，测编排 / 闭环 | 不调外部 agent |
| **antigravity.cli_v1** | `--backend antigravity` / `agy` | cwd=`directory` 起一次 agy print | 不当 Lead；不改 `teleagent_adapter`；Hermes 不当账本 |

编排账本仍是 collab（charter → `jobs/runs`）。agy 的 `conversation_id` 只作 `run_id` 映射，不是目标/任务账本。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `COLLAB_EXECUTION_BACKEND=antigravity.cli_v1` | 选工人（CLI `--backend` 优先） |
| `AGY_BIN` | agy 可执行文件 |
| `AGY_MODEL` | 模型，默认 `gemini-3.8-flash-low` |
| `AGY_AUTO_APPROVE` / `COLLAB_AGY_AUTO_APPROVE` | 仅 `true` 时才加 `--dangerously-skip-permissions` |
| `COLLAB_AGY_LIVE=1` | 打开可选 live smoke（单测默认 skip） |
| `COLLAB_AGY_ACCOUNT_POOL` | 外围账号池 JSON；见 [agy-account-pool.md](./agy-account-pool.md) |

## 测例

模拟子进程（假 agy，不打真服务）：

```bash
PYTHONPATH=src python3 -m unittest src.test_antigravity_cli_v1 -q
```

可选 live：PATH 有 agy 且已登录时，`COLLAB_AGY_LIVE=1` 跑一条无工具短问（仍**默认不** skip-permissions）。

多账号串行调度（HOME 隔离、一次 spawn 钉死一个 HOME）是外围账号池，见 [agy-account-pool.md](./agy-account-pool.md)。
