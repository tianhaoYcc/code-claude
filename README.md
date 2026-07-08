# Agent 学习仓库

这个仓库的初衷很简单：给想做 agent 开发的中国实习生一个能跑、能读、能改的学习项目。

我们不是直接复刻一个完整 Claude Code，而是把 Claude Code 里最值得面试时讲清楚的机制拆出来，做成小版本、可测试、可对照源码的 Python 实现。目标是让你不仅能说“我用过 coding agent”，还能解释 query 循环、tool use、tool result、上下文、记忆、权限和工具注册这些底层机制。面试时不慌，甚至可以反过来拷打面试官：这个 agent 到底是怎么跑起来的？

## 章节

- [第一章 初识 agent-query 循环](coding_agent/README.md)
- 第二章 上下文压缩：待补充
- 第三章 记忆系统：待补充
- 第四章 工具注册与 ToolSearch：待补充
- 第五章 权限、Shell 与 Plan Mode：待补充

## 当前版本

当前版本聚焦第一章：实现一个最小但完整的 agent query loop。

它已经支持：

- OpenAI-compatible 大模型调用
- mock 模型调试
- `tool_use` / `tool_result` 循环
- `read_file`、`list_dir`、`glob`、`grep` 四个工具
- workspace 路径保护
- 大工具结果落盘与预览
- append-only transcript
- resume 时严格校验工具配对
- 单元测试覆盖核心路径

后续章节会继续补上下文压缩、记忆系统、动态工具注册、权限系统、写文件/编辑工具、Shell 工具和 Plan mode。
