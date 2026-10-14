# collab 接手说明（给 Hermes，2026-10-14）

> 写给接手的 Hermes 秘书 / 项目经理。动手前先读本文件、`docs/HERMES-PITFALLS.zh-CN.md`，以及主人的进度文件 `G:\codex\collab-progress.md`。

## 一、现状

- **代码在哪**：GitHub 仓库 `musekind0229/teleagent-collab`，分支 `claude/agy-first-loop`。它基于 `origin/main` 的 `49fa3a3`（10-06）。本机检出位置是 worktree `G:\codex\projects\teleagent-collab-agy`。
- **注意**：`G:\codex\projects\teleagent-collab` 的本地 `main` 比 `origin/main` 落后 32 个提交，还有别人留下的未提交改动。**不要在那里开发，也不要把它当基线。**
- **本分支新增的提交**（都在 `origin/main` 之上）：

| 提交 | 内容 |
|---|---|
| `f63fe2b` | 账号池默认不再清空系统凭据 `gemini:antigravity`，主人自己的 agy 和 Antigravity 桌面端不会再被登出 |
| `de2fbae` | agy 交活后由组长审查：工作区快照 → 算改动 → 标越界 → 组长判 → 不通过就恢复越界改动并带着意见返工 |
| `9c63f55` | 同一任务重派时，内容没变的输入清单直接复用（以前返工必然报 `manifest destination already exists`） |
| `883b710` | 组长审查遇到协议失误（漏 `application_id` 等）再问一次 |
| `50eb494` | 交付报告 `delivery_report_md`，用目标语言写，给上层直接读 |

- **测试**：新增 `src/test_agy_post_run_review.py`（10 项）。全量测试 926 项，其中 12 项失败，**这 12 项在 `origin/main` 上一模一样地失败**（符号链接、DeepSeek 封装、只在 Linux 成立的心跳），不是本分支引入的，见第五节。

## 二、现在能跑什么

**已经实机验证过的闭环（10-14）**：调用方交目标 → Grok 组长规划（会拆成有依赖的多个任务）→ AGY 工人改代码 → 协调器算改动、标越界 → Grok 组长逐个任务审查 → 不通过就恢复越界改动并返工 → 交付报告。

| 实测 | 结果 |
|---|---|
| 固定规划 + 真 AGY，slugify | 59 秒完成，独立复跑 6 项测试通过 |
| Grok + 真 AGY，wordfreq（4 个子任务） | 约 10 分钟完成，4 个任务都经组长审查通过，越界为空 |
| Grok + 真 AGY，测试本来就会失败的 calc | AGY 如实记录“测试未全部通过”，没有越权去改 `calc.py`，组长通过 |
| Grok + 脚本化越界工人 | 组长拦下对输入文件的修改 → collab 恢复原文 → 返工 → 复审通过 |

最后一行的工人是模拟的，因为真 AGY 是否越界有随机性，没法按需诱发。组长和 collab 的机制是真的。10-14 上午的一次真实运行里，AGY 确实越界重写过上游任务的 `wordfreq.py`，那时还没有审查，没人发现，这正是补审查的原因。

## 三、怎么跑演示

在 worktree 根目录 `G:\codex\projects\teleagent-collab-agy`：

```powershell
# 拦截演示：真 Grok 组长 + 故意越界的模拟工人，约 1 分钟，不耗 AGY 额度
powershell -ExecutionPolicy Bypass -File demo\agy-review-demo.ps1

# 真实演示：真 Grok + 真 AGY 做一个小编码任务，几分钟，耗 AGY 和 Grok 额度
powershell -ExecutionPolicy Bypass -File demo\agy-review-demo.ps1 -Mode real -Pool G:\codex\projects\teleagent-collab\jobs\agy-account-pool.json
```

脚本在 `%TEMP%\collab-demo-<时间>` 里起服务、提交目标、等结束、打印交付报告并存成 `report.md`，最后停掉服务。

## 四、正式使用

```powershell
$env:COLLAB_API_TOKEN = '本机随机值'
$env:AGY_AUTO_APPROVE = '1'   # 只放在这个服务进程里
py -3.12 bin\collab-service.py --persist <持久化目录> --port 8791 --planner grok --backend antigravity --agy-account-pool <池 JSON>
```

调用方（包括 Hermes）只用客户端，不直接找组长或工人：

```powershell
$env:COLLAB_API_BASE = 'http://127.0.0.1:8791'
py -3.12 bin\hermes-collab-request.py open --goal "……" --artifact a.txt
py -3.12 bin\hermes-collab-request.py wait <request_id>
py -3.12 bin\hermes-collab-request.py report <request_id> --md
```

端口别用 8765，那是主人的 Token 看板。细节见 `docs/application-api.zh-CN.md` 里“agy 交活后的组长审查与返工”和“交付报告”两节。

## 五、已知缺口（按优先级）

1. **组长只看 diff 和工人自述，不亲手运行产物。** 下一步最值钱的是让审查前由协调器在工作区里跑一遍验收命令（例如测试），把真实输出交给组长。
2. **组长可替换还没实测。** `lead_adapter` 里已有 `codex_cli`、`deepseek_harness`，但审查（`kind=review`）只在 Grok 上实测过。
3. **多账号并发没实测。** 主人有多个 AGY 账号；池子支持多账号和租约，但目前池里只有一个 `main`，并发上限因此是 1。
4. **12 项老测试在 Windows 上失败**，与本分支无关，需要单独分类处理。
5. **合并到 main 需要主人批准。** 本分支只推送，不合并。

## 六、接手规矩（来自主人的执行方案）

- 每个新会话、每接一个新任务，第一件事是读进度文件和易错清单。
- 尽量用短会话，任务做完就结束，不在一个会话里滚雪球。
- 动手前先 `git fetch`，核对远端基线。
- **Hermes 管“做什么、为什么、做到什么程度”，collab 管“怎么做”。** 遇到实现层面的障碍，只能退回给 collab 处理或升级给主人，不得自行更换技术路线或引入新的模型、供应商。
- 先接小任务，交付时附可亲手验证的演示，并设额度上限，超出就停下来汇报。连续几次交付干净再放宽；出问题就退回上一级，并把问题补进易错清单。
- 从阶段二起，`collab-progress.md` 只由秘书维护；PM 和 worker 只交报告。
