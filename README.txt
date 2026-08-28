Code Helper 是一个不依赖 LangChain、LangGraph 或其他 Agent SDK 的本地 Coding Agent。它使用 DeepSeek 原生 Tool Calling，自研 Agent Loop、上下文管理、权限审批、文件哈希校验、检查点回滚、Repo Map、Skills、动态计划和自动验证。

运行：
1. 安装 Python 3.11+。
2. 执行 `python -m pip install -e .[dev]`。
3. 设置 `DEEPSEEK_API_KEY`，可选设置 `CODE_HELPER_MODEL`、`CODE_HELPER_THINKING_MODE` 和 `CODE_HELPER_REASONING_EFFORT`。
4. CLI：`code-helper --workspace 项目路径`；Web：`code-helper-web`，浏览器打开 http://127.0.0.1:8765。

演示任务：让 Agent 读取一个 Python 文件，将指定函数改为目标行为，运行测试并查看 Diff；Act 模式下写入和命令执行需要用户批准。

仓库：https://github.com/idealcounty/Code-Helper

提交材料整理：在仓库根目录执行 `powershell -File packaging/prepare-submission.ps1 -Name 你的姓名 -VideoPath 演示视频.mp4`，脚本会校验 MP4/200MB 限制并仅打包 README.txt 与视频。
