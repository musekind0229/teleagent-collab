# Backlog（仅未完成 P1 / P2 — 本轮不吞 P2）

## P1（剩余）

- [ ] Question API 完整编排（`/question` 人机问答 → lead/人）与 session 过滤联调
- [ ] 真机 Windows 探测：若未来版本出现受支持 local auth，再新增 `windows_local_vN`（替换 blocked）
- [ ] 调度器 live A/B：多 job 并行 + 真 pending 串行弹权的长稳跑
- [ ] 审批 reconfirm 失败时的自动重拉 + 告警指标

## P1（本轮已完成，留档）

- [x] 完成判定与验收（条2）：全产物 / 禁 timeout 成功 / force_lead_review 串并行 / 指纹门闩 / ReworkBudget
- [x] 组长可插拔适配（条3）：`lead_adapter` + 结构化协议；删 disallowed-tools 降级；inprocess/Codex 对话接入；Grok CLI 为可选后端

## P2（勿吞）

- [ ] SSE `/event` 推送替代部分轮询（保留自适应 poll 兜底）
- [ ] 跨机器 worker 注册表（多 box）与 session 亲和
- [ ] 产物签名 / 供应链校验钩子
- [ ] 更细的 `doctor` 版本矩阵（SAC `/version` ↔ 包版本）
- [ ] 权限指纹冲突时的人工升级通道（need_human）
- [ ] Lead 可插拔：Claude Code / Codex CLI 与 Grok 同一契约的**真机**回归集（协议已就绪，真机回归仍挂 P2/后续）

> P0 = 条1 + 条4；P1 本轮 = 条2 + 条3。P2 只写 backlog，未实现。
