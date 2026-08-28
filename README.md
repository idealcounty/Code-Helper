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

# 单轮硬性运行时间（秒）；达到后以 PARTIAL 结束
CODE_HELPER_RUN_TIMEOUT=600

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

Web 服务只监听 `127.0.0.1:8765`。打开页面后输入本地项目的绝对路径，写文件和执行命令时会要求用户批准。

运行时会先向模型暴露 Skills 的名称、描述和触发条件；只有模型判断某个流程适用时，才调用 `load_skill` 读取对应的 `SKILL.md` 全文。复杂任务可通过 `update_plan` 创建和更新计划，Web UI 的“任务计划”页签会实时显示状态。

## 测试

```powershell
python -m pytest -q
```

测试使用脚本化假模型，不需要 API Key，也不会连接外部模型服务。
