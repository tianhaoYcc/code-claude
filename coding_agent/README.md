# 第一章 初识 agent-query 循环

这一章只做一件事：把 Claude Code 最核心的 agent query 循环拆出来，用 Python 写一个小而完整的版本。

它不是完整 Claude Code，也不追求 UI、插件、MCP、子 agent 一步到位。当前目标是先讲清楚：

```text
用户输入
  ↓
模型响应
  ↓
收集 tool_use
  ↓
执行工具
  ↓
把 tool_result 放回上下文
  ↓
继续下一轮，直到没有工具调用或触发终止条件
```

## 启动方式

先在 `.env` 中配置 OpenAI-compatible 模型。`.env` 可以放在 `coding_agent/.env`、`coding_agent/agent/.env` 或运行目录的父级目录中：

```dotenv
LLM_API_KEY=你的 API Key
LLM_MODEL_ID=你的模型 ID
LLM_BASE_URL=https://example.com/v1
```

推荐在 `E:/code claude/coding_agent` 下运行：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m agent.cli "请使用 read_file 工具读取 README.md，然后用一句话总结这个项目。" --workspace . --max-turns 3 --model-client openai
```

如果只想调试本地 query loop，不调用真实模型：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m agent.cli "read README.md" --workspace . --model-client mock
```

写文件和编辑文件默认会被拒绝。调试写入能力时需要显式打开权限：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m agent.cli "请写一个 demo.txt，内容为 hello agent" --workspace . --model-client openai --write-permission allow
```

也可以使用 `--write-permission ask`，每次写入前在命令行确认。

PowerShell 默认同样会被拒绝。建议调试时优先使用 `ask`：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m agent.cli "请运行 git status" --workspace . --model-client openai --shell-permission ask --shell-timeout-seconds 30
```

`powershell` 工具有危险命令拦截、workspace cwd、超时、取消和大输出落盘，但它不是操作系统级沙箱。复杂或无法静态证明安全的命令仍应经过人工确认。

也可以直接运行入口文件，不依赖 `-m agent.cli` 的包路径：

```powershell
& "E:\Anconda\python.exe" "E:\code claude\coding_agent\agent\__main__.py" "请使用 read_file 工具读取 README.md，然后用一句话总结这个项目。" --workspace "E:\code claude\coding_agent" --max-turns 3 --model-client openai
```

运行测试：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m unittest discover -s tests
```

说明：你原先指定的 `E:\Anconda\envs\yolov8\python.exe` 当前在本机上是 0 字节损坏文件，所以 README 示例先使用已验证可用的 `E:\Anconda\python.exe`。修复 yolov8 环境后可以替换回原路径。

## 当前代码结构

- `agent/models.py`：可序列化的消息、工具块和事件。
- `agent/model_client.py`：抽象模型客户端接口。
- `agent/mock_model.py`：脚本化和启发式 mock 模型客户端。
- `agent/openai_model.py`：OpenAI-compatible Chat Completions 适配器。
- `agent/tools.py`：文件工具、路径保护、输入校验、权限策略、diff 输出和工具结果预算。
- `agent/tool_registry.py`：动态工具注册、启用/禁用和模型可见工具过滤。
- `agent/tool_orchestration.py`：只读并发、变更串行、工具事件和结果顺序保证。
- `agent/powershell_tool.py`：PowerShell 安全分类、执行、超时、取消和输出预算。
- `agent/query_loop.py`：async generator 形式的 query 循环。
- `agent/transcript.py`：append-only JSONL transcript 和严格 resume 校验。
- `agent/cli.py`：统一的 mock / OpenAI-compatible CLI 入口。

## 机制对照 Claude Code

这个版本不是凭空设计的，而是按 Claude Code 源码里的主链路缩小实现。

本章重点是 query 循环。工具安全机制已经独立成 [第二章 工具执行安全机制](../docs/chapter-02-tool-safety.md)，这里先保留和 query loop 直接相关的摘要。

### 1. QueryEngine 与 query loop 分层

Claude Code 中，`src/QueryEngine.ts` 更像会话外壳，负责处理用户输入、system prompt、工具上下文、transcript 和 SDK 输出。

真正驱动 agent 多轮运行的是 `src/query.ts` 中的 `query()` / `queryLoop()`：

```text
整理上下文
  ↓
调用模型
  ↓
收集 assistant 里的 tool_use
  ↓
执行工具
  ↓
把 tool_result 追加进 messages
  ↓
继续下一轮
```

我们的对应实现是：

- `agent/cli.py`：扮演简化版 QueryEngine，负责 CLI 参数、模型选择、transcript 初始化。
- `agent/query_loop.py`：扮演简化版 `queryLoop`，负责真正的多轮 agent 状态机。

### 2. tool_use / tool_result 协议

Claude Code 遵循模型工具调用协议：assistant 产出 `tool_use`，runtime 执行工具后，用 user message 返回 `tool_result`。

对应 Claude Code 本地源码可以看：

- `src/query.ts`：收集 assistant message 里的 `tool_use` blocks。
- `src/services/tools/toolOrchestration.ts`：调度工具执行。
- `src/services/tools/toolExecution.ts`：校验、权限、执行工具，并生成 `tool_result`。

我们的对应实现是：

- `agent/models.py`：定义 `ToolUseBlock` 和 `tool_result_block()`。
- `agent/query_loop.py`：扫描 assistant message 里的 `tool_use`，执行工具后生成 user message。
- `agent/tool_registry.py`：确定模型可以看到和执行器可以查找的工具集合。
- `agent/tool_orchestration.py`：执行工具并生成按原调用顺序排列的结果。
- `agent/tools.py`：定义工具基类、输入校验、路径保护和结果预算。

简化后的协议是：

```text
AssistantMessage.content[]:
  { type: "tool_use", id, name, input }

UserMessage.content[]:
  { type: "tool_result", tool_use_id, content, is_error? }
```

这和 Claude Code 的核心结构一致。

### 3. 工具参数不能信任模型

Claude Code 在 `toolExecution.ts` 里不会直接相信模型参数，而是先用每个工具的 `inputSchema.safeParse(input)` 做 Zod 校验；失败时不会让程序崩，而是返回 `tool_result(is_error=true)` 给模型。

我们的当前版本做了 stdlib-only 的简化版：

- `agent/tools.py` 中每个工具有 `input_schema`。
- `agent/openai_model.py` 会尝试修复 fenced JSON、Python dict 字面量、`{}{"path": "."}` 这类连续 JSON object。
- `normalize_input()` 支持 `path -> file_path` 这类参数别名，以及字符串数字/布尔值的基础转换。
- `validate_input()` 检查 required 字段和基础类型。
- schema 错误会被包装成 `is_error=true` 的 `tool_result`，让模型下一轮有机会修正参数。
- `QueryLoopConfig.max_bad_tool_input_attempts` 默认是 3，超过后用 `bad_tool_arguments` 终止，避免坏参数死循环。
- 坏参数、未知工具、路径越界都会被包装成 error `tool_result`。

这就是为什么真实模型工具参数不稳定时，query loop 仍然能继续，而不是直接崩溃。

### 4. 权限系统

Claude Code 的工具执行不会只靠“路径在 workspace 内”这一层防线，还会结合工具类型、用户配置和运行时确认来决定能不能执行。

我们的当前版本先实现一个最小权限模型：

- `read_permission`：控制 `read_file`、`list_dir`、`glob`、`grep`。
- `write_permission`：控制 `write_file`、`edit_file`。
- `shell_permission`：控制 `powershell`。
- 每类权限支持 `allow`、`deny`、`ask`。

默认策略是读允许、写拒绝、Shell 拒绝：

```text
read=allow
write=deny
shell=deny
```

CLI 对应参数是：

```powershell
--read-permission allow
--write-permission deny
--shell-permission deny
```

### 5. OpenAI-compatible 适配层

Claude Code 内部使用自己的消息和工具抽象，再在 API 层映射到模型服务需要的格式。

我们的 `agent/openai_model.py` 也做同样的事情：

- 把内部工具 `Tool` 转成 OpenAI-compatible `tools`。
- 把 OpenAI `tool_calls` 转回内部 `ToolUseBlock`。
- 把内部 `tool_result` 转成 OpenAI 的 `role: "tool"` 消息。

这样 `QueryLoop` 不关心底层模型是 mock 还是 OpenAI-compatible，只关心统一的内部 `AssistantMessage` 和 `ToolUseBlock`。

### 6. transcript 与 resume

Claude Code 用 append-only JSONL transcript 保存会话，并在 resume 时重建当前有效消息链。

我们的 `agent/transcript.py` 实现了最小版：

- 每个事件追加写入 JSONL。
- 可以加载历史 messages。
- resume 时用 `ensure_tool_result_pairing()` 严格校验工具调用配对。

这对应 Claude Code 在 `src/utils/messages.ts` 中对 `tool_use` / `tool_result` 配对进行修复或严格检查的思路。

## 当前版本支持的工具

默认工具由 `default_tools()` 创建，再进入 `ToolRegistry`；运行时也可以通过 `register_tool()` 增加或替换工具：

- `read_file`：读取工作区内文本文件。
- `list_dir`：列出工作区目录。
- `glob`：按 glob 查找文件。
- `grep`：搜索文件内容。
- `write_file`：写入工作区内文本文件，并返回 unified diff；覆盖已有文件必须显式传 `overwrite=true`。
- `edit_file`：用 `old_text` / `new_text` 精确替换文件内容，并返回 unified diff。
- `powershell`：在 workspace cwd 下执行 PowerShell，包含危险命令拦截、权限、超时、取消和输出预算。

所有文件路径都会通过 workspace guard，默认不能访问 `--workspace` 外部路径。写入、编辑和 PowerShell 还需要通过各自权限检查。blanket-deny 的工具不会发送给模型，显式禁用的工具即使被模型调用也不会执行。

## 当前版本边界

还没有实现：

- 上下文压缩
- 记忆系统
- ToolSearch
- MCP 工具发现与动态加载
- 操作系统级 Shell sandbox 和完整 PowerShell AST 校验
- 更完整的权限确认 UI 与权限持久化
- Plan mode
- 子 agent

这些会放到后续章节继续补。
