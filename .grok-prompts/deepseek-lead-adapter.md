# 刀：DeepSeek harness 当 LeadAdapter

仓库：musekind0229/teleagent-collab  
基线 HEAD：`37e7315`  
参考：`docs/lead-adapter.md`、`src/lead_adapter/grok_cli.py`、`src/lead_adapter/claude_code.py`（fail-closed stub 纪律）、`src/lead_adapter/schema.py`

**硬约束**：不碰用户办公机/本机；不做工人池 / Win 真机 / 部署。

你是实现 agent。改码、加测、文档、提交并 `git push origin main`。工坊只派收。

## 必须

1. **新适配 kind**（建议 `deepseek_harness` / `deepseek` 别名），工厂 `COLLAB_LEAD_ADAPTER` 可选项；更新 `get_lead_adapter` 与 `docs/lead-adapter.md`。
2. **严格**走 `build_lead_request` / `validate_lead_decision`（及 `pin_lead_response_schema` 若走 CLI JSON schema）：
   - 权限：`once|reject|deny_job|demand_safe_path`
   - 验收：`pass|fail`
   - **必须回绑 `application_id`**（建议同时回绑 `context_summary`）
3. 非法 / 超时 / 调用失败 → `call_failed` 或保持待决（`safe_failure`）；**禁止假 once/pass**（对齐 claude stub 纪律）。禁止「去掉工具限制再重试」类降级。
4. **调用面**：优先 `COLLAB_LEAD_BIN` 指向可执行包装（stdin 或文件换 JSON），与 `grok_cli` 同构尽量复用。
   - 若 DeepSeek harness CLI 契约不明：落地适配器 + **示例包装脚本**（如 `bin/run-deepseek-lead.sh` 或 `.py`）+ 文档，并明确标 **「真 harness 未接线验收」**。
   - 不要假装已对接真实 DeepSeek 线上 API；无密钥/真 CLI 时 mock 测即可。
5. **unittest**：模拟决策（合法 once/pass、非法 JSON、超时/spawn 失败、application_id 不匹配 → fail-closed）。Linux 相关 lead 回归（如 `test_p1_completion_lead`）不破。

## 不做

工人池、Win 真机、部署、memslice、改 TeleAgent 工人适配主路径（除非工厂文档一行带过）。

## 交付

- SHA
- 测试命令与结果
- 是否真接过 DeepSeek CLI（通常：否 / 未接线验收）
- 怎么 export 环境变量（`COLLAB_LEAD_ADAPTER`、`COLLAB_LEAD_BIN`、其它若有）

开始前确认基线 `37e7315`。勿提交 `.grok-prompts/`、jobs 脏文件。
