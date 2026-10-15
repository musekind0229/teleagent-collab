# 调用方命令验收（隔离候选）

AGY 后端的调用方可设置：

```json
{"acceptance":{"artifacts":["delivery.txt"],"command":{"argv":["python","-m","unittest","test_example"],"cwd":".","timeout_sec":10,"max_output_bytes":65536}}}
```

- `command` 来自已验证的目标契约，不采纳工人结果内同名证据。仅 AGY 后端接受该字段；省略时保持旧流程。
- 命令在调度器分配的**每个任务工作区**执行，不是目标聚合后执行。多任务需调用方确保同一命令对每个任务均适用；本切片验证单任务链路。
- `argv` 无隐式 shell；`cwd` 必须是工作区内真实目录，拒绝绝对路径、上溯和途经链接／junction。命令本身可以运行任意授权程序，**这些检查不是系统沙箱，也不防恶意源码访问本机文件或网络**。只能接收可信授权调用方命令。
- 超时默认 10 秒，范围 0.05–120 秒；强制 wall 预算下取剩余任务时间较小值。Windows 子进程挂起创建、加入专用 Job 再恢复；结束或超时收束后代。POSIX 分支使用新会话，尚未在本机验证。
- stdout/stderr 分别默认保留前 65536 字节（可设 16–1048576）；超限继续排空，记录总字节和截断标志，不把截断内容称为完整输出。保留字节文件及 SHA256，解码文本送审。
- argv、实际 cwd、退出码、超时、捕获完成情况、截断信息、原始输出路径与哈希、goal/task/run 绑定及回执落在工作区外 `.collab-review/commands/`，进入任务结果和组长审查输入。
- 命令非零、超时、启动或捕获失败不能被工人自述成功或组长 pass 覆盖；走原有有界返工，额度耗尽失败。未配置命令不相信工人伪造的 `acceptance_execution`。
- 公开失败来源为 `acceptance`，阶段为 `acceptance/testing`；`failure_kind` 区分 `command_nonzero`、`command_timeout`、`command_execution_error`，并公开实际 `returncode` 与布尔 `timed_out`。按结构化回执分类，不从错误文字中的 `timed_out=False` 推断超时。验收超时不冒充工人超时；真正工人 wall 超时仍保留 `worker_timeout`。`independent_checks` 对应真实命令通过／失败，不再显示未运行。
- 子进程只继承最小运行环境，不直接传入服务／供应商 token；不是凭据或文件系统隔离保证。调用方自身命令输出仍须避免泄密。

验证使用合成组长／工人 fixture 与真实 OS 命令；不是实际 Grok＋AGY 演示。真实审查及小编辑交付恢复由秘书抽验后另组织；不能据此宣称 AGY 实施能力恢复。
