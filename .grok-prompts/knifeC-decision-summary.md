你在仓库 teleagent-collab 根目录。只改下列文件，不要 git commit/push，不要读取或打印凭据/token/.env/账号池文件。

## 背景（实测发现）
用 bin/durable-cli.py escalate <goal> --kind over_budget --reason "R" --task-id T 在真实 collab-service 上造出待决策后，
bin/hermes-collab-request.py wait 正确 exit 4，但 wait.decisions[].summary 只是 "escalate_over_budget"：
- durable escalate_to_upper 会把 title 设成 kind（title=="escalate_over_budget"），真正的人类可读原因在决策记录的 `reason` 字段；
- src/framework/app_service.py 的 public_pending_decisions() 没把 `reason` 拷到公开行，所以 status/decisions 接口看不到原因；
- 客户端 _decision_summary 先取 title，于是摘要等于 kind，对人没用。

## 最小修复
1. src/framework/app_service.py public_pending_decisions：公开行增加 "reason": str(row.get("reason") or "")。不改其它字段。
2. bin/hermes-collab-request.py：
   - _decision_brief 输出增加 "reason"（字符串，没有则 ""）。
   - _decision_summary 取值顺序改为：title（仅当非空且不等于 kind）→ details.summary → details.message → details.reason → details.question → row.reason → lead_error.message → title → kind。仍然压成单行、截断 200。
3. 文档 docs/hermes-collab-min-client.zh-CN.md 中 exit 4 的 summary 取值说明同步更新，decisions[] 示例加 reason 字段。
4. SKILL.md（integrations/hermes/skills/teleagent-collab/SKILL.md）中若描述了 summary 取值则同步；version 升到 0.2.2。

## 单测
- src/test_hermes_collab_request.py：
  - title==kind 且有 row.reason → summary 取 reason；
  - title==kind、details.summary 存在 → 取 details.summary；
  - title 是有意义文本（≠kind）→ 仍取 title（原行为）；
  - 只有 kind → kind；
  - decisions[] 带 reason 字段。
  - 现有断言若依赖旧顺序，按新顺序调整。
- 新建 src/test_public_pending_decisions.py（不要依赖 test_app_service.py，它在基线里导入失败）：直接 from framework.app_service import public_pending_decisions，断言 reason 被拷贝、缺省为 ""、其它原有字段不变。运行方式与其它测试相同（cd src && python3 -m unittest test_public_pending_decisions）。如果 import framework.app_service 在当前环境失败，先查明原因（不要改无关代码），改为在测试里做最小 sys.path 处理。

完成后运行：cd src && python3 -m unittest test_hermes_collab_request test_public_pending_decisions -v，全部通过。最后简短列出改动。
