# 2026-10-11 隔离源码恢复检查点

仅新增本目录；未替换主线源码、未部署、未推送、未打开 PR。`SOURCE_MANIFEST.json` 记录六个历史原文件的 SHA-256、字节数、实现归属及限定验收范围。原文件逐字节保存，未静默脱敏后沿用接受结论。

## 有界复核

在本目录使用 Python 3.12.4 及已安装的 jsonschema（本次默认 Python 3.14.7 缺少 jsonschema，离线测试改用已有 3.12；未安装包或改变环境）：

```sh
python -I -B verify_checkpoint.py
python -I -B verify_checkpoint.py --offline-tests
```

默认模式只核对 source manifest、隐私模式扫描及六个源码的内存编译，不导入候选生产模块。离线测试模式额外执行九项 `native_entry` 假 runner 测试及三项纯 `lease_witness` 测试。测试临时数据位于本目录专属临时子目录并自动清理；禁止子进程、系统命令、网络和 keyring 导入。测试不调用 AGY CLI、真实模型或账户代码。报告不等于 OS 沙箱证明。

`review-policy` 文件仅静态编译；本次不运行其依赖旧 framework/common 的行为测试。历史六项 GREEN 不算本次重跑，完整测试文件不是独立可运行的上游集成保证。没有执行 CI、正式服务、账户操作或主线能力回归。

## 发布边界

不发布原始 receipt、日志、trace、工作指令、账户标识、私有 controller execution ID、绝对个人路径、认证目录、环境配置或能力绑定。租约模块、控制脚本、绑定私有环境的 public-native backend 和其它候选均不在本次范围内。模式扫描与人工阅读是有界风险检查，不是任意未知秘密不存在的证明。

目录内快照不得视为当前 main 的覆盖件；必须另行完成版本差异审查、责任归属、集成及正式入口验收。技术结论见 `RETROSPECTIVE.zh-CN.md`。本次策展和验证由 Hermes 完成，保留 AGY 实现者与独立评审者归属。
