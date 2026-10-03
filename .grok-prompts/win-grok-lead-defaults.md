# 任务：Windows 控制层默认接本机 Grok CLI

Repo：`/workspace/teleagent-collab`（musekind0229/teleagent-collab）。改完测绿后 **push origin/main**。

## 背景
- 工人层 TeleAgent 在 DESKTOP-TBB531F 已通：HTTP **:4397** + PEB 凭据。
- 本机 Grok：`C:\Users\Admin\.grok\bin\grok.exe` v1.0.34。
- 现 `src/lead_adapter/grok_cli.py` 与 `bin/run-live-grok-lead.py` 默认 `COLLAB_LEAD_BIN=/workspace/run-grok.sh`（Linux）。
- `run-live-grok-lead.py` 已有 fallback `~/.grok/bin/grok`，但 Win 上应认 **`grok.exe`**；默认 base-url 仍 4399，Win 真机是 **4397**。

## 要做（最小）
1. **解析默认 lead bin**（可放 `grok_cli.py` 小函数，供工厂/`run-live` 共用）：
   - 若 `COLLAB_LEAD_BIN` 已设且存在 → 用之。
   - else PATH 上的 `grok` / `grok.exe`。
   - else `Path.home()/".grok"/"bin"/"grok.exe"`（Win）或 `.../grok`（posix）。
   - else 保留旧 Linux 默认 `/workspace/run-grok.sh`（仅当文件存在时）；否则清晰错误。
2. `GrokCliLeadAdapter` 默认 bin 走上述解析；**禁止**失败后去掉 `--disallowed-tools` 再试。
3. `bin/run-live-grok-lead.py`：
   - `--lead-bin` 默认同上。
   - `--base-url`：env `TELEAGENT_BASE_URL` 或 Win 默认 `http://127.0.0.1:4397`，非 Win 默认 4399；或复用 `discover_windows_base_url`。
   - docstring/Usage 写清 Win 用法。
4. 文档：`docs/lead-adapter.md` 和/或 `docs/windows-teleagent-adapter.md` 补一段 **Windows 控制层**：
   ```
   set COLLAB_LEAD_ADAPTER=grok_cli
   set COLLAB_LEAD_BIN=%USERPROFILE%\.grok\bin\grok.exe
   set TELEAGENT_BASE_URL=http://127.0.0.1:4397
   python bin/run-live-grok-lead.py
   ```
5. 单测：解析逻辑用临时路径 mock（Win/posix）；现有 lead 测保持绿。
6. **无 token/密钥进 git**。

## 验收
```bash
PYTHONPATH=src python3 -m unittest discover -s src -p 'test_*.py' -q   # 或现有 lead/adapter 测包
git push origin main
```
回报：SHA、文件清单、默认解析行为一句话。

不要改 Antigravity；不要动海景房。
