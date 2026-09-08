# 8bbd01c 第二轮复核

日期：2026-09-08。基线 ac1279a，最新 origin/main 为 8bbd01c。

结论：有实质修复，但仍有三个必修问题，暂不建议按“核心已验收，只需 Windows 移植”推进。本轮只评估，未修改上游实现，也未合并进本地 Windows 分支。

## 已修复且验证通过

- 取消/超时会发送 abort；abort HTTP 500 时保持停止待确认，不再直接报告取消成功。
- 状态接口 HTTP 500 不再直接视为 idle。
- 持久化/恢复保留任务 charter、产物清单及 force_lead_review；旧版缺失合同记录阻塞恢复。
- 生产权限和验收路径不再替缺少 application_id 的组长决定补绑定。
- 禁止环境代理、默认限制 loopback 基地址已经加入。
- Question 扫描已接入 tick，但仍只是 need_human 标记，没有完整组长回答及回写闭环。

## 必修 1：完成门禁仍可被强制验收绕过（P1）

位置：src/scheduler.py:1432、1492、1503。

status_usable 只排除部分错误对象，没有严格校验状态结构；200 + 字符串也能被视为 idle。成功 finish 的门禁只在非 force_lead_review 分支执行。强制验收时，即使消息接口 500、没有 finish，或 finish=cancelled，只要绑定正确的组长返回 pass 就会 DONE。组长批准不能替代工人执行状态的硬约束。

独立复现（dry_run=False）：

| 输入 | 当前结果 |
|---|---|
| 状态 200/idle，消息接口 500，组长正确绑定 pass | done，ok=true，finish=null |
| 状态 200/idle，assistant finish=cancelled，组长正确绑定 pass | done，ok=true，finish=cancelled |
| 状态 200 + 字符串，非强制验收，assistant finish=stop | done，ok=true |

修复：把有效状态结构、消息读取成功、本轮明确成功 finish、无错误/取消等检查移到所有验收分支之前；确认最新 user 轮次关联，不接受旧轮次成功信息。状态未知保持未知，不允许组长把硬失败改成成功。两种验收模式都要覆盖消息失败、取消、格式错误等测试。

## 必修 2：abort 被接收仍被当作停止确认（P1）

位置：src/scheduler.py:440、effect_cancel 及超时分支。

当前任何 2xx 都令 abort.ok=True，随后直接 CANCELLED/TIMEOUT 并释放槽位，没有进一步核验停止状态。

独立复现：POST /session/probe-session/abort 返回 202 + accepted=true；模拟后端的状态若被查询仍为 busy。实际调用记录只有一次 POST，没有状态确认，结果却 cancel_effected=true、state=cancelled。

修复：区分请求已接收与停止已确认。202、未知响应或仍 busy 均保持停止待确认，继续占用槽位并有界轮询。若某 TeleAgent 版本的 200 响应保证同步停止，需提供受支持接口契约或实机证据并在适配器明确建模；不能统一把所有 2xx 当停止。超时走同一逻辑。会话 idle 也不应被描述为已证明所有系统子进程退出。

## 必修 3：重定向检查发生在认证头发送之后（P1）

位置：src/teleagent_adapter/linux_local_v1.py:161。

opener.open 使用默认 HTTPRedirectHandler，已自动跟随重定向并发送请求，之后才检查 resp.geturl()。因此最终抛出 AdapterError 并不能阻止认证头外发。

独立复现：仅用本机回环地址和虚构凭据，从 127.0.0.1 重定向到不在当前允许列表中的 127.0.0.2。目标服务实际收到 Authorization 与 X-SA-Signature，之后调用端才抛 AdapterError。没有使用真实凭据，也没有外网请求。

修复：本地控制 API 最简单是彻底禁止自动重定向；使用自定义 HTTPRedirectHandler 在重定向请求发出前拒绝。若必须支持则需先校验严格 origin 并重新签名，不能先发后验。测试应验证目标端收到零请求/零凭据，不能仅断言调用端最终报错。

## 本轮测试结果

- 新增 test_astra_p1_fixes：12 项全通过。
- 上游修改后的 review/reproduce_ac1279a.py：全部通过；它覆盖四项旧缺陷的基本修复场景，但未覆盖上述边界。
- 七组测试合计 85 项：80 通过、3 个 /workspace 路径错误、2 个 Windows blocked 实机跳过。路径错误与前轮一致，属于迁移事项。
- 本轮独立脚本 review_remaining.py：五个边界探针全部复现上述问题。使用临时目录、假工人传输和仅回环 HTTP；未操作真实 TeleAgent，未安装软件。

复跑：在本审查副本运行 `python review_remaining.py`。这是缺陷复现脚本：断言当前缺陷存在；修复后需要将预期改为安全结果，不能把该脚本退出 0 解释成验收通过。

## 未解决但已明确的后续范围

- Windows 认证适配器仍 blocked；当前对话组长可沿 inprocess 文件协议接入，独立 Codex/Claude CLI 仍是 stub。
- 本提交没有补交至少一次真实权限申请→组长批准→工人继续的证据；此前 approved=0 不能替代这项验证。
- 会话 ask 策略的设置/确认、Question 回答回写闭环仍需接通。

建议给 Grok：先修这三个明确问题并补相应边界测试，保留已修复四项的回归；随后补真实审批链路证据，再交给 Windows 迁移。避免扩大重构范围。
