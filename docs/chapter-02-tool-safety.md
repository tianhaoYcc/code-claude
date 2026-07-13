# 第二章 工具执行安全机制：让 Agent 有手，但不乱动手

第一章解决的是 agent 怎么循环。第二章解决的是另一个更工程化的问题：

```text
模型已经能调用工具了，那我们怎么保证它不会乱读、乱写、乱执行命令？
```

Claude Code 的核心经验是：工具不是模型直接执行的，而是经过一层 tool runtime。这个 runtime 负责参数校验、权限判断、路径边界、执行调度、结果截断、审计记录，再把结果包装成 `tool_result` 交回 query loop。

## 和 Claude Code 的对应主线

可以对照本地 Claude Code 源码中的这些位置阅读：

- `src/query.ts`：从 assistant message 中收集 `tool_use`，等待工具结果后继续下一轮。
- `src/services/tools/toolOrchestration.ts`：工具调度层，决定怎么执行一批工具调用。
- `src/services/tools/toolExecution.ts`：工具执行层，负责 schema 校验、权限、错误包装和 `tool_result`。
- `src/types/permissions.ts`：权限模式、权限行为和权限规则的类型定义。
- `src/components/permissions/`：权限确认 UI。
- `src/tools/FileReadTool/`、`src/tools/FileWriteTool/`、`src/tools/FileEditTool/`：文件工具。
- `src/tools/BashTool/`、`src/tools/PowerShellTool/`：Shell 工具、安全校验和只读命令识别。

Python 版当前对应实现：

- `coding_agent/agent/tools.py`：工具基类、输入校验、路径保护、权限策略、文件工具和 diff。
- `coding_agent/agent/tool_registry.py`：动态注册、启用/禁用和按权限过滤模型可见工具。
- `coding_agent/agent/tool_orchestration.py`：只读工具并发、有副作用工具串行、事件流和结果排序。
- `coding_agent/agent/powershell_tool.py`：PowerShell 分类、危险命令拦截、路径检查、超时、取消和输出预算。
- `coding_agent/agent/query_loop.py`：把工具错误包装成 `tool_result`，并维护坏参数次数上限。
- `coding_agent/agent/openai_model.py`：修复 OpenAI-compatible 模型可能返回的 malformed arguments。
- `coding_agent/agent/cli.py`：暴露 read/write/shell 权限开关。

## 1. Tool 安全威胁模型

coding agent 的风险不是“模型会不会回答错”，而是“模型回答错之后会不会真的动手造成损害”。

这一章先把风险拆成几类：

- 参数错误：模型传 `{}`、`{"path": "."}`、`{}{"path": "."}`，或者类型不对。
- 路径越界：模型试图读取 workspace 外的文件。
- 写入风险：模型覆盖用户文件，或者写入用户没预期的路径。
- Shell 风险：模型执行删除、移动、网络、权限提升、长时间运行等命令。
- 上下文风险：工具输出过大，把上下文挤爆。
- 审计风险：执行过什么工具、写了什么文件、为什么失败，如果没有记录就无法复盘。

Python 版现在已经把这些风险的 v1 防线接进同一条工具执行链。它仍然不是操作系统级沙箱，尤其不能把字符串规则误认为绝对安全，但默认拒绝和人工确认能让风险停在执行前。

## 2. 工具注册与模型可见性

工具安全的第一步不是执行时拒绝，而是决定哪些工具应该出现在模型的 tool schema 中。

Claude Code 的工具池主线是：

```text
getAllBaseTools()
  ↓
getTools(permissionContext)
  ↓
filterToolsByDenyRules(...)
  ↓
toolUseContext.options.tools
  ↓
同时交给模型和工具执行器
```

相关实现可以对照 `src/tools.ts`。其中 `getAllBaseTools()` 是完整工具源，`getTools()` 再根据运行模式、功能开关和 deny rules 过滤。这样一个被整类禁止的工具不会继续占用 schema token，也不会诱导模型反复发起必然失败的调用。

Python 版增加了 `ToolRegistry`：

- `register(tool)`：运行时注册工具，重名默认报错。
- `register(tool, replace=True)`：显式替换同名工具。
- `enable(name)` / `disable(name)`：控制工具是否对模型和执行器可用。
- `available_tools(permission_policy)`：过滤未启用工具和 blanket-deny 工具。
- `QueryLoop.register_tool()`：query loop 创建后仍可动态扩展工具。

这里保留两个工具集合概念：

```text
完整 registry：处理模型可能产生的未知、禁用或越权调用
模型可见 tools：只包含 enabled 且没有被整类 deny 的工具
```

例如默认 `write=deny` 时，`write_file` 和 `edit_file` 不会发给模型；如果模型仍然幻觉调用它们，执行层仍会返回明确的错误 `tool_result`，而不是找不到内部安全策略。

## 3. 参数安全

工具参数不能直接信模型。Claude Code 在工具执行前会做 schema 校验，失败时不让程序崩，而是返回错误 `tool_result`，让模型下一轮修正。

Python 版当前分三步处理：

```text
模型原始 arguments
  ↓
parse_tool_arguments 尝试修复结构
  ↓
normalize_input 做参数别名和基础类型转换
  ↓
validate_input 做 required/type 校验
```

当前支持的修复：

- 去掉 Markdown fenced JSON 外壳。
- 解析 Python dict 字面量，例如 `{'file_path': 'README.md'}`。
- 修复连续 JSON object，例如 `{}{"path": "."}`。
- 参数别名，例如 `path -> file_path`、`file -> file_path`、`text -> content`。
- 基础类型转换，例如 `"100" -> 100`、`"true" -> True`。

修不了的参数不会瞎猜。比如 `read_file` 收到 `{}`，缺少 `file_path`，会返回 error `tool_result`。query loop 会给模型重试机会，但 `max_bad_tool_input_attempts` 默认是 3，超过后用 `bad_tool_arguments` 终止，避免坏参数死循环。

## 4. 路径安全

路径安全的第一原则：工具只能访问 workspace root 内的路径。

Python 版当前做法：

```text
workspace_root.resolve()
  ↓
用户传入路径转绝对路径
  ↓
candidate.resolve()
  ↓
检查 resolved.relative_to(root)
```

这能拦住：

- `../outside.txt`
- workspace 外部绝对路径
- 大多数通过 `..` 绕过边界的情况

注意：更完整的实现还要继续考虑 symlink 策略、多 workspace 目录、只读目录、临时目录和系统目录。Claude Code 里相关权限范围会比这个 v1 更细。

## 5. 权限系统

只有 workspace guard 还不够。一个路径在 workspace 里，不代表模型可以随便改。

当前 Python 版有三类权限：

```text
read
write
shell
```

每类权限有三种行为：

```text
allow：直接允许
deny：直接拒绝
ask：运行时询问用户
```

默认策略是：

```text
read=allow
write=deny
shell=deny
```

CLI 参数：

```powershell
--read-permission allow
--write-permission deny
--shell-permission deny
```

这个设计是 Claude Code 权限体系的缩小版。Claude Code 还有更丰富的 permission mode、permission rules、持久化规则、按工具/命令/路径的确认 UI。Python 版先把三态权限和工具执行链路打通。

## 6. 写入安全

coding agent 不能只有读工具。要真正能改代码，至少需要写入和编辑工具。

当前 Python 版支持：

- `write_file`：写入文件，返回 unified diff。
- `edit_file`：用 `old_text` / `new_text` 精确替换，返回 unified diff。

安全默认值：

- 写权限默认是 `deny`。
- `write_file` 新建文件可以写，但覆盖已有文件必须显式传 `overwrite=true`。
- `edit_file` 必须匹配到 `old_text`，否则拒绝修改。
- 每次写入和编辑都会返回 diff，方便模型和用户复盘。

`edit_file` 比纯 `write_file` 更适合 coding agent，因为它要求模型明确指出旧文本，能降低误覆盖整文件的概率。

## 7. PowerShell 安全

Claude Code 没有把 Shell 当成一个简单的 `subprocess(command)`。以 `src/tools/PowerShellTool/PowerShellTool.tsx` 为主线，它会分别处理：

```text
inputSchema
  ↓
validateInput
  ↓
checkPermissions
  ↓
call
  ↓
mapToolResultToToolResultBlockParam
```

Python v1 的 `powershell` 工具采用同样的分层思路，并实现：

- 固定使用 workspace root 作为初始 cwd。
- 使用 `-NoProfile -NonInteractive` 启动 PowerShell，减少配置和交互带来的不确定性。
- 识别 `read_only`、`mutating`、`network`、`dangerous`、`unknown` 五类命令。
- 删除命令、磁盘命令、嵌套 PowerShell、encoded command、`git reset --hard`、`git clean -f`、force push 等危险模式直接拒绝。
- 显式出现的 workspace 外绝对路径或 `..` 越界路径直接拒绝。
- 所有可执行命令仍需通过 `shell_permission` 的 allow / deny / ask 策略。
- 默认超时 30 秒，允许单次调用在 1 到 600 秒之间调整。
- query loop 取消时终止正在运行的进程；Windows 下尝试终止整个进程树。
- stdout、stderr、exit code 分开记录；非零退出码返回 `is_error=true`。
- 输出超过 Shell 预算后写入 `.agent_outputs/`，上下文只保留路径和预览。

CLI 示例：

```powershell
& "E:\Anconda\python.exe" -m agent.cli `
  "运行 git status 检查仓库" `
  --workspace . `
  --model-client openai `
  --shell-permission ask `
  --shell-timeout-seconds 30
```

必须明确：这个 v1 是“默认拒绝 + 静态检查 + 权限门”，不是 OS sandbox。动态变量、复杂 PowerShell AST、子进程内部行为无法只靠正则完全证明安全。无法静态证明的命令被标记为 `unknown`，不会获得只读并发待遇，并继续受 shell 权限控制。

## 8. 工具编排：读并发，写串行

Claude Code 在 `src/services/tools/toolOrchestration.ts` 中先调用 schema parser，再调用每个工具的 `isConcurrencySafe()`，把一批调用切成：

```text
连续的 concurrency-safe 工具 → 有上限地并发执行
非 concurrency-safe 工具     → 一个一个串行执行
```

Python 版的 `ToolOrchestrator` 现在做同样的事情：

- 只有参数能成功归一化和校验的工具才有资格判断并发安全。
- `read_file`、`list_dir`、`glob`、`grep` 可以并发。
- `write_file`、`edit_file` 必须串行。
- PowerShell 只有被识别为 `read_only` 时才允许并发，其余命令串行。
- 默认最大并发数为 10，可以用 `--max-tool-concurrency` 调整。
- `started`、`finished`、`errored`、`cancelled` 事件会真正从 query loop yield 给 CLI，同时写入 transcript。

并发完成顺序可以变化，但协议顺序不能变化。例如第二个 read 先完成，最终注入模型的 `tool_result` 仍然按原始 `tool_use` 顺序排列。这能同时满足性能和消息配对不变量。

## 9. 审计与可恢复

安全机制最后一定要落到记录上。

当前 Python 版已经有 append-only transcript：

- user message
- assistant message
- tool event
- tool_result
- terminal result

写工具还会把 diff 放进 tool result。后续可以继续增强：

- 写前备份。
- undo 文件。
- 权限决策记录。
- 参数修复记录。
- 工具输出落盘索引。
- 每轮 agent 行为摘要。

审计不是锦上添花。agent 一旦能写文件和执行 shell，审计就是安全机制的一部分。

## 本章小结

第二章的核心不是多加几个工具，而是建立一条工具安全流水线：

```text
tool_use
  ↓
registry 过滤模型可见工具
  ↓
参数修复
  ↓
schema 校验
  ↓
路径边界
  ↓
权限判断
  ↓
只读并发 / 变更串行
  ↓
工具执行与取消
  ↓
结果预算
  ↓
tool_result
  ↓
transcript 审计
```

这条线跑通后，agent 才从“会调用工具的 demo”变成“可以逐步扩展成 coding agent 的工程骨架”。
