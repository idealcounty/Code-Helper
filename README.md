# Code Helper

Code Helper 是一个从零实现的本地 Coding Agent。它通过模型原生 Tool Calling 自主读取和修改项目文件、执行命令并验证结果，同时记录完整执行轨迹。

项目不依赖 LangChain、LangGraph、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架或 SDK。模型客户端只负责普通 API 通信，Agent Loop、上下文、工具执行、权限、终止条件与错误处理均在本仓库实现。

## 当前开发阶段

- [x] 合规仓库与 Python 项目骨架
- [x] 自研 Agent Loop、状态机与 JSONL 事件
- [x] 本地文件、搜索和命令工具
- [x] CLI 读—改—测闭环
- [x] 本地 Web API、WebSocket 事件流与基础 UI
- [x] 读取后修改、文件哈希、结构化验证证据和验证新鲜度
- [x] Git Diff、Agent 产物检查点、冲突预检与二次确认回滚
- [x] Repo Map Lite 与项目 `AGENTS.md` 规则注入
- [x] 三个项目 Skills（bug-fix、add-feature、code-review）按需加载
- [x] 动态计划工具与 Web 计划面板
- [x] Session 恢复
- [x] 上下文压缩（长输出裁剪、完整结果引用、上下文预算、历史摘要和压缩事件）
- [x] 会话恢复、证据化报告、可选桌面窗口与 PyInstaller onedir 规格
- [x] 项目级跨对话记忆（持久存储、显式写入、自动召回和遗忘）
- [x] 10 项确定性 Agent Eval、JSON/Markdown 基线与 CI 质量门禁

详细设计见 [docs/architecture.md](docs/architecture.md)。
记忆分层、边界与后续路线见 [docs/memory.md](docs/memory.md)。

## 本地配置

复制 `.env.example` 中的变量到系统环境变量或未入库的 `.env`。任何 API Key 都不得提交到仓库、README 或演示视频。

项目默认使用 DeepSeek 的 OpenAI 兼容 Chat Completions API。至少设置：

```text
CODE_HELPER_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek API Key
CODE_HELPER_BASE_URL=https://api.deepseek.com
CODE_HELPER_MODEL=deepseek-v4-flash

# 单轮最多 160 个 Agent Step；适合包含多文件生成和验证的复杂任务
CODE_HELPER_MAX_STEPS=160

# 单轮硬性运行时间（秒）；达到后以 PARTIAL 结束
CODE_HELPER_RUN_TIMEOUT=4800

# 可选：供应商返回 usage 后，在下一动作前执行 Token 预算门禁
# CODE_HELPER_TOKEN_BUDGET=50000
```

`deepseek-v4-flash` 是默认模型；需要更强能力时可改为 `deepseek-v4-pro`。两者均支持原生 Tool Calling。

### DeepSeek 思考模式

```text
# enabled / disabled；留空则使用服务端默认行为
CODE_HELPER_THINKING_MODE=enabled

# low / high / max；DeepSeek 也会把 medium、xhigh 映射为 high
CODE_HELPER_REASONING_EFFORT=high
```

思考模式的 `reasoning_content` 只作为 DeepSeek 连续工具调用所需的内部协议状态保存并回传，不会展示在 CLI、Web UI 或事件日志中。关闭思考可降低响应延迟和输出 token 消耗。

如需切换其他 OpenAI 兼容服务，可设置 `CODE_HELPER_PROVIDER=openai-compatible`，并用 `CODE_HELPER_API_KEY`、`CODE_HELPER_BASE_URL` 和 `CODE_HELPER_MODEL` 覆盖对应配置。Agent Loop 与模型服务保持解耦。

## 运行

```powershell
python -m pip install -e .

# CLI
code-helper --workspace D:\path\to\project

# 本地 Web UI
code-helper-web

# 可选桌面窗口（需要 desktop extras）
code-helper-desktop
```

## 构建 Windows 桌面版

在 Windows PowerShell 中执行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build-windows.ps1
```

构建完成后，运行 `dist/code-helper/code-helper.exe`。整个
`dist/code-helper` 目录都是发行包，不能只复制其中的 EXE；程序会从启动目录读取可选的
`.env` 配置文件。首次运行前可将发行目录中的 `.env.example` 复制为 `.env` 并填入
API Key；构建过程不会把本地真实密钥打入发行包。

Web 服务默认只监听 `127.0.0.1:8765`。打开页面后输入本地项目的绝对路径，写文件和执行命令时会要求用户批准。

## Windows 服务器最小部署

该模式适合少量可信用户共享一台 Windows 轻量应用服务器和同一个模型
API Key。它不会改变桌面版的默认行为。

1. 使用远程桌面登录服务器，安装 Python 3.11+ 和 Git。
2. 将本仓库 Clone 到 `D:\CodeHelper\Code-Helper`。
3. 在管理员 PowerShell 中第一次运行安装脚本：

```powershell
Set-Location D:\CodeHelper\Code-Helper
powershell -ExecutionPolicy Bypass -File packaging/install-windows-server.ps1
```

第一次执行会从 `.env.server.example` 创建 `.env` 后退出。编辑 `.env`，至少替换：

```dotenv
DEEPSEEK_API_KEY=你的服务器统一Key
CODE_HELPER_ACCESS_PASSWORD=一个足够长的随机访问密码
```

然后再次执行安装脚本。脚本会创建独立的非管理员账号 `CodeHelperSvc`、
Python 虚拟环境、Windows 防火墙规则和开机启动的计划任务。Agent 不会以
`Administrator` 或 `SYSTEM` 身份运行。

公开 GitHub 项目需要 Clone 到受限目录中，例如：

```powershell
git clone https://github.com/OWNER/REPOSITORY.git D:\CodeHelper\workspaces\REPOSITORY
```

远端用户访问 `http://服务器IPv4:8765`。浏览器会显示 HTTP Basic 登录框，
默认用户名是 `codehelper`，密码是 `.env` 中的
`CODE_HELPER_ACCESS_PASSWORD`。HTTP、API 和 WebSocket 使用相同认证。

服务器模式设置 `CODE_HELPER_WORKSPACE_ROOT=D:/CodeHelper/workspaces` 后，
文件夹浏览、历史会话和新会话都不能越过该目录。请在腾讯云防火墙中仅向
可信用户公网 IP 开放 TCP 8765，不要开放全部端口。由于 IP 直连使用普通
HTTP，登录信息和项目内容没有 TLS 加密；该方案只适合临时、小范围且有
来源 IP 限制的访问。获得域名后应改用 HTTPS 反向代理。

常用维护命令：

```powershell
# 查看任务状态
Get-ScheduledTask -TaskName "Code Helper Server"

# 重启后端
Stop-ScheduledTask -TaskName "Code Helper Server"
Start-ScheduledTask -TaskName "Code Helper Server"

# 查看日志
Get-Content D:\CodeHelper\Code-Helper\.server-logs\server.log -Tail 100
```

运行时会先向模型暴露 Skills 的名称、描述和触发条件；只有模型判断某个流程适用时，才调用 `load_skill` 读取对应的 `SKILL.md` 全文。复杂任务可通过 `update_plan` 创建和更新计划，Web UI 的“任务计划”页签会实时显示状态。

## 测试

```powershell
python -m pytest -q
```

测试使用脚本化假模型，不需要 API Key，也不会连接外部模型服务。

确定性 Agent Eval 同样不需要 API Key，覆盖项目问答、代码修改、审批、恢复、取消和敏感环境等十类契约：

```powershell
# 运行 Eval 并与仓库基线比较
python -m evals.runner --compare evals/reports/baseline.json

# 显式运行会消耗 API 额度的真实模型 Eval
python -m evals.runner --mode real --allow-paid --output-dir .eval-results/real
```

评测任务、指标口径、质量门禁和真实模型运行限制见 [docs/evals.md](docs/evals.md)。
