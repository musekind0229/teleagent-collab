# 任务：Windows 端口发现补 4398

Repo：`/workspace/teleagent-collab`（musekind0229/teleagent-collab）。改完测绿后 **push origin/main**。

## 背景（真机已核实）
- DESKTOP-TBB531F 重启 TeleAgent 后，工人 HTTP 现监听 **:4398**（401 面在）；**:4397 / :4399 均关闭**。
- 当前 `DEFAULT_WIN_PORTS = (4399, 4397)`，doctor 仅回退到 4397 → 报 `not_running`。
- 凭据 PEB 仍 blocker（另跟）；**本刀只改端口发现/文档/测**，不要做 live，不要抬 SeDebug，不要碰海景房 / Antigravity。

## 要做（最小）
1. `src/teleagent_adapter/windows_local_v1.py`：
   - `DEFAULT_WIN_PORTS` 改为 **`(4399, 4397, 4398)`**（顺序：先 Linux 惯用 4399，再历史 Win 4397，再新观察 4398）。
   - `discover_windows_base_url` 文档字符串同步；保持 `TELEAGENT_BASE_URL` / `TELEAGENT_PORT` 覆盖优先。
2. `src/teleagent_adapter/doctor.py`：
   - extras `ports` 报告里包含 **4398**。
   - Win 上回退：preferred 关闭时依次试 **4397 再 4398**（与 discover 一致），不要只认 4397。
3. 单测（`test_adapter_contract.py` / doctor 相关）：
   - 仅 4398 开 → discover 得到 `http://127.0.0.1:4398`。
   - doctor：base 指 4399 但仅 4398 开 → 落到 4398（mock `port_open_fn`）。
   - 原 4399/4397 测保持绿。
4. 文档短改：`docs/windows-teleagent-adapter.md`（及必要时 `docs/lead-adapter.md` / `docs/teleagent-adapter.md`）写清 Win 发现顺序 **4399→4397→4398**，并记一笔 DESKTOP-TBB531F 曾见 **:4398**。不要把默认 `TELEAGENT_BASE_URL` 死写死成 4398（仍可用 4397 作文档示例，强调以发现为准）。
5. **无 token/密钥进 git**。

## 验收
```bash
cd /workspace/teleagent-collab
PYTHONPATH=src python3 -m unittest \
  teleagent_adapter.test_adapter_contract \
  teleagent_adapter.test_windows_process_environ -q
git push origin main
```
回报：SHA、文件清单、发现顺序一句话。

不要改凭据发现主路径；不要跑真机 live；不要动海景房。
