# Windows：TeleAgent + Antigravity 部署

本机短清单。账号池见 [agy 账号池](agy-account-pool.md)，agy 工人见 [Antigravity 工人适配](antigravity-worker-adapter.md)，每日派工门见 [Windows 每日开工](WINDOWS-DAILY-STARTUP.zh-CN.md)。

## 1. 拉取仓库

```powershell
git clone https://github.com/musekind0229/teleagent-collab.git
cd teleagent-collab
git pull
```

已有检出时，在仓库根对齐 `origin/main` 即可。

## 2. agy 与 TeleAgent GUI

`agy` 在 PATH 上，或把 `AGY_BIN` 指到可执行文件。先登录桌面 TeleAgent，再派工。端口发现 **4399 → 4397 → 4398**（桌面常见 **:4397**）。不要为了探活改走 `--teleagent-stdin-wrap`。

## 3. 双 HOME

每个 agy 账号一个目录，例如 `C:\Users\Admin\agy-profiles\<id>`。池 JSON 的 `home` 用这个绝对路径。一次 spawn 钉死一个 HOME。

## 4. 文件凭据（只进子进程）

换号后库在**本次 spawn 的子进程 environ** 注入伪 `SSH_CONNECTION` / `SSH_CLIENT` / `SSH_TTY`，agy 1.2.11 才走文件 token。`GEMINI_FORCE_FILE_STORAGE=true` 仍会设置，但 **1.2.11 不认**。不要把这些变量写进 PowerShell profile 或机器环境。

串行换号时清机器槽 `gemini:antigravity`（`cmdkey /delete:gemini:antigravity`；槽不存在也继续）。

## 5. 串行换号

不要同时跑多个 `agy --print`。并发写可能把 oauth 文件尾部写坏。换号只发生在下一次 spawn。

## 6. 代理

本机示例 `http://127.0.0.1:7897`。写在池 JSON 的 `http_proxy` / `https_proxy`（账号或顶层），或 `COLLAB_AGY_HTTP_PROXY` / `COLLAB_AGY_HTTPS_PROXY`。只配了 HTTP 时镜像到 HTTPS。库内不写死地址。

## 7. 池 JSON

真实池（如 `jobs/agy-account-pool.json`）保存为 **UTF-8、无 BOM**，并且 gitignore。从 `jobs/examples/agy_account_pool.example.json` 复制，再填本机 `home`。文件里不要放 token。

## 8. 每日 `--ready` 与 tip

```powershell
python bin/collab-service.py --ready
```

退出码 0 才开服务。reason 里出现 `running tip <…> != HEAD <…>` 时，重启 `collab-service`，让进程内 tip 与当前 `HEAD` 相同后再派。

## 9. hello 烟测才开 auto-approve

```powershell
$env:AGY_AUTO_APPROVE = '1'
python bin/run-job.py --backend antigravity jobs/examples/hello.charter.yaml
```

等价：`COLLAB_AGY_AUTO_APPROVE=1`，或章程 `agy_auto_approve: true`。这只给这次烟测加上 `--dangerously-skip-permissions`，工人才能写出产物。不设时权限通道 unsupported，产物为空，finish 像 stop 也会假失败。不要把 skip-permissions 做成所有工单的默认。

## 10. 不要动别的机器

只操作本机这份检出和本机 agy profile。不要登录、改配置或派工到海景房和其他桌面。

## 11. 不要贴 token

禁止粘贴、打印或提交 access / id / refresh token、oauth JSON 和真实池文件。日志和异常同样不要带 token 正文。
