# 86f9dec 第四轮复核

日期：2026-09-08。独立副本审查，未修改上游业务代码、未合并本地分支。

## 结论

上轮具体问题和已有回归已通过；停止检查已统一为 busy/idle/unknown，正常带 ID 的消息也不再复用旧轮次。但轮次证明缺失时的生产回退仍会误报成功，需要去掉。此次只要求收紧这一处已有门禁，不扩大重构范围。

## 已验证通过

- error 字典与未知字符串状态不再确认停止，也不再直接通过验收。
- 带 ID 的最新 user 尚未得到对应 assistant 时，不再拿上轮 stop 通过；强制和非强制验收均覆盖。
- 原有取消轮询、强制验收失败门禁、重定向阻断、任务合同恢复、决定 ID 校验回归通过。
- test_astra_p1_fixes 共 26 项通过；三组 review 脚本的安全预期均通过。
- 七组测试共 99 项：94 通过、3 个已知硬编码 /workspace 路径错误、2 个 Windows blocked 实机跳过。没有宣称 Windows 或真实权限闭环已验收。

## 剩余必修：不能证明轮次时仍降级接受 last_assistant（P1）

位置：src/glue.py:370–396，尤其 return last_assistant(messages)。

this_round_assistant 在 latest_uid 和 dispatch_uid 都为空时退回 last_assistant，注释说明为兼容 legacy single-assistant probes。但它被生产 scheduler 和串行 glue 直接调用，没有 dry_run 限制。派工之后捕获 user ID 允许失败，且旧持久化记录也可能没有 ID，所以不能以“正常情况下有 ID”作为安全保证。

本轮 dry_run=False 独立复现：

| 消息列表（任务没有已保存的 dispatch_user_message_id） | 当前结果 |
|---|---|
| 仅有旧 assistant，finish=stop | done / ok=true |
| 旧 assistant finish=stop 后面有一个 user，但 user 缺 id | done / ok=true |

第二种输入尤其说明：即使已看见更新的 user，只要其标识缺失，仍会回用旧成功记录。缺字段或不完整响应应阻塞验收，而不是被解释为旧协议兼容成功。

最小修复：

1. 生产 this_round_assistant 缺失可证明的 user/dispatch 绑定时返回 None，让任务保持待确认。
2. 不要为了旧测试继续保留生产后门；模拟消息补上真实协议形状的 user id 和 assistant parentID。若确有另一种受支持消息协议，单独适配并提供等价轮次证明。
3. 将缺 user 行、最新 user 缺 ID、派工捕获 ID 失败后恢复三类用例加到强制和非强制验收测试；全部要求不得 DONE。正常配对完成仍应通过。

复现脚本：本目录 review_binding_fallback.py，运行 python review_binding_fallback.py。仅使用注入的假传输和临时文件，不访问 TeleAgent、不做系统安装。脚本断言缺陷存在，退出 0 表示复现成功。

## 后续

此处修复后，再以修复提交作为 Windows 迁移候选基线。此前明确的 Windows 鉴权、Question 回答回写、真实权限申请→组长批准→工人继续的实机证据仍待接通；这些没有在本轮获得验证，不能因单测通过而宣称全部可用。
