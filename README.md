# Code Helper

Code Helper 是一个从零实现的本地 Coding Agent。它通过模型原生 Tool Calling 自主读取和修改项目文件、执行命令并验证结果，同时记录完整执行轨迹。

项目不依赖 LangChain、LangGraph、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架或 SDK。模型客户端只负责普通 API 通信，Agent Loop、上下文、工具执行、权限、终止条件与错误处理均在本仓库实现。

## 当前开发阶段

- [x] 合规仓库与 Python 项目骨架
- [x] 自研 Agent Loop、状态机与 JSONL 事件
- [x] 本地文件、搜索和命令工具
- [x] CLI 读—改—测闭环
- [x] 本地 Web API、WebSocket 事件流与基础 UI
- [x] 读取后修改、文件哈希和验证新鲜度
- [ ] Diff 工具、轻量检查点与 Session 恢复
- [ ] 上下文压缩、Skills 和 Repo Map Lite

详细设计见 [docs/architecture.md](docs/architecture.md)。

## 本地配置

复制 `.env.example` 中的变量到系统环境变量或未入库的 `.env`。任何 API Key 都不得提交到仓库、README 或演示视频。

至少设置：

```text
CODE_HELPER_API_KEY=你的模型服务密钥
CODE_HELPER_BASE_URL=https://api.openai.com/v1
CODE_HELPER_MODEL=支持原生 Tool Calling 的模型名称
```

## 运行

```powershell
python -m pip install -e .

# CLI
code-helper --workspace D:\path\to\project

# 本地 Web UI
code-helper-web
```

Web 服务只监听 `127.0.0.1:8765`。打开页面后输入本地项目的绝对路径，写文件和执行命令时会要求用户批准。

## 测试

```powershell
python -m pytest -q
```

测试使用脚本化假模型，不需要 API Key，也不会连接外部模型服务。
