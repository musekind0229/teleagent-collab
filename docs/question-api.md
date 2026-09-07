# Question API（P3）

TeleAgent SAC 人机问答面，与 `/permission` 并列。工人工具可抛出 pending question；编排侧必须按 **sessionID** 过滤，默认 **need_human**，禁止发明答案当 PASS。

## 探测结果（本机 2.5.0 / SAC 1.2.27）

| 路由 | 状态 |
| --- | --- |
| `GET /question` | **有** — 返回 list（可空） |
| `POST /question/:id/reply` | **有** — body `{"answers": [[string, ...], ...]}`（`[][]string`） |
| `POST /question/:id/reject` | **有** |

若探测失败：`doctor` extras.`question_api` 与本页标 **缺口**；`probe_question_api` → `available=False`。

## 代码

| 文件 | 作用 |
| --- | --- |
| `src/question_api.py` | probe / list / reply / reject + session 绑定 + `need_human` |
| `src/teleagent_adapter/linux_local_v1.py` | `list_questions` / `reply_question` / `reject_question` |
| `src/teleagent_adapter/windows_blocked.py` | 全部 blocked |
| `src/teleagent_adapter/doctor.py` | 附加 question probe（缺口写入 extras，不单独把 doctor 打红） |
| `bin/run-live-grok-lead.py` | 真机轮询 question → need_human |

## 编排规则

1. **session 绑定**：reply/reject 前比对 `session_id_of(pending)`；错会话抛错/跳过。
2. **默认 need_human**：`handle_question_need_human` 无 `auto_answers` / lead 回调时不自动答。
3. **禁止假 PASS**：缺 API ≠ 当作已回答。

## 待办

- [ ] lead/人完整问答编排（多题、选项 UI 映射）
- [ ] scheduler 扫描环与 permission 对称的 question 串行处理
