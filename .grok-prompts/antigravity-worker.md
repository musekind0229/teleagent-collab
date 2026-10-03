# 刀：Antigravity（agy CLI）ExecutionBackend 工人适配

仓库：musekind0229/teleagent-collab  
基线 HEAD：`3a81c9c`（origin/main）  
角色：**工人**（ExecutionBackend），**不是** Lead。  
箱子已装 agy（`/home/box/.local/bin/agy`，约 1.2.x），已登录；可选 live smoke。

你是实现 agent。改码、测、文档、提交并 `git push origin main`。工坊只派收。不碰办公机/海景房。Hermes 不当账本。不要动 teleagent_adapter 主路径。不要做 Lead 适配。

## 验过的非交互形态（必须尊重）

```bash
agy --output-format=json --model=gemini-3.8-flash-low --dangerously-skip-permissions --print='…'
```

成功 JSON 含：`conversation_id` / `status` / `response` / `usage`。  
**坑**：`--print` 必须带 `=`，否则会把下一 flag 当 prompt。

## 现有工人面

- ABC：`src/execution_backend/base.py`（`ExecutionBackendABC` / Protocol）
- 现有：`inprocess.local_v1`；`bin/run-job.py` 还有 teleagent（glue）线
- 接线看：`src/execution_backend/run_job_wire.py`、`__init__.py`、`bin/run-job.py --backend`

## 范围（最小一刀）

1. 新增 **`antigravity.cli_v1`**（命名写死这个；`agy` / `agy.cli_v1` 可作为工厂别名）实现 `ExecutionBackendABC`：
   - `start_run`：在 `directory` 下起一次 agy print（cwd=`directory`）；instruction/charter 拼成 prompt；产物路径从 `charter.done_when.artifacts` 或 `artifacts` 参数拿。
   - `observe_run` / `collect_result`：映射子进程态与 JSON 结果（run_id 可用 `conversation_id` 或自生成 id 映射）。
   - `list_pending_actions` / `reply_permission`：默认 **fail-closed unsupported**（与 inprocess 一致）。**禁止**默认开 `--dangerously-skip-permissions` 当「自动批准」绕过合同。可选：仅当 charter/env 显式 `agy_auto_approve=true` 才允许 skip（文档写清，**默认关**）。
   - `cancel`：能杀子进程就杀，否则 unsupported。
2. 接线：
   - 工厂 `get_execution_backend`（若尚无则新增，并导出）+ `resolve_run_job_backend` / `bin/run-job.py --backend antigravity|agy`
   - env：`COLLAB_EXECUTION_BACKEND=antigravity.cli_v1`，`AGY_BIN` / `AGY_MODEL` 可配（默认 bin=`agy` 或 `/home/box/.local/bin/agy`，model 可用文档默认）。
3. 单测：模拟子进程（不强制真打 agy）；可选 live smoke 仅当 PATH 有 agy 且 skippable。
4. 短文档：`docs/antigravity-worker-adapter.md`（角色=工人、登录前提、flag 坑、与 inprocess/TA 分工、默认 skip-permissions=否）。

## 验收

- 模拟测例绿
- 工厂能选出 antigravity
- 推 origin/main
- 回报：SHA + 文件清单 + **默认是否 skip-permissions（必须默认否）**

## 不做

Lead 适配、办公机、Hermes 账本、把 agy 塞进 teleagent_adapter、默认 always skip-permissions。

开始前确认基线 `3a81c9c`。勿提交 `.grok-prompts/`、jobs 脏文件。
