# Agent 学习仓库

这个仓库的初衷很简单：给想做 agent 开发的中国大学生找实习一个能跑、能读、能改的学习项目。

我们不是直接复刻一个完整 Claude Code，而是把 Claude Code 里最值得面试时讲清楚的机制拆出来，做成小版本、可测试、可对照源码的 Python 实现。目标是让你不仅能说“我用过 coding agent”，还能解释 query 循环、tool use、tool result、上下文、记忆、权限和工具注册这些底层机制。面试时不慌，甚至可以反过来拷打面试官：这个 agent 到底是怎么跑起来的？

## 章节

- [第一章 初识 agent-query 循环](coding_agent/README.md)
- [第二章 工具执行安全机制：让 Agent 有手，但不乱动手](docs/chapter-02-tool-safety.md)
- [第三章 上下文压缩：让 Agent 在长对话中继续工作](docs/chapter-03-context-compaction.md)
- [第四章 双层记忆：让 Agent 记得住，也压得动](docs/chapter-04-memory-system.md)
- [第五章 Plan Mode 与子 Agent：先规划，再协作](docs/chapter-05-plan-mode-and-subagents.md)

## 当前版本

当前版本覆盖前五章的基础实现：先让 agent query loop 跑起来，再补齐工具执行安全、长对话上下文压缩、双层记忆、Plan mode 和前台子 Agent 协作。

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
- `summary.md` 会话滚动检查点和 session-memory compact 优先路径
- `MEMORY.md` 长期索引、topic 文件和最多 3 个相关 topic 的确定性召回
- 隔离的 session/durable memory 子 QueryLoop、受限工具注册表和后台任务合并
- UUID 增量游标、resume 恢复、原子状态写入、敏感信息过滤和失败降级
- `MemoryEvent` 事件流，以及 CLI 退出前的 Worker flush
- `execute` / `plan` 模式切换、计划落盘、批准/拒绝和 resume 恢复
- Plan mode 只允许修改当前 session 的 `plan.md`，并在执行前复查工具可见性和路径权限
- `explore`、`plan`、`general-purpose` 三类前台子 Agent，以及独立 QueryLoop 和 transcript
- 只读子 Agent 有界并发、通用子 Agent 串行、取消传播、超时和错误降级
- 单元测试覆盖核心路径

后续章节会继续补后台 Agent、任务列表与 DAG、Agent resume、worktree、MCP 和 ToolSearch。
