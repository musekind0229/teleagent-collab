# systemd：collab-service（只放在仓库里）

这些文件不会被本仓库的测试或开发流程安装到系统里。需要的人在目标机器上按下面的步骤自己复制。

## 前提

- 已经有 Xvfb（或别的 X），并且 TeleAgent GUI 已在那个显示上登录。登录之后 `127.0.0.1:4399` 才会监听。
- collab-service 用 **TeleAgent 同一个用户** 跑，这样读得到 `/proc/<pid>/environ`。用 root 也可以。不要用第三个无关账号，否则会看到「进程在，但 environ 不可读」。
- 代码放在示例目录 `/opt/collab/teleagent-collab`（按实际路径改 `WorkingDirectory`）。
- 持久化目录示例是 `/var/lib/teleagent-collab`，运行用户必须能写。
- Python 3.10+。运行时不需要第三方包。

## 单元在做什么

`collab-service.service`：

- `ExecStart=/usr/bin/python3 bin/collab-service.py --persist /var/lib/teleagent-collab --port 8765 --backend teleagent-linux --planner deterministic`
- `EnvironmentFile=-/etc/teleagent-collab/collab-service.env`（文件可以先不存在；`COLLAB_API_TOKEN` 放这里，模式 `600`）
- `Restart=on-failure`
- `NoNewPrivileges=yes`、`PrivateTmp=yes`、`ProtectSystem=full`
- **不要** 加 `ProtectProc=invisible`（会藏住 `/proc` 里的 environ）
- **不要** 加 `ProtectHome=`（会藏住 `~/.local/share/TeleAgent`）
- `ProtectSystem=full` 只把 `/usr`、`/boot`、`/etc` 收成只读，`/proc` 仍可见，`/var/lib` 仍可写。服务用户如果不能往安装目录写字节码，保持 `PYTHONDONTWRITEBYTECODE=1`。

`collab-service.env.example` 里只有占位符，没有真密钥。

## 安装

```bash
sudo install -d -m 0755 /etc/teleagent-collab
sudo install -m 0600 deploy/systemd/collab-service.env.example /etc/teleagent-collab/collab-service.env
# 编辑 /etc/teleagent-collab/collab-service.env，把 COLLAB_API_TOKEN 换成本机随机值
sudo install -m 0644 deploy/systemd/collab-service.service /etc/systemd/system/collab-service.service
# 若用户不叫 teleagent，先改单元里的 User=/Group=
sudo systemctl daemon-reload
sudo systemctl enable --now collab-service.service
sudo systemctl status collab-service.service
journalctl -u collab-service.service -n 100 --no-pager
# 用运行服务的同一个用户做每日门禁（退出码 0 才能派工）
sudo -u teleagent python3 /opt/collab/teleagent-collab/bin/collab-service.py \
  --persist /var/lib/teleagent-collab --backend teleagent-linux --ready
```

`--ready` 不启动 HTTP，也不创建 TeleAgent session。

## 用 venv 和只绑本机（drop-in，2026-10-04 在 mde 上用的做法）

仓库里的单元不改，另加一个 drop-in 覆盖 `ExecStart`：

```bash
python3 -m venv /opt/collab/venv
/opt/collab/venv/bin/pip install -r /opt/collab/teleagent-collab/requirements.txt
sudo install -d -m 0755 /etc/systemd/system/collab-service.service.d
sudo tee /etc/systemd/system/collab-service.service.d/override.conf >/dev/null <<'EOF2'
[Service]
ExecStart=
ExecStart=/opt/collab/venv/bin/python bin/collab-service.py --host 127.0.0.1 --port 8765 --persist /var/lib/teleagent-collab --backend teleagent-linux --planner deterministic
EOF2
sudo systemctl daemon-reload && sudo systemctl restart collab-service
```

checkout 归 root、服务以 TeleAgent 用户跑是正常配置：服务读 `HEAD` 时只对这个 checkout 加 `-c safe.directory=<仓库>`，不需要改全局 git 配置。旧版本（61e410c 及以前）在这种配置下读不到 `HEAD`，每次开单都会 409 `running_tip_mismatch`。

更新代码后要 `systemctl restart collab-service`，否则 `--ready` 报运行 tip 与 `HEAD` 不一致（这是有意的）。

## 内存上限（drop-in `memory.conf`，2026-10-04 在 mde 上加的）

```ini
# /etc/systemd/system/collab-service.service.d/memory.conf
[Service]
MemoryAccounting=yes
MemoryHigh=700M
MemoryMax=900M
MemorySwapMax=0
OOMScoreAdjust=1000
```

- 服务拉起的 worker（agy/grok/codex）、组长和它们的工具子进程都继承 `collab-service.service` 的 cgroup（`setsid` 不换 cgroup），一起受这个上限约束。
- 先确认 cgroup v2 memory 控制器真正生效：`cat /sys/fs/cgroup/system.slice/collab-service.service/memory.max` 应为 `943718400`。不是的话，限制是空的。
- `OOMScoreAdjust` 取 1000，不是 500：oom_score ≈ RSS 占比×1000 + adj。在 mde 上 TeleAgent 渲染进程是 adj 300、score 约 1018，服务取 500 时 score 约 1000，仍然比它低。取 1000 才能保证全局 OOM 时服务先被杀。
- worker 被 cgroup OOM 杀掉时，失败 `source` 是 `oom`，`failure_reason` 以 `killed by OOM (cgroup memory limit)` 开头。
- 服务主进程被 OOM 杀掉后，systemd 会重启并给一个新的 cgroup（`oom_kill` 计数归零）。所以单元里的 `ExecStopPost=… --record-exit` 会把 `$SERVICE_RESULT`（`oom-kill`）写到 `<persist>/last-exit.json`。下次启动时日志会打 `previous service process ended: result=oom-kill`，被收割的 run 的原因后面会加上 OOM 说明。

## 卸载

```bash
sudo systemctl disable --now collab-service.service
sudo rm -f /etc/systemd/system/collab-service.service
sudo systemctl daemon-reload
# 密钥文件和 /var/lib/teleagent-collab 不会自动删。确认不需要后再手动删除。
```
