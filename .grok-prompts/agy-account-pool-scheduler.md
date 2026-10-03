# 任务：外围 agy 账号池调度接到 antigravity.cli_v1

**仓库**：`/workspace/teleagent-collab`，推 **origin/main**。
**禁止**：改 `teleagent_adapter` / glue 主路径；把 oauth / token / refresh / 完整凭据提交进 git；默认打开 `--dangerously-skip-permissions`；mid-run 热换 HOME；多账号并发。

参考（只读，勿把探针目录并进 collab）：
- `/workspace/agy-multi-account-probe/REPORT.md` §8
- `.grok-prompts/ref_error_classifier.py` + `ref_fake_errors.jsonl`（移植分类逻辑进 collab，**不要**运行时依赖探针路径）

基线 tip：`93b6493`（antigravity.cli_v1）。现有：
- `src/execution_backend/antigravity_cli_v1.py`：`Popen(..., env=dict(self._env()))`；`AntigravityCliExecutionBackend(environ=...)`
- `get_execution_backend("antigravity")` → `AntigravityCliExecutionBackend`
- `run_antigravity_charter(..., environ=...)` / `bin/run-job.py --backend antigravity`

## 要实现（最小外围）

### 1. 账号池模块
建议路径：`src/execution_backend/agy_account_pool.py`（或同包拆分，保持小）。

- 池 JSON 形状（示例无 token）：`id`（或 `name`）、`home`（绝对路径可配）、`state` ∈ `available|exhausted|cooldown|unavailable`；可选 `email_mask`/`notes`/`cooldown_until`。
- 示例文件：`jobs/examples/agy_account_pool.example.json`（假路径如 `/path/to/profiles/homeA`，**不要**指向真实 oauth、不要含 token）。
- `.gitignore`：确保不会误提交 `**/profiles/**/oauth*`、`.gemini/**`、真实池状态文件若含敏感路径可选；示例 JSON 可提交。

### 2. 错误分类器（移植）
在 collab 内实现（可同文件或 `agy_error_classify.py`）：
`eligibility_blocked` / `auth_invalid` / `quota_exhausted` / `rate_limit` / `ordinary_task_failure` / `ok`
- 匹配探针规则：`Eligibility check failed` / `not currently available in your location` → eligibility_blocked
- 模型 `503` / `No capacity` → **quota_exhausted**（≠ eligibility）
- 单测 fixture 可放 `src/fixtures/agy_fake_errors.jsonl` 或测内嵌；移植 ref fixture 里的 eligibility 两条。

### 3. 调度器
- `load_pool(path)` / `save_pool`（原子写）
- `select_account(pool, *, precheck=callable|None)`：只从 **available** 选；可选派工前极短探测（默认可 mock）：对候选跑 `--print`/`agy models`（测试用注入 mock）；按分类：
  - eligibility_blocked / auth_invalid → `unavailable`，**不派**，试下一个
  - quota_exhausted → `exhausted`（或 cooldown）
  - rate_limit → `cooldown`
  - ok → 选中
- 验收剧本：**A 成功**；把 A 标 **exhausted** 后下一单选 **C**；**B** 因 eligibility **永不被选**（池里 B 初始即可 unavailable，或 precheck 打成 unavailable）。
- **不做** mid-run 换号；一次 spawn 固定 HOME。

### 4. 接到工厂 / run-job
- env 或 CLI：例如 `COLLAB_AGY_ACCOUNT_POOL=/path/to/pool.json`；可选 `run-job.py --agy-account-pool PATH`。
- 选中账号后构造 environ：
  - `HOME=<profile.home>`
  - `GEMINI_FORCE_FILE_STORAGE=true`
  - `AGY_PROFILE=<id>`
  - 保留已有 `AGY_BIN` / `AGY_MODEL` / `AGY_AUTO_APPROVE`
- 传给 `AntigravityCliExecutionBackend(environ=...)` 或 `run_antigravity_charter(..., environ=...)`。
- `get_execution_backend("antigravity", ...)`：若 kwargs/env 有池，可返回已注入 environ 的 backend，或提供 `prepare_antigravity_environ_from_pool()` 给 wire 调用。**最小改** `run_job_wire.run_antigravity_charter` + `bin/run-job.py` 即可；不要大改 `antigravity_cli_v1` 核心（允许小改：结果里带回 `agy_profile` 元数据）。
- `reply_permission` 仍 unsupported；**默认 skip-permissions=否**。

### 5. 单测（mock subprocess + 假池）
`src/test_agy_account_pool.py`（或同类名）：
1. 假池 A available / B unavailable(eligibility) / C available
2. 第一单选 A；模拟成功后 mark A exhausted
3. 第二单选 C（永不选 B）
4. precheck 对 B 返回 eligibility → 跳过
5. classifier fixture 全绿（含 eligibility）
6. 工厂/run-job 带池 mock：断言传给 backend 的 env 含 `HOME=...homeA`、`AGY_PROFILE=A`、`GEMINI_FORCE_FILE_STORAGE=true`，且 **argv 不含** `--dangerously-skip-permissions`（除非显式 approve）
7. live 可选 `@unittest.skipUnless` / env 门闩，默认 skip

### 6. 短文档
`docs/agy-account-pool.md`：如何配池、状态机、禁止事项（API key ≠ Pro；不并发同一钥匙串；不提交 oauth；不 mid-run 换 HOME）。链到现有 `docs/antigravity-worker-adapter.md` 一句即可。

## 验收命令
```bash
cd /workspace/teleagent-collab
PYTHONPATH=src python3 -m unittest src.test_agy_account_pool src.test_antigravity_cli_v1 -v
# 确保旧 antigravity 测仍绿
# 确认无 oauth 进 git：
git status
# 若有真实 profiles 路径被 add，必须 unstage
git add -A   # 但仔细检查；排除 token
git commit -m "feat(agy): peripheral account-pool scheduler for antigravity.cli_v1"
git push origin main
```

## 回报（stdout 末尾）
1. SHA（push 后 tip）
2. 变更文件清单
3. 默认 skip-permissions=否（测断言一句）
4. 模拟：A→exhausted→C、B 永不选（PASS/FAIL）
5. 是否动过 teleagent_adapter（必须否）
