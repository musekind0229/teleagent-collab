# teleagent-collab

把天翼星辰 **TeleAgent** 当成编排里的便宜工人：用本地 HTTP（`:4399`）下单、等完结、接权限弹窗；**组长/门禁可插拔**（烟测默认 Grok Build），不焊死某一家。

> GitHub About: `Scriptable TeleAgent worker loop: pluggable lead approval, evidence review, and portal-metering via queryID.`

## 解决什么

市面编程 agent 能干活，但不适合人一直坐着点审批。本仓把「投递 →（可选）审批 → 交货 → 验收」收成可脚本化回路，方便接到更大的开发 cluster（永续层发包、执行层施工）里。

## 已验证能力

- **工人**：TeleAgent 本地 HTTP（Basic + HMAC），不靠 GUI 点窗口开工
- **审批环**：`GET /permission` → 组长决策 `once|always|reject` → `POST /permission/:id/reply`
- **验收环**：产物 + 统一 `run-evidence.txt`（命令 + stdout/stderr）；组长 `pass|fail`
- **等待策略**：会话 idle / 产物齐即收；墙钟只做死锁保险丝（避免「货已交仍 timeout」）
- **编制**：`call_lead` + `COLLAB_LEAD_BIN`，组长可换 Claude Code / Codex 等
- **计量**：HTTP 下单需带 `queryID: q_<uuid>`，门户积分按模型档服务端计算（如 **chat-pro**）；缺 `queryID` 时本地有账、门户不计

## 非目标

- 不是又一个编程 IDE / 不是重造 TeleAgent
- 不接陪伴人格记忆（与 memslice 等解耦）
- 不把密钥、session 日志、沙箱产物推进 Git

## 仓库结构

```
docs/                 # 工人契约、发现笔记、计量笔记
src/                  # glue / cut2 / cut3 胶水源码
templates/            # 报告模板（脱敏）
```

## 快速使用

1. TeleAgent 桌面已登录，本地 `:4399` 在听。
2. 安装并登录可插拔组长（默认 Grok Build CLI）。
3. 设置环境变量后跑胶水：

```bash
export COLLAB_LEAD_BIN=/path/to/grok   # 或其它组长二进制
export COLLAB_LEAD_NAME=grok
# 门户积分可见时用 chat-pro；默认 chat-lite 可能不计门户分
export TELEAGENT_MODEL_ID=chat-pro
export TELEAGENT_PROVIDER_ID=NewApi

cd src
python3 cut3.py   # 或 glue.py / cut2.py（按脚本约定的工作目录）
```

**禁止** `--always-approve` / yolo / 全局 auto-approve。审批必须走 `call_lead`。

每次工人 prompt 会自动带 `queryID: q_<uuid4()>`（见 `src/glue.py`）。

## 一句话

**TeleAgent 出力，可插拔组长把门，脚本把审批和验收跑完——给人只留开题和收件。**
