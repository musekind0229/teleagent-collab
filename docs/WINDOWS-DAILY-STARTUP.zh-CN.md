# Windows 每日开工（只读就绪）

每天派工前按这三步。`--ready` 只读：不启动 stdin-wrap、不创建 session、不抢桌面锁、不结束 GUI。

## 1. 打开并登录桌面 TeleAgent

启动已安装的桌面 TeleAgent 并完成登录。不要为了探活改走 `--teleagent-stdin-wrap`。端口发现顺序 **4399 → 4397 → 4398**，桌面默认 **:4397**。

## 2. 只读就绪门

在仓库根目录：

```powershell
python bin/collab-service.py --ready
```

薄别名（同一聚合，不另写判定）：`python -m win_collab ready`。

退出码 **0** 仅当 JSON 里 `ready` 与 `dispatch_allowed` 都为真：GUI doctor 为 `ok`，且 `/session/status` 把整机 `occupancy.state` 判为 `idle`。

`occupancy.state` 为 `busy` 或 `unknown`（含接口失败、无法解析）时退出码 **1**，`reasons` 含 `DO NOT DISPATCH` 与 `不要派工`。此时不要派工。

`lock_holder` 只是锁文件里的诊断，可能过期；探针不根据它抢锁，也不把它当成派工许可。

`--check-gui` 仍只做 doctor（端口与凭据存在性），**不读** session 占用。doctor 绿而 session 正忙时它会退出 0，不能当作每日派工门。

## 3. 启动 collab-service

上一步退出码为 0 后再开服务，不要加 `--teleagent-stdin-wrap`：

```powershell
python bin/collab-service.py --persist .collab-app --port 8765 `
  --planner grok --backend teleagent-windows
```

契约见 [应用入口 API](application-api.zh-CN.md)，路径边界见 [Windows 整合状态](WINDOWS-UNIFIED.zh-CN.md)。
