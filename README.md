# Agent 学习仓库

这个仓库的初衷很简单：给想做 agent 开发的中国大学生找实习一个能跑、能读、能改的学习项目。

我们不是直接复刻一个完整 Claude Code，而是把 Claude Code 里最值得面试时讲清楚的机制拆出来，做成小版本、可测试、可对照源码的 Python 实现。目标是让你不仅能说“我用过 coding agent”，还能解释 query 循环、tool use、tool result、上下文、记忆、权限和工具注册这些底层机制。面试时不慌，甚至可以反过来拷打面试官：这个 agent 到底是怎么跑起来的？

## 章节

- [第一章 初识 agent-query 循环](coding_agent/README.md)
- [第二章 工具执行安全机制：让 Agent 有手，但不乱动手](docs/chapter-02-tool-safety.md)
- [第三章 上下文压缩：让 Agent 在长对话中继续工作](docs/chapter-03-context-compaction.md)
- 第四章 记忆系统：待补充
- 第五章 规划与复杂任务执行：待补充

## 当前版本

当前版本覆盖前三章的基础实现：先让 agent query loop 跑起来，再补齐工具执行安全和长对话上下文压缩。

它已经支持：

- OpenAI-compatible 大模型调用
- mock 模型调试
- `tool_use` / `tool_result` 循环
- `read_file`、`list_dir`、`glob`、`grep`、`write_file`、`edit_file`、`powershell` 七个工具
- malformed tool arguments 修复、schema 错误重试和坏参数次数上限
- read/write/shell 三类 allow / deny / ask 权限策略
- 写文件与编辑文件后的 unified diff 输出
- 动态工具注册、启用/禁用和按权限过滤模型可见工具
- 只读工具并发、写入与非只读 Shell 串行、取消和结果顺序保证
- PowerShell 危险命令拦截、workspace cwd、超时和输出预算
- workspace 路径保护
- 大工具结果落盘与预览
- append-only transcript
- resume 时严格校验工具配对
- OpenAI-compatible token usage 记录与本地保守 token 估算
- 完整 transcript 与模型活跃上下文分离
- microcompact 旧工具结果、完整摘要和 compact boundary
- 自动压缩、手动压缩、prompt-too-long 恢复和三次失败熔断
- 单元测试覆盖核心路径

后续章节会继续补记忆系统、Plan mode、子 agent、MCP 和 ToolSearch。
