你在仓库 teleagent-collab 根目录。只改下列文件，不要改其它文件，不要 git commit/push，不要读取或打印任何凭据/token/账号池文件。

## 需求：bin/hermes-collab-request.py 的 wait 遇到人工决策立即退出

背景：collab-service 的 GET /v1/requests/{id}（src/framework/app_service.py 的 CollabApplication.status）返回字段：
state（goal 状态：queued/running/blocked/completed/failed/cancel_requested/cancelled）、
pending_decisions（list，每行 decision_id/request_id/kind/title/task_id/run_id/status/backend_kind/backend_request_id/details[/lead_error]）、
pending_decision_count（int）、awaiting_decision（bool）、tasks（list，task.status 可能为 "awaiting_decision"）、need_human、failure_reason。
当前 cmd_wait 只认终态，遇待决策会一直轮询到超时（退出码 3）。

改动（bin/hermes-collab-request.py）：
1. 新增常量 EXIT_NEED_HUMAN = 4（并把 0/1/2/3 也定义成常量 EXIT_OK/EXIT_ERROR/EXIT_FAILED/EXIT_TIMEOUT，行为不变）。更新模块 docstring 说明退出码。
2. 新增纯函数 need_human_view(status: dict) -> dict | None：
   - 若 status 的 state 是终态（completed/failed/cancelled）返回 None（终态优先，原逻辑不变）。
   - 触发条件（任一）：pending_decisions 非空 → reason="pending_decisions"；否则 awaiting_decision 为真或 pending_decision_count>0 → reason="awaiting_decision"；否则 tasks 里有 status=="awaiting_decision" → reason="task_awaiting_decision"。都不满足返回 None。
   - 返回 {"reason": ..., "state": ..., "decision_ids": [...], "decisions": [ {decision_id, kind, title, task_id, status, summary} ...]}。
     summary：依次取 row.title、details.summary、details.message、details.reason、details.question、lead_error.message 中第一个非空字符串，压成单行，截断到 200 字符；都没有则用 kind。
     task_awaiting_decision 且无决策行时，decisions 为空列表，另给 "task_ids": [那些 task 的 task_id]。
3. cmd_wait：每次拿到 status 后，终态判断之后、超时判断之前调用 need_human_view；命中时：
   - 若 decisions 为空且 reason!="pending_decisions"，尽力 GET /v1/requests/{id}/decisions 一次（失败忽略，ClientError 吞掉）拿 pending_decisions 补全 decisions/decision_ids。
   - 在 last 上加：last["code"]="need_human"；last["need_human"]=True；last["wait"]={"terminal": False, "need_human": True, "timed_out": False, "reason": ..., "state": ..., "decision_ids": [...], "decisions": [...]}（有 task_ids 也放进 wait）。
   - raise ClientError(last, exit_code=EXIT_NEED_HUMAN)。
   - 保留原 status 其余字段（ok 字段保持服务端原值）。
4. 正常 completed → 0、failed/cancelled → 2、超时 → 3 行为保持不变。

## 单测（src/test_hermes_collab_request.py，追加到现有 unittest 类或新类，沿用文件里 mock urlopen 的写法）
- pending_decisions 非空：wait 第一次轮询就返回 exit 4，stdout JSON code=need_human、wait.reason=pending_decisions、decision_ids 正确、summary 正确，并断言只轮询了 1 次（不 sleep 到超时；patch time.sleep）。
- awaiting_decision=True 但 pending_decisions=[]：命中 reason=awaiting_decision，并验证会补打 /decisions 拿到 decision id；再测 /decisions 返回 HTTP 500 时仍 exit 4、decisions=[]。
- tasks[].status=="awaiting_decision"：reason=task_awaiting_decision，task_ids 正确。
- 先 running（无决策）再 completed：exit 0，输出无 code=need_human。
- completed 但 pending_decisions 非空：终态优先 exit 0。
- failed → 2、timeout → 3 回归各一条（若已有则不用重复）。
- need_human_view 纯函数的 summary 截断/回退逻辑一条。
用 main([...]) 调用并捕获 stdout 断言退出码。

## 文档（中文，保持现有风格，简洁）
- docs/hermes-collab-min-client.zh-CN.md：wait 一节写明退出码表 0/1/2/3/4，以及 exit 4 的 JSON 形状示例（code、need_human、wait.reason 三种取值、decision_ids、decisions[].summary）。
- integrations/hermes/skills/teleagent-collab/SKILL.md：Procedure 第 3 步退出码加 4 = 需要人拍板（立即返回，不再等超时）；“停下问用户”一节写明 exit 4 时把 request_id、wait.reason、decision_ids、每条 summary 转述给用户，禁止自己批准/拒绝决策；version 升到 0.2.0。
- 保持 SKILL.md frontmatter 合法 YAML。

完成后运行：cd src && python -m unittest test_hermes_collab_request -v ，确保全部通过。最后简短列出改了什么。
