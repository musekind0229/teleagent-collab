# v0.3 阶段2 第一刀：job_end 产物齐 + finish=stop 不得判 fail

仓库：musekind0229/teleagent-collab  
基线 HEAD：`888fba5`  
设计：`/workspace/agent-exec-framework-v0.3.md` §11 阶段2（真实闭环）+ P2 实跑缺口  
参考失败 run：`jobs/runs/p2-st-reinstall-20260913T110115Z`  
  status：`ok=false` / `error: lead verdict=fail arts_ok=True missing=[] finish=stop`

你是实现 agent。改码、加测试、提交并 `git push origin main`。工坊只派收。

## 问题

P2 真 TA 把 SillyTavern 重装做成了（证据齐、HTTP 200、finish=stop），但 job_end / lead_review 路径仍写成合同 **fail**。纸面与物理验收脱节。

阶段2第一刀**只修这一条语义**：

> 当 `arts_ok=True`（`missing=[]`）且工人本轮 `finish=stop`（或等价成功 finish）时，job_end **不得**判 `fail`。

要求：

1. 停止/成功收工必须留下**可核证据**（session id、run/status、产物路径或指纹），不能只改状态字假装过了。
2. 若 lead 模型仍返回 fail，但产物门禁已齐且 finish=stop：内核应按合同成功收口（或等价成功），并记录 lead 意见为旁证/备注，而不是覆盖成 fail。具体落点你自选，但验收必须过。
3. 不要为了测去烧真 TeleAgent 额度；用 **inprocess** 注入「产物齐 + finish=stop」语义即可。

## 验收（必须）

1. 新增可重复测（建议 `src/test_framework_v03_s2_k1.py` 或等价）：复现「产物齐 + finish=stop」→ 结果为通过/等价成功，**不是** fail。
2. 回归仍绿：`src.test_framework_v03_s51`、`s52`、`s53`。
3. 成功路径的 status/report 里能核到 session/run 或产物证据字段（测里断言即可）。

## 不做

工人池、五入口整段接通、组长交接包、部署、Windows 真机、阶段2其他条目。不要扩大改 glue/lead 无关路径。

## 交付

SHA、测试命令与结果、这条语义验了没（明确写）。commit message 点明 § 阶段2-k1 / P2 job_end 缺口。

开始前确认基线 `888fba5`。勿提交 `.grok-prompts/`、jobs 脏文件。
