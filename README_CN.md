# CodexQuotaTool

中文 · [English](README.md)

在本机查看 Codex 账号用量和重置机会。导入 OAuth `auth.json` 后，可以查询额度窗口、查看重置机会的到期时间、立即使用一次机会，或预约一次重置。

- 查看剩余额度和各窗口的自然恢复时间。
- 选择指定重置机会，立即使用或定时执行。
- 分别管理多条预约，查看执行记录。
- 使用隔离的离线演示账号体验完整流程。

![演示账号的用量、重置机会和预约任务](docs/dashboard.png)

## 快速开始

需要 **macOS 或 Linux、Python 3.10+ 和 curl**。服务使用 Python 标准库，网页由静态 HTML、CSS 和 JavaScript 组成。

```sh
git clone https://github.com/T-Atlas/CodexQuotaTool.git
cd CodexQuotaTool
./run.sh start
```

启动器会打开网页并打印地址，从 `8765` 开始选择空闲的本机端口。macOS 也可以双击 `启动.command`。

在页面上传 `auth.json`，或点击「粘贴 JSON」，粘贴文件内容后选择「解析并导入」，再点击「刷新用量与重置机会」。也可以把文件放进项目目录，服务会在空闲时读取文件变化。网页当前使用中文界面。

支持 Codex 的嵌套 `tokens` 对象和 CLIProxyAPI 的平铺 OAuth 格式。凭证需要包含 `access_token`；账号 ID 可以由 `account_id` 提供，也可以从令牌的账号声明中读取。文件中有可用的 `refresh_token` 时，服务会续期过期的访问令牌，并将更新后的凭证保存到本地。这些接口使用 ChatGPT 账号凭证，OpenAI API key 用于另一套 API。

## 重置机会与预约

额度窗口会在上游返回的时间自然恢复。重置机会有独立的到期时间，可以在到期前使用，恢复符合条件的额度窗口。

选择一张机会后，可以立即使用，也可以设置未来的执行时间。每条预约绑定一个具体的机会 ID。不同机会可以分别预约，并在执行前单独取消。确认弹窗会显示账号、所选机会、时间和浏览器时区。

服务在提交重置前重新核实机会状态，先保存请求 UUID，再向上游提交。随后查询用量和机会明细，核实执行结果。结果不确定时，该账号的后续重置会暂停。点击「核实结果」只查询状态；需要重试时，工具沿用原请求 UUID 和机会 ID。

预约和操作记录会保留到服务重启后。定时执行需要电脑保持唤醒、联网，并运行后台服务。关闭网页或终端后，后台服务继续运行；重启电脑后需重新启动服务。机会已过期，或预约迟到超过 15 分钟时，任务会跳过。

## 命令

```sh
./run.sh start --no-open  # 后台启动
./run.sh status          # 查看服务地址和状态
./run.sh open            # 打开网页
./run.sh restart         # 在当前端口重启
./run.sh stop            # 完成正在进行的工作后停止
./run.sh run             # 前台运行
./run.sh logs            # 查看近期日志
./run.sh test            # 运行 Python 测试
```

加上 `--demo`，即可操作离线演示服务：

```sh
./run.sh start --demo
./run.sh stop --demo
```

演示模式使用虚构账号和独立的数据目录，运行相同的网页、调度和操作记录逻辑。已使用的演示机会在重启后仍保持已使用状态。演示端口从 `8785` 开始选择。

需要指定端口时，账号服务使用 `CODEX_QUOTA_PORT`，演示服务使用 `CODEX_QUOTA_DEMO_PORT`：

```sh
CODEX_QUOTA_PORT=8888 ./run.sh start
```

curl 请求遵循标准的 `HTTPS_PROXY` 环境变量。网络需要代理时，在启动或重启服务时设置该变量。

## 本地数据

| 路径 | 内容 |
| --- | --- |
| `auth.json` | OAuth 凭证，权限限制为当前用户读写 |
| `state.json` | 用量缓存、预约和操作记录 |
| `.runtime.json`、`.server.lock` | 服务身份信息和实例锁 |
| `server.log` | 服务诊断日志 |
| `data/demo/` | 独立的演示凭证与状态 |

服务监听 `127.0.0.1`，校验请求来源，并要求修改操作携带本地会话令牌。导入后，页面显示账号摘要；关闭粘贴输入框时会清空原文。凭证令牌不会出现在进程参数中。请求发往固定的 ChatGPT 额度接口和 OpenAI OAuth 令牌接口。上述本地数据文件已列入 `.gitignore`。

请将凭证和状态文件作为私有数据保管。提交问题时，提供平台、错误状态以及使用演示数据复现的步骤；附件中应排除账号凭证和标识信息。

## 常见问题

- **续期后仍返回 401：** 重新登录 Codex，导入更新后的 `auth.json`。
- **返回 403 或连接失败：** 检查账号访问权限和服务的代理配置。
- **切换账号被阻止：** 先取消待执行预约，并核实结果不确定的操作。同账号的凭证可以更新。
- **重置成功，核实待完成：** 刷新页面数据或点击只读核实按钮，成功记录会继续保留。
- **无法读取状态：** 停止服务并检查文件。恢复有效的操作记录后，才能继续提交重置。

ChatGPT 账号接口可能随上游更新。请求与响应处理参考 [Codex 后端客户端](https://github.com/openai/codex/blob/5c5308fc9a9ee789049d646ef11e5400384b9c6f/codex-rs/backend-client/src/client/rate_limit_resets.rs) 和 [CLIProxyAPI 管理面板](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/bbac79d2222a0f345458203a5ab92d859f30ff30/src/features/quota/providers/codex/data.ts)。

## 开发

`core.py` 负责凭证、额度查询、操作记录和预约调度；`server.py` 提供本地 HTTP 接口；`manage.py` 管理服务进程；`demo.py` 提供模拟上游；`web/` 存放网页代码。

安装 Python 开发工具并运行检查：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
sh -n run.sh
sh -n 启动.command
```

前端格式检查和浏览器测试使用 Node.js 24+：

```sh
npm ci
npx playwright install chromium
npm run format:check
npm run test:ui
```

本机已安装 Google Chrome 时，可以运行 `PLAYWRIGHT_CHANNEL=chrome npm run test:ui`。浏览器测试会启动临时演示实例，结束后删除测试数据。Python 测试使用模拟上游响应和可控时钟，覆盖预约、重置恢复、凭证更新、本地 HTTP 访问和网页交互。

提交贡献时，请围绕一个具体改动组织代码，为行为变化补充回归测试，并同步更新两份 README。修改 Python 或前端代码后，分别运行 `ruff format .` 和 `npm run format`。

## 许可证

[MIT](LICENSE)，版权归 2026 Lian Junhong 所有。
