# Backlog（仅 P1 / P2 — 本轮不实现）

## P1

- [ ] Question API 完整编排（`/question` 人机问答 → lead/人）与 session 过滤联调
- [ ] 真机 Windows 探测：若未来版本出现受支持 local auth，再新增 `windows_local_vN`（替换 blocked）
- [ ] 调度器 live A/B：多 job 并行 + 真 pending 串行弹权的长稳跑
- [ ] Lead 可插拔：Claude Code / Codex 与 Grok 同一 `call_lead` 契约的回归集
- [ ] 审批 reconfirm 失败时的自动重拉 + 告警指标

## P2

- [ ] SSE `/event` 推送替代部分轮询（保留自适应 poll 兜底）
- [ ] 跨机器 worker 注册表（多 box）与 session 亲和
- [ ] 产物签名 / 供应链校验钩子
- [ ] 更细的 `doctor` 版本矩阵（SAC `/version` ↔ 包版本）
- [ ] 权限指纹冲突时的人工升级通道（need_human）

> P0（条1 安全加固 + 条4 适配层）见本轮提交；此处只列后续项。
