# TeleAgent / Antigravity 接入审查

审查日期：2026-09-27。基线：远端 main `d9deeab`。本报告描述该固定基线，后续修复请以代码与回归验证为准。

## 结论

Antigravity 已有独立 CLI 工人后端、账号 HOME 选择、错误分类和 Windows 取消支持。但当前尚不等同于 TeleAgent 的 Grok 规划—执行—验收闭环：`bin/collab-service.py` 的后端入口仅接受 inprocess / teleagent-windows。建议暂缓扩展 Codex 组长，先修本报告的问题，再将单个 agy 工人接入已有上层闭环。

此前对项目“没有实际跑通”的判断需要更新：`docs/WINDOWS-LIVE-20260920.zh-CN.md` 记录了外部 HTTP → Grok 两步规划 → TeleAgent 执行 → Task 验收 → Goal completed 的实机证据。这里是读取已有记录，不是本次重新做了实机测试。它证明有限文件任务闭环，不等于永续人格层已经接入。

## 发现

### P1：stdout/stderr 管道未持续读取，大输出会使工人卡住

位置：[antigravity_cli_v1.py:471](https://github.com/musekind0229/teleagent-collab/blob/d9deeab/src/execution_backend/antigravity_cli_v1.py#L471)，管道创建于 351–355，collect_result 先 wait 于 594。

`Popen` 将两路输出接到 PIPE，但 `_harvest` 只有在 `poll()` 判定退出后才 communicate。输出超过管道缓冲时，子进程等待父进程读取、父进程等待子进程退出。真实开发的长 JSON 回复或日志可触发，外层最终把正常工作判成超时。

离线复现：假工人只输出约 200 KB JSON，observe 显示 busy；主动排空输出后立即 exit=0。建议启动后持续消费两路输出，或将输出写到受控临时文件；不能仅延长超时。

### P1：Antigravity 路径忽略强制验收，错误产物仍返回 ok

位置：[run_job_wire.py:316](https://github.com/musekind0229/teleagent-collab/blob/d9deeab/src/execution_backend/run_job_wire.py#L316)；最终成功条件：[antigravity_cli_v1.py:611](https://github.com/musekind0229/teleagent-collab/blob/d9deeab/src/execution_backend/antigravity_cli_v1.py#L611)。

同一章程接口支持 `force_lead_review` 和 `acceptance`，但 agy 分支不调用验收。成功只要求 CLI 成功且文件存在。复现：要求 answer.txt 内容严格为 RIGHT 并设置 force_lead_review=true，假工人写 WRONG，返回 ok=true/state=ok。

建议执行完成先标为待验收，接复用的审查路径；尚不支持时，显式拒绝强制验收合同，不能默默报通过。

### P1：账号池没有实际租约，串行只是文档要求

位置：[agy_account_pool.py:354](https://github.com/musekind0229/teleagent-collab/blob/d9deeab/src/execution_backend/agy_account_pool.py#L354)。

选择 available 后直接返回，没有跨进程锁、busy/reserved 状态或持续到进程退出的租约。两个独立入口可选择同一 HOME 并同时启动，即使它们使用不同工作目录。原子保存 JSON 并不能防止双重派工。

复现：连续模拟两个尚未释放的调用方，均选中 A，池中仍为 available。未操作真实 token，未声称本次实际损坏凭据。仓库自身文档已指出并发会损坏 oauth 文件；应当落实互斥而非依赖用户记住。当前阶段可以用一个 agy 全局跨进程锁保证串行，后续再细化每 HOME 锁和池状态事务。

### P2：直接轮询 ExecutionBackend 时不执行 timeout_sec

位置：[antigravity_cli_v1.py:527](https://github.com/musekind0229/teleagent-collab/blob/d9deeab/src/execution_backend/antigravity_cli_v1.py#L527)。

start_run 保存 started_at 和 timeout_sec，但 observe/_refresh 不检查期限。复现：timeout=0.1 秒，0.3 秒后仍 busy。当前 run-job 包装器有外层截止时间，因此其路径有保护；未来接入只轮询 busy 的 Goal 协调器时，这个缺口会显现。建议 backend 自身落实截止时间并确认停止，避免每个调用方重复实现。

## 接入与文档建议

1. 给 `collab-service` 接 agy 前先解决输出读取、验收和互斥。仅在 argparse 加一个 backend 选项还不够；agy 运行句柄目前在内存，需明确服务重启后如何对账/停止，不能假称可恢复。
2. agy 没有逐条审批回传通道，返回 501 是诚实的能力表达。不要把 TeleAgent 的权限监督能力自动归到 agy 身上；当前 skip-permissions 和“留在工作目录”的 prompt 也不等于 OS 隔离。
3. 部署文档 PowerShell 示例设置 `$env:AGY_AUTO_APPROVE='1'` 后没有恢复。这会留在当前 shell，影响后续命令，并不只作用于一次烟测。建议改为 try/finally 保存恢复原值，或使用独立子进程环境。
4. `503 / No capacity` 目前映射 exhausted，不会像 cooldown 自动到期恢复。建议区分账号额度耗尽和服务临时无容量，避免临时故障永久耗尽池。
5. 优先用一条固定账号执行真实小任务：规划 → agy → 收集证据 → 独立验收 → 失败返工/停止 → 最终报告。先证明单工人闭环，再扩账号池或第二组长。

## 验证

- Windows Python 3.12.4。
- `test_antigravity_cli_v1`、`test_agy_account_pool`、`test_app_service`：共 80 项，78 通过，2 live 测试跳过。
- 使用测试包装器拦截真实 cmdkey 调用，避免现有部分测试清除本机凭据槽；清理测试进程内 AGY/COLLAB_AGY 环境，未改变父进程或全局配置。
- [测试日志](../review/20260927-antigravity/tests.txt)。
- [离线复现脚本](../review/20260927-antigravity/reproduce.py) / [结果](../review/20260927-antigravity/result.json)。复现只启动 Python 假工人，操作临时目录，没有调用真实 agy、账号、模型或部署节点。
- 本次提交仅归档审查报告和离线证据，未修改产品代码。源代码与运行现状的结论以本次审查范围为限。

## 复现方式

在仓库根目录执行 `python -X utf8 review/20260927-antigravity/reproduce.py`。脚本仅使用临时目录和 Python 假工人，不会调用真实 agy 或 cmdkey。当前结果对应审查基线；后续修复后结果应随之变化。
