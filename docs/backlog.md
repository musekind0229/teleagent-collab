# Backlog（P1 剩余 + P2 后续）

## P1（剩余）

- [ ] Question API 完整编排（`/question` 人机问答 → lead/人）与 session 过滤联调
- [ ] 真机 Windows 探测：若未来版本出现受支持 local auth，再新增 `windows_local_vN`（替换 blocked）
- [ ] 调度器 live A/B：多 job 并行 + 真 pending 串行弹权的长稳跑
- [ ] 审批 reconfirm 失败时的自动重拉 + 告警指标

## P1（已完成，留档）

- [x] 完成判定与验收（条2）
- [x] 组长可插拔适配（条3）

## P2（本轮已完成）

- [x] 明确任务授权范围（条5）：`task_kind` / `network_allow` / `install_roots` / `lead_review_steps` / `user_gate_permissions` / acceptance / rollback；`docs/task-authorization.md`；样例章程；机械隔离 vs 提示词约束分述
- [x] 故障恢复与测试（条6）：`state_store` 持久化；重启不重派/不重发决定/不误接管会话；取消请求 vs 执行停止；超时仅本任务；模拟回归 + 真机尽力路径

## P2 / 后续（未做）

- [ ] SSE `/event` 推送替代部分轮询（保留自适应 poll 兜底）
- [ ] 跨机器 worker 注册表（多 box）与 session 亲和
- [ ] 产物签名 / 供应链校验钩子
- [ ] 更细的 `doctor` 版本矩阵（SAC `/version` ↔ 包版本）
- [ ] 权限指纹冲突时的人工升级通道（need_human）
- [ ] Lead 可插拔：Claude Code / Codex CLI 与 Grok 同一契约的**真机**回归集（协议已就绪，真机回归仍挂后续）
- [ ] 条6 真机全链路若登录/TeleAgent 卡住：解除阻塞后再补「创建→权限→外部组长批→继续→验收」完整绿通
- [ ] 不做 IPv6 服务器部署（明确非目标）

> P0 = 条1 + 条4；P1 = 条2 + 条3；P2 本轮 = 条5 + 条6。
