# teleagent-collab ac1279a 复核

日期：2026-09-08。对照 aa6a9b4 后五次提交。通过 git fetch 获取 origin/main，再用 git archive 创建独立审查副本；原本 windows-codex-lead 分支及未提交修改保留，没有合并或推送。本轮未修改上游实现、未派真实工单、未安装软件。

## 结论

方向符合要求，新增适配器、授权模型、验收模块及持久化有价值。但存在核心运行缺陷，不能判为“没有大问题，可以只做平台迁移”。建议先修以下问题，再进行 Windows 接入。

## 必修项

### 1. P1：取消/超时只停止本地跟踪，没有停止工人

`src/scheduler.py:415` 的 effect_cancel 直接标记 CANCELLED、cancel_effected=True，并写 execution stopped，但没有调用 adapter.cancel 或 POST /session/{id}/abort。超时分支（1210 起）也只标记 TIMEOUT。工人可能继续执行安装或写文件，而调度器已释放槽位。

修复：先请求停止本会话，检查响应并继续确认停止状态。失败/状态未知保留停止待确认；不可把请求收到等同于进程实际停止。为真实传输调用与失败情况增加测试。

### 2. P1：并行完成判定仍可能误报成功

`src/scheduler.py:1255` 忽略 /session/status 的 HTTP 状态；错误对象经 session_busy 判断为非忙，只要所有文件存在就 DONE。不读取本轮 assistant finish/error，也没有独立验证非强制组长模式下的内容条件。验收包在 1141 起使用固定 execution_result、空 error，并把权限摘要当作工具记录，不能反映真实工人执行错误。

复现：模拟 GET /session/status 返回 500 + error，已有产物内容为 wrong content，force_lead_review=False，结果仍 ok=True。

修复：要求成功且格式有效的状态响应、本任务最新轮次明确完成且无错误、无未处理申请/问题；验收依据实际消息及工具证据。接口失败保持未知/失败，不能当 idle。

### 3. P1：恢复丢失原任务合同

`src/scheduler.py:353` 持久化未保存完整 charter、instruction、expected_artifacts、force_lead_review 等。`restore_from_store`（452 起，尤其 474）用 goal=(restored)、空 must/must_not、空产物列表重建，强制验收标志回到 False。

影响：恢复后审批缺少原始约束，产物无法正常验收；已有测试只检查 session/token/handled IDs，没有检查恢复后的审批与交付。

修复：持久化和恢复完整任务合同、授权范围、验收与预算；缺失或版本不兼容应阻塞，不得以空约束继续。补“重启后新权限申请→审批→交付”的集成测试。另需覆盖创建会话或发 prompt 时崩溃的窗口，而不仅正常落盘后恢复。

### 4. P1：生产验收路径替组长补绑定字段

`src/scheduler.py:1180` 附近将 permission once 转成 review pass，并为缺失 application_id 的结果补当前请求 ID/context。这段逻辑并未限定 dry_run，破坏了结构化协议应拒绝无绑定输出的要求。

复现：dry_run=False，组长只返回 verdict=pass/reason（不含 ID），最终 DONE。

修复：生产输出必须原样经过严格校验；缺 ID、错 ID、错上下文应拒绝。测试假组长也应返回合规输出，不要让测试兼容代码进入生产路径。

## 接线与验证缺口

- Question API 有独立模块，但 src/scheduler.py 和 src/glue.py 的主循环没有接入问题扫描及回答回写。实机脚本仅把问题标记 need_human；不能据此宣称当前对话组长可接住全流程问题。
- 创建会话未声明并核验会话级 ask 权限策略（scheduler 537、glue 562 附近），因此 glue 只能处理实际弹出的申请，不能保证授权范围内操作都经过预定审批门。需要在受支持 API 范围内核实策略，不能依赖用户当前全局配置。
- docs/p3-test-results.md 的实机 PASS 明确注明 approved=0、未触发权限申请。它验证了派工与 Grok 验收，但没有验证“权限申请→外部组长批准→工人继续”。Question 实测只有空列表读取，未验证真实回答与恢复执行。应增加必定触发且统计至少一次批准的独立会话试验，不降低全局权限来凑通过。
- WindowsBlockedAdapter 仍明确 blocked；这是如实保留的迁移阻塞，不算本轮意外回归。Codex/Claude CLI 仍是 stub；当前对话模式有 inprocess 文件协议，可作为迁移基础。
- Linux HTTP 适配器仍直接使用 urllib.request.urlopen；未限制 base_url 为 loopback、未禁环境代理/重定向。Windows 迁移时应保留本地旧分支已有的连接加固，避免认证头意外流向其他地址。

## 本机验证

在 Windows / Python 3.12.14 对未修改的上游运行其文档列出的六组测试：

| 模块 | 测试数 | 结果 |
|---|---:|---|
| test_p0_security | 8 | 6 通过，2 错误：硬编码 /workspace 写入路径 |
| test_p1_completion_lead | 14 | 全通过 |
| test_p2_auth_recovery | 20 | 19 通过，1 实机跳过（Windows blocked） |
| test_p3_lead_question_install | 13 | 11 通过，1 错误（默认 /workspace 路径），1 实机跳过 |
| teleagent_adapter.test_adapter_contract | 13 | 全通过，模拟契约 |
| test_scheduler | 5 | 全通过 |

总计 73 项：68 通过、3 路径错误、2 实机跳过。路径错误属于待迁移事项，不拿它代替核心逻辑问题的证据。

新增审查脚本 review_repro.py（未改变上游实现），使用临时目录和注入的假传输，dry_run=False，四个探针均复现上述缺陷：

```text
cancel_without_abort: cancelled / cancel_effected=true / transport_calls=[]
status_http_500_accepted: done / ok=true
restore_loses_contract: must_not=[] / artifacts=[] / force_lead_review=false
unbound_lead_accepted: done / ok=true
```

复跑：Python 3.12+ 执行本目录 review_repro.py。没有访问真实 TeleAgent、没有系统安装或外部消息副作用。

## 后续交接

先修四个必修项并补真实权限链路证据；再接通 Windows 认证适配器，保留现有本地实现的防重定向、DPAPI、完整上下文绑定等措施。最终先用小文件工单验证派工/审批/问题/取消/恢复，再试 RustDesk 分阶段安装。可以换模型接续，但任务应包含上游缺陷修复，不能仅标为平台移植。

提交内复现脚本：review/reproduce_ac1279a.py。用法：python review/reproduce_ac1279a.py <ac1279a 审查副本的 src 绝对路径>。原始审查副本保留在 G:/codex/teleagent-review-ac1279a。
