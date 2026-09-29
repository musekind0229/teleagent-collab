# Hermes 集成：teleagent-collab skill

源文件：`integrations/hermes/skills/teleagent-collab/SKILL.md`（agentskills.io / Hermes `SKILL.md` 格式：YAML frontmatter + Markdown 正文）。

## Hermes v0.21.3 从哪读 skill（依据）

- Hermes home = `HERMES_HOME` 环境变量，否则平台默认；Windows 为 `%LOCALAPPDATA%\hermes`
  （`hermes --help`：`--ignore-user-config  Ignore ~/AppData/Local/hermes/config.yaml`；该目录下有 `config.yaml`、`SOUL.md`、`skills\`、`memories\`）。
- skills 目录 = `<hermes home>\skills`（`hermes-agent/hermes_constants.py: get_skills_dir() -> get_hermes_home() / "skills"`）。
- 布局 `skills\<category>\<name>\SKILL.md`（如自带 `skills\software-development\codebase-inspection\SKILL.md`）。
- 加载方式：渐进披露。系统提示里常驻 skill 索引（name + description，`agent/prompt_builder.py: build_skills_system_prompt`），
  模型需要时再 `skill_view(name)` 读全文；也可 `hermes -s teleagent-collab` 预加载或 `/teleagent-collab` 斜杠调用。
  官方文档：`hermes-agent/website/docs/user-guide/features/skills.md`（“All skills live in ~/.hermes/skills/”）。
- 常驻人格 `SOUL.md` 在 home 根；项目级 `AGENTS.md` 从 CWD 注入。本 skill 不改这两者。

## 安装（Windows）

```powershell
$dst = Join-Path $env:LOCALAPPDATA 'hermes\skills\autonomous-ai-agents\teleagent-collab'
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item integrations\hermes\skills\teleagent-collab\SKILL.md $dst -Force
& "$env:LOCALAPPDATA\hermes\bin\hermes.exe" skills list | Select-String teleagent-collab
```

新会话生效。token 不要写进 skill；需要时放进进程环境变量或 `%LOCALAPPDATA%\hermes\.env` 的 `COLLAB_API_TOKEN`。

## 实测（2026-09-29，Win DESKTOP-TBB531F）

- collab-service `--backend antigravity --port 8765`（loopback，无 token），池 `jobs/agy-account-pool.json`。
- 在 `%USERPROFILE%`（非仓库目录，无 AGENTS.md）执行 `hermes chat --query-file <prompt> --oneshot -Q`，
  prompt 只说“帮我让 collab 那边的 worker 写个 hello.txt 文件，内容就是 hello”，未点名脚本。
- Hermes 日志顺序：`skill_view`（本 skill）→ `status __ping__`(404=在线) → `open` → `wait`(~36s) → `report`。
- 结果：`goal_hello_cc92a700` completed，run `agy_a75929f3f2b9`，账号 musekind0003，`hello.txt` 字节 `68 65 6C 6C 6F 0A`。

## 实测：中文往返（2026-09-29，刀E，skill 0.2.3，无代码改动）

- `--backend antigravity`，Hermes 一句中文“请让 collab 那边的 worker 写一个 poem.txt 文件，内容就是“春眠不觉晓，处处闻啼鸟。”……”，未点名脚本。
- Hermes 未加 `--unicode`，默认 ASCII 转义输出下 `open → wait → report` 全程读对中文：
  `goal_poem_2966b015` completed，run `agy_9b6f6f825595`，账号 musekind0003；
  任务描述转述与服务端 `desired_outcome` 逐字一致；`poem.txt` 37 字节 = “春眠不觉晓，处处闻啼鸟。”+`\n`。
- 坑：`-Q` 单次模式下 Hermes 的 `python -c` 被自身危险命令策略拦截，它改用 `wc`/`od` 核对，不影响结果。

## 实测：TeleAgent 原生决策（2026-09-29，刀F，skill 0.2.4）

- 桌面 TeleAgent（:4397）+ `--backend teleagent-windows`，deterministic planner 不自动拍板，worker 交付后控制器发起 `review`，
  服务投影为 `artifact_review` 决策 → `wait` 退出码 4、`code=need_human`、`wait.reason=pending_decisions`。
- 0.2.3 时 `summary` 只有 “TeleAgent review”；0.2.4 起从 worker payload 生成，例如
  `review: artifacts hello2.txt(18B); tools write,read,powershell,read,report_final_files; finish=stop; violations=0`。
- 提交方 `POST /v1/requests/{id}/decisions/{dec}` `{"verdict":"pass"}` 后任务 succeeded、goal completed。
- 坑：TeleAgent 的 `write` 工具会给文本追加 “AI生成” 标识和大量零宽字符（14 字节内容 → 10777 字节）；验收只看文件存在时会放行，审阅时注意字节数。
