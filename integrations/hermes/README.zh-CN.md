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
