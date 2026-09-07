# Backlog

## P3（本轮）

- [x] Grok Lead 真机脚本 `bin/run-live-grok-lead.py`（创建→权限→GrokCLI→继续→验收；失败写清原因）
- [x] Claude/Codex 骨架 stub + docs 待办（不假 PASS）
- [x] Question API：探测 TeleAgent；接入 list/reply/reject + session 绑定；doctor extras；缺则标缺口
- [x] 受控安装单：`task_kind=system_install`；workdir 内假包/可回滚；授权字段门控；危险真装 blocked
- [x] Win：确认仍 blocked（含 question 方法）
- [x] `docs/p3-test-results.md` + 本 backlog

## P1 / P2 剩余

- [ ] Question API 完整 lead/人编排与 scheduler 对称扫描（基础 list/reply 已在 P3）
- [ ] 真机 Windows：若未来版本出现受支持 local auth，再新增 `windows_local_vN`
- [ ] 调度器 live A/B：多 job 并行 + 真 pending 串行弹权长稳跑
- [ ] 审批 reconfirm 失败时的自动重拉 + 告警指标
- [ ] Claude Code / Codex CLI **真机** lead（替换 stub）

## 已完成留档

- [x] P0：条1 + 条4（teleagent_adapter + 权限硬化）
- [x] P1：条2 + 条3（完成判定 + lead_adapter）
- [x] P2：条5 + 条6（任务授权 + 故障恢复）

## 后续 / 非目标

- [ ] SSE `/event` 推送替代部分轮询
- [ ] 跨机器 worker 注册表与 session 亲和
- [ ] 产物签名 / 供应链校验钩子
- [ ] 更细 doctor 版本矩阵
- [ ] 权限指纹冲突人工升级通道
- [ ] 不做 IPv6 服务器部署（明确非目标）
- [ ] 不做关鉴权 / 假 PASS
