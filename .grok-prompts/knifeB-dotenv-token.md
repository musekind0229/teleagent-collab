你在仓库 teleagent-collab 根目录。只改下列文件，不要 git commit/push，不要读取或打印任何真实凭据/token/账号池文件（包括 ~/.hermes/.env、~/.grok）。

## 背景 / bug
bin/hermes-collab-request.py 只从进程环境读 COLLAB_API_BASE / COLLAB_API_TOKEN。但 skill（integrations/hermes/skills/teleagent-collab/SKILL.md）和文档说来源是“进程环境变量，或 Hermes 的 .env（Windows: %LOCALAPPDATA%\hermes\.env）”。
直接在终端跑脚本（进程环境里没有 COLLAB_API_TOKEN，token 只在 Hermes 的 .env 里）时，脚本不带 Bearer → 服务 401。

## 最小修复（bin/hermes-collab-request.py）
1. 新增 _dotenv_path() -> Path | None：优先 COLLAB_ENV_FILE（非空）；否则 HERMES_HOME（非空）/.env；否则 Windows(os.name=="nt") 用 %LOCALAPPDATA%\hermes\.env（LOCALAPPDATA 缺失则 None）；其它系统 ~/.hermes/.env。
2. 新增 _read_dotenv(path) -> dict[str,str]：文件不存在/不可读返回 {}；utf-8-sig 解码（errors="replace"）；忽略空行和 # 注释行；支持可选 "export " 前缀；KEY=VALUE 按第一个 = 切；key/value strip；值两端成对的单/双引号去掉（引号内内容原样保留）；未加引号的值去掉 " #" 开始的行内注释。不做变量展开。
3. 新增 _setting(name) -> tuple[str, str]：返回 (value, source)。进程环境非空（strip 后）→ (value, "env")；否则 .env 里该 key 非空 → (value, "dotenv")；否则 ("", "none")。只查 COLLAB_API_BASE / COLLAB_API_TOKEN 两个 key，不要把 .env 其它变量注入 os.environ。结果每次调用实时读（不缓存，便于测试），可接受。
4. _base_url() / _token() 改用 _setting。
5. HTTP 401/403 时，在错误 payload 里追加 "auth": {"token_source": "env"|"dotenv"|"none", "env_file": str(路径) 或 null}，绝不包含 token 值或其长度/前缀。
6. 模块 docstring 与 argparse description 写明查找顺序。

## 单测（src/test_hermes_collab_request.py，沿用现有 mock 写法；用 tempfile 写临时 .env，并用 mock.patch.dict(os.environ, ..., clear=False) 删掉/设置 COLLAB_API_TOKEN、COLLAB_API_BASE、COLLAB_ENV_FILE、HERMES_HOME；注意现有 setUp 往环境里放了 COLLAB_API_TOKEN=test-token，新测试要显式 pop 掉）
- 环境无 token、COLLAB_ENV_FILE 指向含 COLLAB_API_TOKEN="dotenv-tok" 的文件 → 请求带 Authorization: Bearer dotenv-tok。
- HERMES_HOME/.env 路径被使用（不设 COLLAB_ENV_FILE）。
- 进程环境优先于 .env。
- .env 里 COLLAB_API_BASE 生效。
- 解析：注释、空行、export 前缀、单/双引号、行内注释、BOM、无 = 的行被忽略。
- 文件不存在 → 无 Authorization 头，不抛异常。
- 401 响应 → exit 1，stdout JSON 含 auth.token_source=="dotenv" 且整个 stdout 不包含 token 字符串。
- 无 token 时 401 → auth.token_source=="none"。
- os.name=="nt" 分支：patch os.name 与 LOCALAPPDATA，只测 _dotenv_path() 返回值（不要真的在 nt 下跑 HTTP）。

## 文档（中文，简洁）
- docs/hermes-collab-min-client.zh-CN.md 环境变量一节：写明查找顺序（进程环境 > COLLAB_ENV_FILE > HERMES_HOME/.env > Win %LOCALAPPDATA%\hermes\.env / 其它 ~/.hermes/.env），只读这两个 key，401 时看 auth.token_source 排查。
- integrations/hermes/skills/teleagent-collab/SKILL.md 连接配置表：同样说明（脚本自己会读 Hermes .env，不需要 export；Hermes 进程也会把 .env 载入环境）；401 时把 auth.token_source 告诉用户，不要回显 token。version 升到 0.2.1。保持 frontmatter 合法 YAML。

完成后运行：cd src && python3 -m unittest test_hermes_collab_request -v，全部通过。最后简短列出改动。
