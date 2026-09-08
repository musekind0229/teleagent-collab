# 71efc43 第三轮复核

日期：2026-09-08。对照 8bbd01c，只读取独立审查副本，未修改上游实现、未合并本地 Windows 分支。

## 结论

上轮列出的具体复现用例均已通过，重定向防护已修好。取消确认与完成门禁仍各有一个未闭合的边界；两项都是此前要求的“状态必须有效、本轮必须完成”的延续，不需要扩大重构。

## 已通过

- 强制组长验收前检查消息接口成功、成功 finish；消息 500 不再 DONE，finish=cancelled 判失败。
- 状态 200 + 字符串不再被当作有效状态。
- abort 202 + 持续 busy 会保持 cancel_requested，轮询五次后仍不释放槽位。
- 重定向在发送第二次请求之前拒绝；仅回环、虚构凭据的测试中目标收到零请求。
- 四项旧修复回归脚本通过，合同恢复、无绑定决定拒绝仍有效。

## 剩余 P1-1：停止确认把错误/未知字典当成 idle

位置：src/scheduler.py:421–441；src/glue.py:227 的 session_busy。

_session_stop_confirmed 只要求 HTTP 2xx 且 body 是 dict，然后对 session_busy 取反。该函数对于 error 字典或不认识的状态返回 False。因此“无法判断是否忙”被解释为“确认停止”。完成检查也仍应使用同一个严格状态解析器，避免两套验证标准继续分叉。

独立复现：abort 返回 202；下一次状态返回 200 + {"error":"backend not ready"}，结果 CANCELLED、cancel_effected=true。另一个用例返回 {"s":{"type":"unrecognized-state"}}，同样被确认停止。

建议：引入统一三态解析（busy / idle / unknown），显式拒绝错误对象、未知状态、错误字段类型；仅 idle 能确认停止。空映射/目标缺席是否表示 idle，必须依据本地 API 契约，并验证整个对象确实是合法状态映射。unknown 必须保留停止待确认，不能用 not busy 代替 idle。将上述两种字典分别覆盖到取消与完成检查测试。

## 剩余 P1-2：完成证据仍未绑定最新用户轮次

位置：src/scheduler.py:1556；src/glue.py:248 的 last_assistant。

last_assistant 只倒序找最近一个 assistant，不检查它是否属于最新 user 消息。函数注释称 this-round，但实际未实现轮次绑定。

独立复现：消息顺序为 user(u-old) → assistant(parentID=u-old, finish=stop) → user(u-new)，没有新一轮 assistant。状态接口暂时返回合法空忙碌映射，磁盘只有 OLD 产物；当前任务要求 NEW。非强制验收仍直接 DONE、ok=true。

适用触发：已有会话恢复/继续，或用户在同一会话追加工作、状态暂未反映忙碌时，旧轮次完成标记被复用。即使正常全新会话较少遇到，也不符合恢复后安全验收要求。

建议：记录/恢复本次 dispatch 对应 user message ID（或可靠的 query/turn 标识），只接受对应轮次的 assistant 完成；检测到更新的 user 消息而无其完成回复时不得通过。至少检查最新 user 边界及 parentID，并在强制、非强制两种验收前共用该门禁。不能仅靠文件存在、HTTP 成功或任意旧 stop。

## 验证记录

- test_astra_p1_fixes：18 项全通过。
- review/review_remaining.py 与 review/reproduce_ac1279a.py：当前上游安全预期全部通过。
- 七组回归合计 91 项：86 通过、3 个硬编码 /workspace 路径错误、2 个 Windows blocked 实机跳过。后两类与前轮一致，仍属平台接入缺口。
- 本轮 review_edge_cases.py 独立复现三种输入对应上述两项缺陷，仅假传输与临时文件，不连接真实 TeleAgent。
- 复现用法：在本副本运行 python review_edge_cases.py。该脚本断言缺陷存在，退出 0 表示复现成功，不表示修复通过。

## 交接范围

请 Grok 只补这两个门禁与对应边界测试，保留已通过回归。Windows 鉴权、Question 回答回写、真实权限申请→组长审批→继续执行的证据仍是此前明确的后续接入工作，本提交没有提供新的实机记录。本轮不重新要求扩大框架设计。
