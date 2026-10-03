你在仓库 teleagent-collab 根目录。只改下列文件，不要 git commit/push，不要读取或打印任何凭据/token/.env/账号池文件。

## 问题
bin/hermes-collab-request.py 的 _emit 用 json.dumps(ensure_ascii=False) 直接 print。Windows 上 Python 被管道/重定向时 stdout 编码是本地代码页（cp936/gbk）。
Windows PowerShell 5.1 里：
- `$x = python bin/hermes-collab-request.py status ID` 按 [Console]::OutputEncoding 解码；
- `python ... | python -c ...` / `| Out-File` 等经 $OutputEncoding（默认 us-ascii）转码，中文变 `?`；
- 若把 stdout 改成 UTF-8 而控制台代码页是 936，PowerShell 又会把 UTF-8 当 GBK 解码成乱码。
Hermes 的 terminal 工具用 UTF-8 解码子进程输出。
所以唯一对 PowerShell（任意代码页、任意管道）和 Hermes 都稳的方案：**默认输出纯 ASCII JSON**（ensure_ascii=True，中文变 \uXXXX；ConvertFrom-Json / json.loads 后字段值是正确中文）。

## 改动（bin/hermes-collab-request.py）
1. _emit 默认 ensure_ascii=True（仍是一行 JSON，default=str 保留）。
2. 可选原样 UTF-8：全局参数 `--unicode`（放在子命令前，和 --http-timeout 同级）或环境变量 COLLAB_JSON_UNICODE=1（true/yes/on 也算）开启；开启时先把 sys.stdout reconfigure 为 encoding="utf-8"（有 reconfigure 才调，失败忽略），再 ensure_ascii=False 输出。
3. main() 开头对 sys.stdout / sys.stderr 做 reconfigure(errors="backslashreplace")（不改 encoding，只防 UnicodeEncodeError；有 reconfigure 才调，异常忽略）。
4. 把 _emit 做成可测：_emit(payload, *, unicode=False, stream=None)；main 根据参数/环境决定。KeyboardInterrupt 路径同样走 _emit。
5. 模块 docstring 与 argparse help 说明默认 ASCII 与 --unicode。

## 单测（src/test_hermes_collab_request.py，沿用现有风格）
- 默认：status 响应含中文（如 goal.desired_outcome="在工作区写 hello.txt"）→ 捕获 stdout 是纯 ASCII（.isascii()），含 "\\u5728"，json.loads 后值等于原中文。
- 错误路径（HTTP 404 且 error 含中文）同样纯 ASCII。
- `--unicode` 与 COLLAB_JSON_UNICODE=1：输出包含原中文字符；环境为 0/空时仍 ASCII。
- 模拟 cp936 流：用 io.TextIOWrapper(io.BytesIO(), encoding="cp936") 作为 stream 调 _emit（默认模式）→ 字节可 ascii 解码；再用 encoding="ascii" 的 TextIOWrapper 流默认模式不抛异常。
- main 对 stdout reconfigure 不会因为 StringIO（无 reconfigure）报错。
现有测试若断言了 ensure_ascii=False 形式的中文输出，按新默认调整。

## 文档（中文，简洁）
- docs/hermes-collab-min-client.zh-CN.md：新增「输出编码 / PowerShell」小节：默认纯 ASCII JSON（中文为 \uXXXX），`$r = python ... | ConvertFrom-Json` 或 `$x = python ...; $x | ConvertFrom-Json` 后字段是正常中文；想直接看中文用 `--unicode`，但在 Windows PowerShell 5.1 中需先 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`（必要时 `$OutputEncoding = [Text.Encoding]::UTF8`）；不要对原始 JSON 文本做字符串匹配中文。
- integrations/hermes/skills/teleagent-collab/SKILL.md：Pitfalls 补一条：输出默认 ASCII 转义，读中文字段用 ConvertFrom-Json / json 解析，不要自己加 --unicode 除非确认终端是 UTF-8；version 升到 0.2.2。保持 frontmatter 合法 YAML。

完成后运行：cd src && python3 -m unittest test_hermes_collab_request -v，全部通过。最后简短列出改动。
