# 组长适配接口（条3）

外部组长经**结构化协议**读待决、提交决定。不再把 Grok CLI 写死在编排核心；Grok CLI 只是一种 `LeadAdapter` 实现。Codex CLI（`codex_cli`）是可选的只读沙箱实现，**已接线，真机尚未验收**（老板还没登录 Codex）。

## 布局

| 文件 | 作用 |
| --- | --- |
| `src/lead_adapter/base.py` | `LeadAdapter` Protocol / ABC |
| `src/lead_adapter/schema.py` | `build_lead_request` / `validate_lead_decision` / `pin_lead_response_schema` / JSON schema |
| `src/lead_adapter/grok_cli.py` | Grok CLI 后端（保留 `--disallowed-tools`，**禁止**失败后去掉限制再重试） |
| `src/lead_adapter/inprocess.py` | 当前 Codex/对话当组长：目录协议 `pending/` + `decisions/`，或 `decision_fn` 进程内回调 |
| `src/lead_adapter/claude_code.py` | **待办 stub**：`call_failed`，禁止假 PASS |
| `src/lead_adapter/codex_cli.py` | Codex CLI：`codex exec` 只读沙箱，**已接线 / 真机未验收**。别名 `codex` 仍→inprocess（并打警告） |
| `src/lead_adapter/deepseek_harness.py` | DeepSeek harness：**JSON-in/JSON-out 包装**；包装内调 `dsh --profile headless` |
| 工厂 | `get_lead_adapter(kind=...)`；`COLLAB_LEAD_ADAPTER=grok_cli\|inprocess\|claude_code\|codex_cli\|deepseek_harness`（`deepseek`→harness；`codex` **不是** CLI，仍是 inprocess） |
| `bin/run-live-grok-lead.py` | 真机：创建→权限→GrokCLI→继续→验收。默认 lead bin：`COLLAB_LEAD_BIN`（存在时）→ PATH `grok`/`grok.exe` → `~/.grok/bin/grok.exe`（Win）或 `grok`（posix）→ `/workspace/run-grok.sh`（仅当文件存在）。Win 默认 worker `http://127.0.0.1:4397`，非 Win `:4399` |
| `bin/run-deepseek-lead.py` | DeepSeek 包装（stdin / `--request-file`）→ `dsh --profile headless`；无 bin/key 时 fail-closed |

## 请求（每次决策自包含）

每条申请绑定：

- `application_id`
- `context_summary`
- `task_goal`
- `authorized_scope`（must / 授权范围）
- `prohibitions`（must_not）
- `acceptance_criteria`
- `current_application`（当前权限包或验收包）

**不假定**新进程记得前文。

## 响应

严格 JSON，且必须回绑 `application_id`（建议同时回绑 `context_summary`）。

- 权限：`decision` ∈ `once|reject|deny_job|demand_safe_path` + `reason`
- 验收：`verdict` ∈ `pass|fail` + `reason`

非法输出 / 超时 / 调用失败 → `LeadDecisionError` → **保持待决**（timeout/call_failed）或 **安全拒绝**（非法 JSON）。**禁止**「去掉 disallowed-tools 再重试」的降级（已从 `glue.call_lead` 删除）。

## 当前 Codex 对话当组长

```bash
export COLLAB_LEAD_ADAPTER=inprocess
export COLLAB_LEAD_EXCHANGE=/path/to/exchange
```

1. 编排写入 `pending/<application_id>.json`（含完整 request + prompt）
2. 对话侧读 pending，按 schema 写 `decisions/<application_id>.json`
3. 或在同进程注入 `InProcessLeadAdapter(decision_fn=...)`

不强制每次另起 Codex/Grok 进程。

## 与 glue / scheduler

- `glue.call_lead_request(request, schema=..., cwd=...)` — 首选
- `glue.call_lead(prompt, schema, cwd)` — 兼容旧调用，内部仍走 adapter
- `scheduler` 权限与 `force_lead_review` 验收均走同一协议

## 测例

`src/test_p1_completion_lead.py`（simulated）：绑定校验、inprocess 文件协议、无 disallowed-tools 降级、并行 `force_lead_review`。

`src/test_deepseek_harness_lead.py`（simulated）：`deepseek_harness` 工厂别名、合法 once/pass、非法 JSON / 超时 / spawn 失败 / `application_id` 不匹配 fail-closed；包装 mock `dsh --profile headless` 成功/失败路径。真 dsh+key 的 smoke 默认 skip。


## Windows 控制层（本机 Grok CLI）

工人层 TeleAgent 在 DESKTOP-TBB531F 的 HTTP 端口以发现为准（**4399→4397→4398**；曾见 **:4397**，重启后曾见 **:4398**），凭据仍走 PEB。控制层默认接本机 Grok CLI（`%USERPROFILE%\.grok\bin\grok.exe`），**不要**再把 Linux 的 `/workspace/run-grok.sh` 或 `:4399` 写死成 Win 默认，也不要把默认 `TELEAGENT_BASE_URL` 写死成 4398。

```
set COLLAB_LEAD_ADAPTER=grok_cli
set COLLAB_LEAD_BIN=%USERPROFILE%\.grok\bin\grok.exe
set TELEAGENT_BASE_URL=http://127.0.0.1:4397
python bin/run-live-grok-lead.py
```

未设 `COLLAB_LEAD_BIN` 时，`resolve_lead_bin()`（`src/lead_adapter/grok_cli.py`）按上面的顺序找 `grok.exe`。未设 `TELEAGENT_BASE_URL` 时，工人适配器按 **4399→4397→4398** 发现；lead 脚本示例仍可用 `:4397`。其它平台默认 `:4399`。失败后**禁止**去掉 `--disallowed-tools` 再试。

## Live Grok echo reliability

Live Grok CLI must echo `application_id` and `context_summary` **exactly** (byte-for-byte) from the request. `pin_lead_response_schema` sets JSON Schema `const` on both fields (and keeps `context_summary` in `required`) before `--json-schema` spawn so the model cannot rewrite them.

Do not loosen `validate_lead_decision`: empty `application_id` is `application_id_mismatch`; rewritten `context_summary` (newlines→spaces, added commentary, whitespace normalize) is `context_summary_mismatch`. Production glue/scheduler must not stitch these fields — dry_run-only stitch stays dry_run-only.

## Codex CLI（`codex_cli`）

适配器 `codex_cli`（工厂别名 `codex-cli`）一次 spawn `codex exec`，走 `build_lead_request` / `pin_lead_response_schema` / `codex_output_schema` / `validate_lead_decision`。**禁止**假 `once`/`pass`。失败后**禁止**第二次尝试：不改沙箱、不丢掉 `--output-schema`、不放宽 `approval_policy`。不读 `~/.codex/auth.json` 或其它 Codex 凭据。真机回归还没做。

### 命令行

提示走 stdin（参数 `-`）。`<tmp>` 为 `tempfile.mkdtemp(prefix="collab-codex-lead-")`，`finally` 里 `shutil.rmtree(..., ignore_errors=True)`。`<cwd>` 为 `decide(..., cwd=)` 且该目录存在，否则用 `<tmp>`（只读沙箱，codex 不能改它）。

```
<codex> exec --sandbox read-only -c approval_policy=never --ephemeral --skip-git-repo-check --color never -C <cwd> --output-schema <tmp>/schema.json -o <tmp>/last.json -
```

不传 `--json`（那是 JSONL 事件流，不是决定）。`exec` 没有 `--ask-for-approval`，审批策略用 `-c approval_policy=never`（裸 `never` 不是合法 TOML，codex 按 help 说明当字面字符串用；argv 不带引号，`.cmd` 路径无需例外）。

### 权限（只读，无绕过，无重试）

- `--sandbox read-only`。禁止 `-s workspace-write`、`-s danger-full-access`，以及对应的 `--sandbox` 长形式。
- `--ephemeral`（不写 session 文件）、`--skip-git-repo-check`、`--color never`。
- **argv 里永不出现**：`--dangerously-bypass-approvals-and-sandbox`、`--full-auto`、`--approve-for-me`、`--dangerously-bypass-hook-trust`、`--add-dir`、`--worktree`。命中则 `call_failed`，**不 spawn**。
- 只 spawn 一次。

### 二进制（懒解析）

`CodexCliLeadAdapter()` 和 `get_lead_adapter("codex_cli")` 在没有二进制时**不抛**。`decide` / `doctor_hint` 才解析。顺序（显式路径必须是已存在的文件，否则继续往后找）：

1. `COLLAB_CODEX_LEAD_BIN` — Codex 专用，避免和 grok 的 `COLLAB_LEAD_BIN` 撞车
2. `COLLAB_LEAD_BIN`
3. PATH `codex`；Windows 再试 `codex.cmd`、`codex.exe`

都没有则 `CodexBinNotFound`，`decide` 返回 `call_failed`（不创建临时目录）。构造函数 `bin_path=` 可覆盖。

DESKTOP-TBB531F 示例（codex-cli 0.155.0）：

```
set COLLAB_LEAD_ADAPTER=codex_cli
set COLLAB_CODEX_LEAD_BIN=C:\Users\Admin\.local\share\TeleAgent\runtimes\node\codex.cmd
```

### Windows `.cmd` / `.bat`

不要把 `.cmd` 直接交给 `CreateProcess`（BatBadBut 一类转义问题）。解析结果以 `.cmd`/`.bat` 结尾**并且**当前是 Windows 时：

- 任一 argv 含 `" % ! ^ & | < >` 或 CR/LF → `call_failed`，不 spawn，错误里写明是哪个参数。固定参数里没有元字符，不设例外。
- 否则 `Popen` **一条命令行字符串**（`shell=False`）：`%COMSPEC% /d /s /c "<list2cmdline(argv)>"`，缺省 `cmd.exe`。`/s` 只剥掉最外一层引号。提示仍走 stdin。
- 非批处理二进制（以及非 Windows）直接 spawn argv 列表。
- 超时：Windows `CREATE_NEW_PROCESS_GROUP`，然后 `taskkill /T /F /PID`（`cmd /c` 会留下 node 子进程占着管道）；POSIX `start_new_session` + `killpg(SIGKILL)`。然后再 `communicate(timeout=5)`，避免一直挂起。

### 输出与失败

`--output-schema` 要 Codex/OpenAI 严格结构化输出。`codex_output_schema(pinned)` 深拷贝 `pin_lead_response_schema` 的结果：每个 `const: X` 改成 `enum: [X]`（保留 type），`required` = 全部属性名，`additionalProperties: false`。`safe_path_hint` 因此变成必填字符串（模型可以填 `""`）。解析后若 `safe_path_hint` 是 `""` 就丢掉。

只解析 `<tmp>/last.json`（`-o`）。文件缺失或空白才回退 **stdout** 的 strip 文本。stderr 是进度日志，**绝不**当决定解析。`json.loads(text.strip())` 必须得到对象：不剥代码块、不正则抠第一个 `{...}`。其它都是非法输出。非 0 退出即使 last.json 里是合法 JSON 也不采信。

错误文本进信封前会打码：`sk-...`、`Bearer ...`、连续 ≥32 的十六进制或 base64。失败信封没有 `decision` / `verdict`。

| 情况 | `decide` 返回 | 上游 `validate_lead_decision` |
| --- | --- | --- |
| 合法 JSON 对象 | `(text[:3000], obj)`。若请求里的 `application_id` 非空且与对象字符串不相等 → `("ILLEGAL_OUTPUT", {_lead_status: error, lead_error_code: application_id_mismatch})` | 再绑 `context_summary`；id 不匹配的信封是 `call_failed` |
| 非法输出（散文、散文里的 JSON、代码块、数组、空） | `("ILLEGAL_OUTPUT", {_lead_status: error, lead_error_code: illegal_output})`。`raw` **不是**模型原文（否则上游可能从散文里抠 JSON）。错误里最多约 300 字模型文本 | `call_failed`（待决 / 安全拒绝） |
| 超时 | `safe_failure("timeout", "codex_cli timeout after Ns")` | code `timeout` |
| 非 0 退出 | `("CALL_FAILED", {_lead_status: call_failed, error: stderr 尾部 ≤1500 或 exit=N, returncode})` | code `call_failed` |
| 找不到二进制 / spawn `OSError` | `safe_failure("call_failed", ...)` | code `call_failed` |

`doctor_hint()`：`status=wired_unverified`，`name=codex_cli`，`fake_pass=false`，`bin_path` 为解析到的路径或 `null`，`bin_error` 为原因或 `null`，`sandbox=read-only`，`approval_policy=never`，`live_verified=false`。二进制缺失也不抛。

### `codex` 别名仍是对话当组长

`get_lead_adapter("codex")` 和 `COLLAB_LEAD_ADAPTER=codex` **仍然**返回 `InProcessLeadAdapter`，并经 logger `lead_adapter` 打 WARNING：`lead adapter alias "codex" means inprocess (dialogue-as-lead), not the Codex CLI; 要用 Codex CLI 请设 codex_cli`。要用 Codex CLI 请设 `codex_cli`。`inprocess` 不打这条警告。

测例：`src/test_codex_cli_lead.py`（假二进制 / mock，不跑真 `codex`、不登录、不读凭据）。

## Claude / Codex 待办

- `claude_code` 仍是骨架：返回 `_lead_status=call_failed`，**绝不**自动 `once`/`pass`。
- `codex_cli` 已按上面的只读命令接线，**live 回归等登录之后再做**。模拟测例不是真机验收。
- 对话当组长继续用 `inprocess`（`COLLAB_LEAD_ADAPTER=inprocess`）。历史别名 `codex` 仍是 inprocess，并警告应改用 `codex_cli`。
- 真机回归集：Grok 见 `bin/run-live-grok-lead.py`；Codex CLI 待登录；Claude 仍是 stub。

## DeepSeek harness（`dsh --profile headless`）

适配器 `deepseek_harness`（别名 `deepseek`）与 `grok_cli` 同构：一次 spawn、走 `build_lead_request` / `pin_lead_response_schema` / `validate_lead_decision`，非法 JSON / 超时 / spawn 失败 → `call_failed` 或保持待决。**禁止**假 `once`/`pass`，**禁止**「去掉工具限制再重试」。**不要把 `dsh` 当 TeleAgent 工人。**

调用面仍是 `COLLAB_LEAD_BIN` 指向的可执行包装：stdin（默认）或 `--request-file` 换 JSON 信封（含完整 request + 钉死的 schema + prompt）。包装在有 harness bin 与 key 时调用公网 `dsh` headless，stdout 必须是回绑 `application_id`（建议同时回绑 `context_summary`）的 **collab-lead-v1 决策 JSON**。

`dsh` 默认 stdout = 终答文本（exit 0/1）。`--json` 是事件流，**不是** lead schema；包装不传 `--json`。从 stdout 取**最后一个**合法 JSON 对象，再按 schema / `validate_lead_decision` 检查（至少 `application_id` 字节级回绑、`decision`/`verdict` 合法集）。validate 失败或 dsh 非 0 → 包装 **exit 2**，stdout **禁止**假 `once`/`pass`。

Lead 提示禁止 bash / 写文件 / 任何改仓库的工具，只出 JSON。包装把 dsh 的 cwd 放到临时目录，避免工具落到本仓或 job workdir。

```bash
export COLLAB_LEAD_ADAPTER=deepseek_harness
export COLLAB_LEAD_BIN=.../bin/run-deepseek-lead.py
export COLLAB_DEEPSEEK_HARNESS_BIN=dsh   # 或 npx 包装
export DEEPSEEK_API_KEY=...
```

安装：`npx @deepseek-ai/dsh` 或全局/源码。headless：`dsh --profile headless "task"`（省略任务或任务为 `-` 时从 stdin 读；包装走 stdin `-`）。

无 `COLLAB_DEEPSEEK_HARNESS_BIN` 且 PATH 无 `dsh`、或无 `DEEPSEEK_API_KEY` 时 **fail-closed**（exit 2），stderr 写清缺的是 bin 还是 key。不要 POST `api.deepseek.com`。若环境里已有 grok 的 `COLLAB_LEAD_BIN`，改成此包装，或设 `COLLAB_DEEPSEEK_LEAD_BIN`（优先于 `COLLAB_LEAD_BIN`）。`COLLAB_DEEPSEEK_IO=stdin|file` 仍可用。

测例：`src/test_deepseek_harness_lead.py`（mock subprocess：合法 once/pass、dsh 非 0、非法 JSON、`application_id` 不匹配）。不把 mock 当真实 DeepSeek 验收。本机同时有 `dsh` 与 `DEEPSEEK_API_KEY` 时才会跑 optional live smoke（默认 skip）。
