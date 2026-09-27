# Windows：TeleAgent + Antigravity 部署

本机短清单。账号池见 [agy 账号池](agy-account-pool.md)，agy 工人见 [Antigravity 工人适配](antigravity-worker-adapter.md)，每日派工门见 [Windows 每日开工](WINDOWS-DAILY-STARTUP.zh-CN.md)。

## 1. 拉取仓库

```powershell
git clone https://github.com/musekind0229/teleagent-collab.git
cd teleagent-collab
git pull
```

已有检出时，在仓库根对齐 `origin/main` 即可。

## 2. agy 与 TeleAgent GUI

`agy` 在 PATH 上，或把 `AGY_BIN` 指到可执行文件。先登录桌面 TeleAgent，再派工。端口发现 **4399 → 4397 → 4398**（桌面常见 **:4397**）。不要为了探活改走 `--teleagent-stdin-wrap`。

## 3. 双 HOME

每个 agy 账号一个目录，例如 `C:\Users\Admin\agy-profiles\<id>`。池 JSON 的 `home` 用这个绝对路径。一次 spawn 钉死一个 HOME。

## 4. 文件凭据（只进子进程）

换号后库在**本次 spawn 的子进程 environ** 注入伪 `SSH_CONNECTION` / `SSH_CLIENT` / `SSH_TTY`，agy 1.2.11 才走文件 token。`GEMINI_FORCE_FILE_STORAGE=true` 仍会设置，但 **1.2.11 不认**。不要把这些变量写进 PowerShell profile 或机器环境。

串行换号时清机器槽 `gemini:antigravity`（`cmdkey /delete:gemini:antigravity`；槽不存在也继续）。

## 5. 串行换号

不要同时跑多个 `agy --print`。并发写可能把 oauth 文件尾部写坏。换号只发生在下一次 spawn。

## 6. 代理

本机示例 `http://127.0.0.1:7897`。写在池 JSON 的 `http_proxy` / `https_proxy`（账号或顶层），或 `COLLAB_AGY_HTTP_PROXY` / `COLLAB_AGY_HTTPS_PROXY`。只配了 HTTP 时镜像到 HTTPS。库内不写死地址。

## 7. 池 JSON

真实池（如 `jobs/agy-account-pool.json`）保存为 **UTF-8、无 BOM**，并且 gitignore。从 `jobs/examples/agy_account_pool.example.json` 复制，再填本机 `home`。文件里不要放 token。

## 配额轮换

`collect_result` 在本次 spawn 的环境里同时有 `COLLAB_AGY_ACCOUNT_POOL` 和 `AGY_PROFILE` 时，把该次结果分类写回池 JSON，并用临时文件 + `os.replace` 原子保存。池读写失败只记异常类型、不让任务采集失败，日志里不出现 token。

| 分类（看 stderr / error / stdout） | 账号 |
| --- | --- |
| `eligibility_blocked` / `auth_invalid` | `unavailable`。例如 `0001` 这种资格不合格，之后一直跳过 |
| `quota_exhausted` | `state=cooldown`，并写 `cooldown_until`（可用 `day_boundary`）。真配额：`MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED` / credits / `fetchQuotaStatus`。不是资格问题，也不是永久 `exhausted` |
| `temporary_no_capacity` | 可重试的 `503` / `No capacity`。**短** `duration` 冷却（`temp_cooldown_sec`，默认 60s），**不会**跟 `day_boundary` 等到次日零点，也不是永久耗尽 |
| `rate_limit` | 同样短 `duration` 冷却（与 `temporary_no_capacity` 共用 `temp_cooldown_sec`） |
| `ok` / `ordinary_task_failure` | 不改账号状态 |

`exhausted` 还留在状态集里，只给手工标记。分类器遇到配额 / 503 / 限流都走 `cooldown`（不是永久耗尽），到期后 `expire_cooldowns`（在 `load_pool` / `select_account`）把它提回 `available`。

池顶字段（保存在 JSON 里，不是账号条目）：

- `cooldown_sec`：秒，默认 300。`cooldown_mode` 为 `duration` 时，`cooldown_until = now + cooldown_sec`（UTC ISO）。**只作用于** `quota_exhausted`。
- `cooldown_mode`：`duration`（默认）或 `day_boundary`。`day_boundary` 的截止点是**本机本地时区**的下一个日历零点（这台 Windows 是中国标准时间，相当于 Asia/Shanghai），存成 UTC。过了该时刻才能再被选中。**只作用于**真配额。
- `temp_cooldown_sec`：秒，默认 60。给 `temporary_no_capacity` / `rate_limit`；**始终** `duration`，忽略 `day_boundary`。

环境变量可盖过池文件：`COLLAB_AGY_COOLDOWN_SEC`、`COLLAB_AGY_COOLDOWN_MODE`、`COLLAB_AGY_TEMP_COOLDOWN_SEC`。

两个处于 `available` 的号会轮换。`musekind0003` 被标成配额冷却后，下一次 `select_account` 或带 `--agy-account-pool` 的 spawn 应把 `AGY_PROFILE` 设成 `musekind0003-alt`。`unavailable`（含 `0001` / eligibility）不参与。

探测（不必烧掉真实额度）：

- 真配额：`MODEL_CAPACITY_EXHAUSTED`、`RESOURCE_EXHAUSTED`、`fetchQuotaStatus`、credits。
- 临时无容量：`No capacity`、`503 UNAVAILABLE`（无真配额关键词时）。
- 限流：`429` / overloaded / try again later。
- 轮换：标记之后再选一次，`AGY_PROFILE` 应换成另一个 `available` 号。
- 本刀用模拟的配额结果验证了轮换，没有对线上 agy 打出真实配额错误。

不要把 token 贴进日志、池文件或文档。

## 8. 每日 `--ready` 与 tip

```powershell
python bin/collab-service.py --ready
```

退出码 0 才开服务。reason 里出现 `running tip <…> != HEAD <…>` 时，重启 `collab-service`，让进程内 tip 与当前 `HEAD` 相同后再派。

## 9. hello 烟测才开 auto-approve

只给**本次**烟测进程打开 skip-permissions。推荐用 `try/finally` 清环境，避免污染后续同壳进程；或把变量只放进子进程环境。

```powershell
# 推荐：try/finally 清掉，避免后续手工 agy / 别的工单继承
$env:AGY_AUTO_APPROVE = '1'
try {
  python bin/run-job.py --backend antigravity jobs/examples/hello.charter.yaml
} finally {
  Remove-Item Env:AGY_AUTO_APPROVE -ErrorAction SilentlyContinue
  Remove-Item Env:COLLAB_AGY_AUTO_APPROVE -ErrorAction SilentlyContinue
}
```

```powershell
# 等价：只给子进程，不改当前壳
$env:COLLAB_AGY_ACCOUNT_POOL = 'jobs/agy-account-pool.json'  # 若用池
cmd /c "set AGY_AUTO_APPROVE=1&& python bin/run-job.py --backend antigravity jobs/examples/hello.charter.yaml"
```

等价：`COLLAB_AGY_AUTO_APPROVE=1`，或章程 `agy_auto_approve: true`。这只给这次烟测加上 `--dangerously-skip-permissions`，工人才能写出产物。不设时权限通道 unsupported，产物为空，finish 像 stop 也会假失败。不要把 skip-permissions 做成所有工单的默认，也不要把 `AGY_AUTO_APPROVE` 留在 PowerShell profile / 机器环境里。

## 10. 不要动别的机器

只操作本机这份检出和本机 agy profile。不要登录、改配置或派工到海景房和其他桌面。

## 11. 不要贴 token

禁止粘贴、打印或提交 access / id / refresh token、oauth JSON 和真实池文件。日志和异常同样不要带 token 正文。

## collab-service 路径（`--backend antigravity`）

经应用 Goal API 派工，而不是只跑 `bin/run-job.py`。成功标准：终态 + report；失败须清晰（need_human / error），勿静默。

```powershell
$env:COLLAB_API_TOKEN = '换成一个本机随机值'
$env:COLLAB_AGY_ACCOUNT_POOL = (Resolve-Path 'jobs/agy-account-pool.json').Path
$prevApprove = $env:AGY_AUTO_APPROVE
$env:AGY_AUTO_APPROVE = '1'
try {
  python bin/collab-service.py --persist .collab-app --port 8765 `
    --planner deterministic --backend antigravity `
    --agy-account-pool $env:COLLAB_AGY_ACCOUNT_POOL
} finally {
  if ($null -eq $prevApprove) { Remove-Item Env:AGY_AUTO_APPROVE -ErrorAction SilentlyContinue }
  else { $env:AGY_AUTO_APPROVE = $prevApprove }
}
```

另开终端交最小 hello（Bearer 同 token）：

```powershell
$headers = @{ Authorization = "Bearer $env:COLLAB_API_TOKEN"; 'Content-Type' = 'application/json' }
$body = @{
  idempotency_key = "agy-service-hello-001"
  client_id = "smoke"
  title = "agy service hello"
  goal = "In the assigned collab workspace only, create hello-from-worker.txt with exactly one short greeting line, then stop."
  boundaries = @{
    must = @("Stay inside the assigned workspace", "Create hello-from-worker.txt")
    must_not = @("Do not access credentials", "Do not use the network")
  }
  acceptance = @{ artifacts = @("hello-from-worker.txt"); text = "one greeting line" }
  budget = @{ wall_sec = 240; max_reworks = 0 }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/v1/requests -Headers $headers -Body $body
# 轮询 GET /v1/requests/{id} 与 /v1/requests/{id}/report 直到终态
```

与 `teleagent-windows` 合同差异（摘要）：
- **权限**：agy `reply_permission=501`；TA 有 session 级 ask / decision 回传。
- **句柄**：agy 内存句柄，服务重启不可恢复；TA / win_collab 控制器可持久观察。
- **AUTO_APPROVE**：只影响 agy 是否带 `--dangerously-skip-permissions`；必须 try/finally，勿写进 profile。
- **池**：启动时选号钉 HOME；配额/503 短冷却写回池 JSON，但本进程不会自动换到下一号（需重启 service 再选）。

