# 任务授权范围（条5）

明确 **文件任务** vs **系统安装任务**，并拆开 **实际隔离能力** 与 **提示词约束**。组长可在用户已授权的章程范围内批操作，不必每次叫用户点；也不可把「安装」解释成无限系统权限。

## 任务种类

| `task_kind` | 含义 | 硬性要求 |
| --- | --- | --- |
| `file_task` | 工作区内文件读写/编辑 | 禁止 `install_roots`；禁止 sudo/包管理/systemd 类 |
| `system_install` | 受控安装（如装远程桌面助手） | 必须非空 `install_roots`、声明 `network_allow`（可 `[]`）、必须有 `rollback` |

样例：

- [`jobs/examples/file-task.charter.yaml`](../jobs/examples/file-task.charter.yaml)
- [`jobs/examples/system-install-sample.charter.yaml`](../jobs/examples/system-install-sample.charter.yaml)（**文档/计划样例**，不是真装 RustDesk）
- [`jobs/examples/controlled-fake-install.charter.yaml`](../jobs/examples/controlled-fake-install.charter.yaml)（P3：workdir 内假包；`controlled_install.py`；真装 blocked）

## 章程字段（扩展）

| 字段 | 说明 |
| --- | --- |
| `task_kind` | `file_task` \| `system_install`（默认 `file_task`） |
| `network_allow` | 可访问的网络来源（主机 / `*.domain` / URL） |
| `install_roots` | 允许改动的安装目录（系统安装必填；安装 ≠ `/`） |
| `lead_review_steps` | 须组长审的步骤标签（编排提示） |
| `user_gate_permissions` | **必须回用户**的权限类别；组长不可代批 |
| `acceptance` / `done_when` | 验收标准 |
| `rollback` | 回滚步骤（系统安装必填） |

校验：`src/charter.py` + `src/task_auth.validate_auth_fields`。  
运行时裁决：`src/task_auth.authorize_action` / `lead_may_approve_without_user`。  
调度器在权限路径上机械拒绝越权与 `user_gate`；灰区仍走 lead，并附带 `task_authorization` 摘要。

## 实际隔离 vs 提示词约束

```text
机械隔离（本仓真做检查 / 落盘）
  ├─ 每 job 独立 workdir
  ├─ path canonicalize + is_path_within
  ├─ hard_rules 永拒 ~/.ssh / cookies / gh hosts / .netrc
  ├─ allow_secret_globs / allow_paths / allow_keys
  ├─ install_roots / network_allow 容纳检查（task_auth）
  ├─ user_gate_permissions 永不被 lead 静默扩大
  └─ state_store：决定落盘；重启不重派、不重发决定

提示词约束（写入工人/组长文本，非 OS 沙箱）
  ├─ must / must_not 散文
  ├─ acceptance / rollback 文案
  ├─ lead_review_steps 标签
  └─ TeleAgent x-opencode-directory 会话目录提示

明确非隔离
  ├─ 无容器 / VM / seccomp / Landlock 由本仓施加
  └─ 「组长已批」≠ 操作系统 capability 边界
```

完整清单见 `task_auth.isolation_capabilities_doc()`。

## 组长权限边界

1. **可批**：落在章程已授权范围内的操作（含文件任务工作区 R/W、系统安装的 `install_roots` 内写入，且不在 `user_gate_permissions`）。
2. **不可批代用户**：`user_gate_permissions` 中的类别（默认含 sudo / systemd / firewall / credential_store_write 等）。
3. **禁止解释**：`task_kind=system_install` ≠ 任意路径、任意端口、任意服务。

## 与条1/条2/条3关系

- 硬规则（条1）仍先于 lead。
- 验收（条2）仍要求产物齐全 + 可选 `force_lead_review`。
- lead 协议（条3）请求里可带 `extra.task_authorization`。
