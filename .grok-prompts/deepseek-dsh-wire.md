# 续刀：DeepSeek lead 包装接到公网 `dsh` headless

仓库：musekind0229/teleagent-collab  
基线 HEAD：`01d9f39`  
产品：https://github.com/deepseek-ai/deepseek-harness ，CLI `dsh`  
安装：`npx @deepseek-ai/dsh` 或全局/源码  
headless：`dsh --profile headless "task"`（也可 stdin 任务）  
现状：`bin/run-deepseek-lead.py` 在设了 `COLLAB_DEEPSEEK_HARNESS_BIN` 时仍 exit 2（未接线）

**硬约束**：不碰用户办公机/本机；不要把 `dsh` 当工人；不要开工具乱改仓库——lead 提示应禁止 bash/写文件，只出 JSON。

你是实现 agent。改码、测、文档、提交并 `git push origin main`。

## 事实（必须尊重）

- 默认 stdout = 终答文本，exit 0/1
- `--json` 是**事件流**，不是我们的 lead schema
- 我们要的仍是 **collab-lead-v1 决策 JSON**：
  - 权限：`once|reject|deny_job|demand_safe_path`
  - 验收：`pass|fail`
  - **字节级**回绑 `application_id` / `context_summary`

## 改动

1. **`bin/run-deepseek-lead.py`**：
   - 有 `COLLAB_DEEPSEEK_HARNESS_BIN`（或 PATH 上的 `dsh`）时：拼 headless 任务 =「只输出决策 JSON」+ envelope 里的 request/schema/prompt；调用类似 `dsh --profile headless "<task>"`（或文档确认的等价 stdin 方式）。
   - 解析 stdout 中**最后一个**合法 JSON 对象；再按 schema / validate 语义检查（至少 application_id 回绑、decision/verdict 合法集）。
   - validate 失败 / 非 0 exit → **exit 2**，stdout **禁止**假 once/pass。
2. **无 dsh / 无 key** 时仍 fail-closed，stderr 写清缺什么（bin、`DEEPSEEK_API_KEY` 等）。
3. **文档**（`docs/lead-adapter.md`）写清：
   ```bash
   export COLLAB_LEAD_ADAPTER=deepseek_harness
   export COLLAB_LEAD_BIN=.../bin/run-deepseek-lead.py
   export COLLAB_DEEPSEEK_HARNESS_BIN=dsh   # 或 npx 包装
   export DEEPSEEK_API_KEY=...
   ```
4. **单测**：mock subprocess，不断言真 API。可选 smoke：本机有 dsh+key 才跑（默认 skip）。
5. 更新既有 `test_deepseek_harness_lead` 里「HARNESS_BIN 仍 fail-closed」的断言：改为 mock 成功路径 / 失败路径，而不是「设了 bin 就永远 exit 2」。

## 不做

工人池、Win 真机、部署、改 glue 主路径（除非最小接线）、把 dsh 当 TeleAgent 工人。

## 交付

SHA、测试命令与结果、本机是否真跑过 dsh（没有就标「未接真模型」）。

开始前确认基线 `01d9f39`。勿提交 `.grok-prompts/`、jobs 脏文件。
