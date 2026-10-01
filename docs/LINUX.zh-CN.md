# Linux 上的 teleagent-collab

在 Debian/Linux 上开发、自检。不要连远程机器。不要把 TeleAgent 本地 API 的用户名、密码、session key，或 `COLLAB_API_TOKEN` 打进日志、报告或 Git。

## 现在能做什么

- `bin/collab-service.py --backend teleagent-linux`（别名 `teleagent_linux`）把工人交给监督控制器。后端 id 是 `teleagent.linux.supervised_v1`。它复用 `win_collab` 的 Engine/Store，不另写一套：外部输入隔离、污染门、权限范围摘要、验收决定、桌面锁都还在。
- 没有 Linux 版的 stdin_wrap。传入 `stdin_wrap=True` 会直接拒绝。
- 状态目录是 `<persist>/linux-controller`。
- GUI 没登录时服务也能起。`GET /health` 不拨 TeleAgent。`--ready` 说明为什么还不能派工。客户端要到真正派工时才创建。
- 桌面锁在没设 `LOCALAPPDATA` 时落在 `~/.local/share/teleagent-collab/desktop-locks`（可用 `TELEAGENT_DESKTOP_LOCK_DIR` 改）。实现是 POSIX `fcntl.flock`。
- Windows 后端 id 仍是 `teleagent.windows.supervised_v1`，行为不变。`python -m win_collab ready` 仍走 Windows 门禁。

## 工人怎么连上 TeleAgent

Linux TeleAgent（GUI 2.5.x，opencode 风格本地 API）使用和 Windows 相同的 local-v1 HMAC，以及同样的三个环境变量：

- `OPENCODE_SERVER_USERNAME`（也认 `SUPER_AGENT_OPENCODE_USERNAME`）
- `OPENCODE_SERVER_PASSWORD`（也认 `SUPER_AGENT_OPENCODE_PASSWORD`）
- `SUPER_AGENT_LOCAL_SESSION_KEY`

登录 GUI 之后才监听 `127.0.0.1:4399`。地址只接受 `http://127.0.0.1:<端口>`。`TELEAGENT_URL` 可以改端口，不能改主机，也不能带用户名、密码、路径。

凭据查找顺序：

1. 本进程三个变量都有，就用它们。
2. 否则只读 TeleAgent 进程的 `/proc/<pid>/environ`。映像必须是 `/opt/TeleAgent/teleagent`，或某个用户 `~/.local/share/TeleAgent/runtimes/` 下面的 super-agent-code / node。`TELEAGENT_LINUX_IMAGES`（`os.pathsep` 分隔）会换成你给的列表。多个来源必须一致，否则报错且不猜。
3. 读不到时看原因：没有 TeleAgent 进程；进程在但 environ 不可读（要用 TeleAgent 用户或 root 跑 collab-service）；键不在 environ（GUI 还没登录）。错误里只有计数，没有值。

## `--ready`

`sys.platform` 以 `linux` 开头时，`--ready` 和 `--check-gui` 都调用 `assess_linux_gui_readiness`。在非 Linux 上只有显式 `--backend teleagent-linux` 才走这条。退出码和 Windows `--ready` 一样：`ready` 和 `dispatch_allowed` 都为真才是 0。

检查项：

| 检查 | 失败时是否挡住派工 |
| --- | --- |
| Python ≥ 3.10 | 是 |
| `jsonschema` 可导入 | 否（测试用；下一步是 `pip install -r requirements.txt`） |
| 有 TeleAgent 进程映像 | 是 |
| X / Xvfb / `DISPLAY` | 否，只提示 |
| `127.0.0.1:4399`（或 `TELEAGENT_URL` 的端口）在监听 | 是 |
| 凭据能发现（只报有没有，区分「这个用户读不到」和「还没登录」） | 是 |
| `/session/status` 空闲 | 是。忙或不确认时 `DO NOT DISPATCH` / `不要派工` |
| 运行 tip 与 HEAD 一致 | 是 |

不会创建 session，也不会抢走桌面锁。

GUI 未登录时的下一步是：打开 GUI（VNC 到 Xvfb 的显示），登录；`:4399` 在登录之后才起来。

## 依赖与 systemd

运行时不需要第三方包。测试依赖见仓库根目录的 `requirements.txt`。

给一台已经在跑 Xvfb 和已登录 TeleAgent 的机器用的单元文件在 `deploy/systemd/`。复制、`daemon-reload`、`enable --now` 之前先看那里的说明。本仓库的开发流程不会安装该单元。
