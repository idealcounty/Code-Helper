# Code Helper

Code Helper 是一个从零实现的本地 Coding Agent。它通过模型原生 Tool Calling 自主读取和修改项目文件、执行命令并验证结果，同时记录完整执行轨迹。

项目不依赖 LangChain、LangGraph、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架或 SDK。模型客户端只负责普通 API 通信，Agent Loop、上下文、工具执行、权限、终止条件与错误处理均在本仓库实现。

## 当前开发阶段

- [x] 合规仓库与 Python 项目骨架
- [ ] 自研 Agent Loop 与事件模型
- [ ] 本地文件和命令工具
- [ ] CLI 完整闭环
- [ ] 本地 Web UI
- [ ] 安全编辑、自动验证与检查点

详细设计见 [docs/architecture.md](docs/architecture.md)。

## 本地配置

复制 `.env.example` 中的变量到系统环境变量或未入库的 `.env`。任何 API Key 都不得提交到仓库、README 或演示视频。
