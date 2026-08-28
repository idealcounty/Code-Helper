# 两分钟演示脚本

使用 `examples/demo_project` 作为工作区，提前准备好 DeepSeek API Key，但不要在录屏中展示配置文件或密钥。

1. **0:00–0:15**：启动 `code-helper-web`，展示银白色 Web UI、资源管理器和 Ask/Plan/Act 选择器。
2. **0:15–0:35**：选择 Plan，输入“修复 greet 返回格式并通过测试”，展示 Agent 读取文件、生成动态计划和执行轨迹。
3. **0:35–1:05**：切换 Act 或按界面提示批准修改，展示 `apply_patch`、检查点和 Diff。
4. **1:05–1:30**：Agent 调用 `run_command` 执行 pytest，展示命令输出和验证结果；若失败，展示有限次自动修复。
5. **1:30–1:50**：打开任务计划、Diff 和执行轨迹页签，展示工具统计与上下文压缩事件（如有）。
6. **1:50–2:00**：展示最终状态和 `/api/sessions/{session_id}/report` 返回的证据字段，说明没有凭空声称完成。

录制前检查：视频为 MP4、时长不超过 2 分钟、文件不超过 200MB；确认画面中没有 API Key、Token 或个人隐私。

完成录制后，在仓库根目录执行：

```powershell
powershell -File packaging/prepare-submission.ps1 -Name 你的姓名 -VideoPath 演示视频.mp4
```
