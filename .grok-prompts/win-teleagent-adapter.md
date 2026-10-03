# 刀：Windows TeleAgent 工人适配

仓库：musekind0229/teleagent-collab  
基线 HEAD：`a58859c`（origin/main）  
参考：
- `/workspace/teleagent/probe-sandbox/handoff/WINDOWS-PORT-BRIEF.md`
- `docs/teleagent-worker-contract.md`（现写仅 Linux — 本刀扩展/注明 Win）
- `src/teleagent_adapter/windows_blocked.py`（要替换或收窄，**不能**继续当 win32 唯一路径）
- Linux 对照：`src/teleagent_adapter/linux_local_v1.py`、`doctor.py`、`base.py`

**硬约束**：不碰用户办公机/已登记本机；不做整机 Win VM；无真机时用模拟/契约测，并明确标「Windows 真机未验收」。

你是实现 agent。改码、加测、提交并 `git push origin main`。工坊只派收。

## 目标

1. **真 Windows 适配器实现**（发现端口、鉴权、session、permission 往返），与 Linux 同构或可适配。
   - `get_adapter(platform="win32"|windows)` **不再**只返回永远 `WindowsBlockedAdapter`。
   - `WindowsBlockedAdapter` 可保留为显式 blocked/降级路径，但不得再是工厂默认唯一 Win 实现。
2. **doctor** 区分至少：`not_running` / `version_incompatible` / `missing_creds` / `auth_failed` / `api_incompatible` / `ok`（与现有 DoctorReport 风格对齐；可扩字段但别拆坏 Linux doctor）。
3. **硬规则路径表**补 Win 常见密钥位置（`src/hard_rules.py` 及测）：如 `%USERPROFILE%\.ssh`、`AppData\...` 浏览器 cookies/Login Data、Credential Manager 相关路径等——按现有 hard-rule 风格（eternal reject / path 片段），文档或代码皆可，代码优先并加测。
4. **unittest**：适配器契约 + doctor 分类；**Linux 回归不破**（至少 `src.test_adapter_contract`、相关 teleagent_adapter 测、硬规则测）。

无真机时：
- 用 mock transport / 注入 HTTP 响应测发现、鉴权失败分类、permission reply 形状。
- commit message + 模块 docstring 写明 **Windows 真机未验收**。
- 不要假装 live Win TeleAgent 已通。

## 不做

整机 Win VM、部署到用户办公机、DeepSeek 组长、工人池、memslice、阶段2其它大项。

## 验收

- `PYTHONPATH=src python3 -m unittest …` 新测 + 既有 adapter/hard_rules 相关回归绿。
- win32 工厂返回可用适配器类型（非仅 blocked）；doctor 分类有测。
- 顶层文档：更新 `docs/teleagent-worker-contract.md` 或新增 `docs/windows-teleagent-adapter.md` 写明与 Linux 差异要点 + 真机未验。

## 交付

收工写清：SHA、测试命令与结果、真机验了没、与 Linux 差异要点。

开始前 `git log -1` 确认 `a58859c`。勿提交 `.grok-prompts/`、jobs 脏文件。
