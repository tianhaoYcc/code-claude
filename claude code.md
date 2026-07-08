# claude code

# 上下文机制

1\.压缩边界

```Plain Text
旧消息
旧消息
compact_boundary
压缩摘要
保留的近期消息
新消息
```

2\.snip

假设历史是：

```Plain Text
A 用户提出任务
B Agent 读取大量文件
C Agent 尝试错误方案
D 工具返回几万字日志
E 找到真正原因
F 当前正在修改代码
```

`HISTORY_SNIP` 可以选择移除：

```Plain Text
C 错误方案
D 大量无用日志
```

保留：

```Plain Text
A、B、E、F
```

## snip实现过程

### 给消息添加 ID

开启后，发送给模型的用户消息会被添加类似 ID 的标记：

```Plain Text
[id: abc123]
```

相关代码在 `appendMessageTagToUserMessage()` 附近。

这样模型使用 `SnipTool` 时，可以准确指定要删除哪些历史消息，而不是靠文本匹配。

### 给 Agent 注册 `SnipTool`

```Plain Text
const SnipTool = feature('HISTORY_SNIP')
  ? require('./tools/SnipTool/SnipTool.js').SnipTool
  : null
```

随后放入工具列表：

```Plain Text
...(SnipTool ? [SnipTool] : [])
```

因此开启这个功能后，Snip 就像 Read、Edit、Bash 一样，是 Agent 可以调用的工具。

### 提醒 Agent清理上下文

当上下文持续增长但长期没有执行 snip 时，系统会注入 `context_efficiency` 提醒。

代码注释显示，大致每增长一定 token 数量就可能提醒一次，让 Agent考虑删除不再需要的历史。

### 在主循环中应用剪枝

每轮请求模型前：

```Plain Text
const snipResult =
  snipModule!.snipCompactIfNeeded(messagesForQuery)

messagesForQuery = snipResult.messages
```

它会根据之前的 Snip 操作生成新的模型上下文，并计算：

```Plain Text
snipResult.tokensFreed
```

也就是这次剪枝节省了多少 token。

### UI 和模型看到的历史可以不同

这是它最重要的设计之一。

在交互式 REPL 中：

```Plain Text
UI：仍然可以滚动查看完整历史
模型：只看到排除 snipped 消息后的历史
```

实现位置就是你刚看的：

```Plain Text
return projectSnippedView(sliced)
```

因此某条消息可能还显示在界面上，但已经不属于 Agent 的“活动上下文”。

### 恢复会话时重新执行删除

Snip 边界会记录：

```Plain Text
removedUuids
```

恢复会话时，系统根据这些 UUID 删除相应消息，并重新连接消息的 `parentUuid` 链，防止被剪掉的内容重新进入上下文。



## 工具上下文

原消息

↓

检查工具结果总大小

↓

没有超限 → 原样返回

↓

超限 → 大结果写入文件

↓

原内容替换成路径和预览

↓

返回更小的消息数组



# Memory机制

### 做成了“多层文件化记忆系统”

memory 的最底层是目录，不是数据库。

这说明系统把 `MEMORY.md` 当成入口索引文件，而不是正文存储文件。

设计意图很明确：

- 每条 memory 应该单独写成一个 markdown 文件

- `MEMORY.md` 只维护索引链接和一行描述

- agent / model 在 prompt 里默认只保证看到 `MEMORY.md`

- 需要更细节时，再去读具体 memory 文件



`loadMemoryPrompt()` 会生成一段系统提示词，告诉模型：

- memory 目录在哪里

- 哪些内容应该保存

- 哪些内容不应该保存

- 如何创建 topic memory 文件

- 如何更新 `MEMORY.md`

- 什么时候应该读取 memory

- memory 与 plan、task 的区别

最终在 `queryLoop` 中：

```Plain Text
const fullSystemPrompt = asSystemPrompt(
  appendSystemContext(systemPrompt, systemContext),
)
```

发送模型：

```Plain Text
deps.callModel({
  systemPrompt: fullSystemPrompt,
  // ...
})
```

这一部分主要是“memory 操作说明”，不一定包含具体 memory 内容。

![image\.png](图片和附件/image.png)



Auto Memory 的更新不是靠当前主线程 agent 每次都手动完成，而是可以由后台提取流程补全。

它的目标是把长期有效的信息沉淀成 durable memories，例如：

- 用户长期偏好

- 项目外部上下文

- 非代码内生知识

- 需要跨会话延续的协作约束

这一层是“全局持久记忆基座”，而 Agent Memory 更像“某个 agent 的专属长期记忆”。



### 从 `query` 出发，这个项目把 memory 放进上下文有两条主要路径：

#### 把“如何使用 memory”的规则放进 `systemPrompt`：

1\.在启动查询之前把memory的机制放进系统提示词里









#### 把“具体 memory 内容”放进模型消息中：

##### 模式A：如果相关动态检索功能没有开启，

`getUserContext()` 会读取 memory 文件，并把memory\.md注入到提示词里

##### 模式B：异步检索memory

入 `queryLoop` 后、进入 `while` 之前：

```Plain Text
startRelevantMemoryPrefetch这个函数异步检索memory
```

找到用户query后进行检索，在检索时递归扫描 `.md` 文件

生成以下类似清单

```Plain Text
读取每个文件前 30 行 frontmatter
获取 filename
获取 description
获取 memory 类型
获取修改时间
最多考虑 200 个文件

- [feedback] feedback_testing.md: User testing preferences
- [project] architecture.md: Important architecture decisions
- [reference] playwright.md: Playwright usage notes
```

然后使用一个小的query进行选择，这个工作和工具调用是异步的。之后的预取memory会去重，去除使用过或处理的memory，之后将这个memory构造成

```Plain Text
const msg = createAttachmentMessage(memAttachment)

yield msg
toolResults.push(msg)//放进下一轮工作的上下文
```

### Session Memory：当前会话摘要层

### 7\.1 Agent Memory 的定位

Agent Memory 和 Auto Memory 的区别在于：

- Auto Memory 是“用户 / 项目维度”的长期记忆

- Agent Memory 是“某种 agent 类型”的长期记忆

也就是说，它不是“当前会话记住什么”，而是“这个 agent 以后应该长期知道什么”。

这使得 agent 不再只是一个静态 prompt 模板，而是：

```Plain Text
agent =
  静态角色 prompt
  + memory scope
  + memory 目录
  + 可写 memory tools
  + snapshot 初始化能力
```

### 7\.2 三种 scope

在 [`src/tools/AgentTool/agentMemory.ts`](https://github.com/liuup/claude-code-analysis/blob/main/src/tools/AgentTool/agentMemory.ts) 中，`AgentMemoryScope` 明确只有三种：

- `user`

- `project`

- `local`

含义分别是：

- `user` 跨项目复用的 agent 长期记忆

- `project` 当前项目内共享的 agent 记忆

- `local` 当前项目、当前机器或当前挂载环境的本地 agent 记忆





agent上下文总结的死锁

上下文太大

↓

必须总结才能缩小

↓

总结请求也需要携带完整上下文

↓

总结请求因为上下文太大而无法运行

↓

无法产生摘要

↓

上下文仍然太大



## 子agent

utils\\forkedAgent\.ts会复用主agent的query循环，但子agent的权限很小，并且主agent在分发子agent通过sequential保证同一文件，并且利用

if \(querySource \!== 'repl\_main\_thread'\) \{

return

\}判断防止套娃



子agent会有完整的主对话，但是权限很小，且被隔离，不会影响主agent，有自己的sidechain transcript



它不是通过普通 `AgentTool` 启动，而是内部直接调用 `runForkedAgent()`。



这是本项目 Compaction 中最具亮点的工程设计。若系统已开启并处于一段长会话中，它不会再去调用额外的 API 浪费 Token 给模型总结大意，而是直接调用 [`trySessionMemoryCompaction`](https://github.com/liuup/claude-code-analysis/blob/main/src/services/compact/sessionMemoryCompact.ts)，读取后台提取记忆的子 Agent 最新沉淀的 Session Memory 文件直接充当上下文断点（SummaryMessage）。



# AttachementMessage

## 两个主要调用时机

### 用户刚提交输入时

`processUserInput.ts` 调用：

```Plain Text
getAttachmentMessages(
  inputString,
  context,
  ideSelection,
  [],
  messages,
)
```

这时会处理：

```Plain Text
用户输入 + @文件 + IDE 选择 + Skill discovery
```

### ReAct 工具轮次之间

`query.ts` 调用：

```Plain Text
getAttachmentMessages(
  null,
  updatedToolUseContext,
  null,
  queuedCommandsSnapshot,
  messages,
)
```

因为 `input` 是 `null`，不会再次处理 `@` 文件，但会收集：

```Plain Text
排队消息
文件变化
任务通知
Memory/Skill
工具变化
系统提醒
```

然后放进 `toolResults`，供下一轮模型调用看到。

一句话概括：

> `getAttachments()` 在每次用户输入或 Agent 工具轮次之间，把散落在文件、IDE、任务、Skill、MCP 和应用状态中的额外信息统一收集成附件，随后注入模型上下文。
> 
> 



```Plain Text
getAttachments()：为当前 Agent 计算允许使用的附件
getAttachmentMessages()：包装成消息
```



## 主要内容

## 文件与 IDE

- `file`：用户 `@` 的文件内容

- `already_read_file`：已经读取过的文件

- `compact_file_reference`：压缩后保留的文件引用

- `pdf_reference`：大型 PDF 的轻量引用

- `edited_text_file`：文本文件发生修改

- `edited_image_file`：图片文件发生修改

- `directory`：目录内容

- `selected_lines_in_ide`：IDE 中选中的代码

- `opened_file_in_ide`：IDE 当前打开的文件

- `diagnostics`：IDE/编译诊断

- `bagel_console`：控制台错误和警告摘要

## Memory

- `nested_memory`：相关目录中的 `CLAUDE.md` 或规则文件

- `relevant_memories`：根据当前问题召回的长期 memory

- `current_session_memory`：当前会话的 Markdown 摘要

例如：

```Plain Text
{
  type: 'relevant_memories',
  memories: [
    {
      path: 'memory/user_preferences.md',
      content: '...',
      mtimeMs: 123456,
    },
  ],
}
```

## Skill

- `dynamic_skill`：动态检测到的 Skill，主要用于 UI

- `skill_listing`：可用 Skill 列表

- `skill_discovery`：根据当前任务发现的相关 Skill

- `invoked_skills`：压缩后保留已经调用过的 Skill 内容

## 工具、Agent 和 MCP 变化

- `deferred_tools_delta`：延迟工具列表变化

- `agent_listing_delta`：可用 Agent 列表变化

- `mcp_instructions_delta`：MCP Server 指令变化

- `mcp_resource`：用户引用的 MCP 资源

- `agent_mention`：用户提到了某个 Agent

- `command_permissions`：命令允许使用的工具和模型

这些是信息通知，不等于真正注册工具。

## 用户消息和后台通知

- `queued_command`：Agent 工作期间排队的用户输入或任务通知

- `task_status`：后台任务状态变化

- `agent_pending_messages` 最终也通常转成 `queued_command`

例如：

```Plain Text
{
  type: 'queued_command',
  prompt: '继续检查测试失败的原因',
  commandMode: 'prompt',
}
```

## Plan、Auto 和任务提醒

- `todo_reminder`

- `task_reminder`

- `plan_mode`

- `plan_mode_reentry`

- `plan_mode_exit`

- `plan_file_reference`

- `verify_plan_reminder`

- `auto_mode`

- `auto_mode_exit`

- `max_turns_reached`

这些附件用于提醒模型当前处于什么工作模式、还有哪些任务以及是否达到轮次限制。

## Token、预算和上下文提醒

- `token_usage`

- `output_token_usage`

- `budget_usd`

- `critical_system_reminder`

- `compaction_reminder`

- `context_efficiency`

- `date_change`

- `ultrathink_effort`

- `companion_intro`

例如：

```Plain Text
{
  type: 'token_usage',
  used: 150000,
  total: 200000,
  remaining: 50000,
}
```

## Hook 执行结果

- `async_hook_response`

- `hook_cancelled`

- `hook_blocking_error`

- `hook_non_blocking_error`

- `hook_error_during_execution`

- `hook_stopped_continuation`

- `hook_success`

- `hook_additional_context`

- `hook_system_message`

- `hook_permission_decision`

它们用于把 Hook 的执行结果、错误、权限决定或额外上下文带回 Agent 循环。

## 多 Agent 团队协作

- `teammate_mailbox`：队友发送的消息

- `team_context`：当前 Agent 所属团队信息

- `teammate_shutdown_batch`：一批队友退出通知

## 其他结果

- `output_style`：当前回答风格

- `structured_output`：结构化输出数据

需要注意，不是所有 attachment 都会原样发给模型：

```Plain Text
Attachment
  ↓
normalizeAttachmentForAPI()
  ├─ 转成 <system-reminder>
  ├─ 转成 meta user message
  ├─ 与 tool_result 合并
```

# TOOL机制

```Plain Text
工具实现
  ↓
tools.ts / getAllBaseTools 注册
  ↓
getTools 权限与环境过滤
  ↓
assembleToolPool 合并 MCP 工具
  ↓
toolUseContext.options.tools
  ↓
callModel 把 Schema 发给模型
  ↓
模型返回 tool_use
  ↓
queryLoop 收集 toolUseBlocks
  ↓
runTools
  ↓
runToolUse
  ↓
权限检查
  ↓
tool.call()
  ↓
生成 tool_result
  ↓
放入下一轮上下文
```

toolsearch

用户需求

↓

queryLoop 把消息发给模型

↓

模型判断需要某种工具

↓

模型生成 tool\_use：

ToolSearch\(\{ query: "read pdf" \}\)

↓

程序执行 ToolSearchTool

↓

返回匹配的 tool\_reference

↓

下一轮把对应工具的完整 Schema 发给模型

↓

模型调用真正的工具，例如 FileRead



**`ToolSearch`**** 不只是写在普通提示词里，而是作为一个正式 Tool Schema 发送给模型。**

模型每轮能看到：

```Plain Text
1. ToolSearch 的完整定义
2. 常用、非延迟 Tool 的完整定义
3. 已经检索发现的 Tool 定义
```

但不会看到所有 deferred Tool 的完整定义：

```Plain Text
大量 MCP Tool
不常用 Tool
标记 shouldDefer 的 Tool
```

完整流程是：

```Plain Text
第一次请求：
[FileRead完整定义, Bash完整定义, ToolSearch完整定义, ...]
                         ↑
              不包含全部 deferred Tool

模型调用 ToolSearch("github issue")
                         ↓
返回 tool_reference: mcp__github__get_issue
                         ↓
下一次请求：
[原有工具, ToolSearch, mcp__github__get_issue完整定义]
```

所以它节省的是大量 Tool Schema 占用的上下文，而不是完全不传工具列表。

`UserMessage` 是发送给模型的“用户角色消息”类型，但不一定真的来自键盘用户。

它的基本结构是：

```Plain Text
{
  type: 'user',
  message: {
    role: 'user',
    content: string | ContentBlockParam[]
  },
  uuid: string,
  timestamp: string,
  isMeta?: true,
  toolUseResult?: unknown
}
```

创建函数在 \[messages\.ts \(line 460\)\]\(E:/claude code/src/utils/messages\.ts:460\)。

### 它可以装什么

1. 用户输入

```Plain Text
createUserMessage({
  content: '帮我读取 query.ts',
})
```

2. 工具执行结果

Anthropic API 规定工具链结构为：

```Plain Text
AssistantMessage: tool_use
        ↓
UserMessage: tool_result
```

例如：

```Plain Text
createUserMessage({
  content: [
    {
      type: 'tool_result',
      tool_use_id: 'tool_123',
      content: '文件内容……',
    },
  ],
})
```

虽然结果不是人说的，但 API 协议把 `tool_result` 放在 `role: "user"` 消息中。

3. 系统注入内容

```Plain Text
createUserMessage({
  content: 'Skills relevant to your task...',
  isMeta: true,
})
```

`isMeta: true` 表示这不是用户键盘输入，而是系统、attachment、memory 或子 Agent 消息。

### `toolUseResult` 与 `content`

两者不要混淆：

```Plain Text
{
  message: {
    content: [toolResultBlock] // 真正发送给模型
  },
  toolUseResult: rawResult    // 项目内部保存的原始工具结果
}
```

所以在这个项目里：

> `UserMessage` 更准确的含义是“从模型外部返回给模型的信息”，包括真实用户输入、工具结果和系统注入信息。
> 
> 

# Usermessage

`UserMessage` 是发送给模型的“用户角色消息”类型，但不一定真的来自键盘用户。

它的基本结构是：

```Plain Text
{
  type: 'user',
  message: {
    role: 'user',
    content: string | ContentBlockParam[]
  },
  uuid: string,
  timestamp: string,
  isMeta?: true,
  toolUseResult?: unknown
}
```

创建函数在 \[messages\.ts \(line 460\)\]\(E:/claude code/src/utils/messages\.ts:460\)。

### 它可以装什么

1. 用户输入

```Plain Text
createUserMessage({
  content: '帮我读取 query.ts',
})
```

2. 工具执行结果

Anthropic API 规定工具链结构为：

```Plain Text
AssistantMessage: tool_use
        ↓
UserMessage: tool_result
```

例如：

```Plain Text
createUserMessage({
  content: [
    {
      type: 'tool_result',
      tool_use_id: 'tool_123',
      content: '文件内容……',
    },
  ],
})
```

虽然结果不是人说的，但 API 协议把 `tool_result` 放在 `role: "user"` 消息中。

3. 系统注入内容

```Plain Text
createUserMessage({
  content: 'Skills relevant to your task...',
  isMeta: true,
})
```

`isMeta: true` 表示这不是用户键盘输入，而是系统、attachment、memory 或子 Agent 消息。

### `toolUseResult` 与 `content`

两者不要混淆：

```Plain Text
{
  message: {
    content: [toolResultBlock] // 真正发送给模型
  },
  toolUseResult: rawResult    // 项目内部保存的原始工具结果
}
```

所以在这个项目里：

> `UserMessage` 更准确的含义是“从模型外部返回给模型的信息”，包括真实用户输入、工具结果和系统注入信息。
> 
> 









### 进入对话历史的 Message

### queryLoop 额外输出的事件

这些不一定属于普通 `Message`：

- `StreamEvent`：模型流式返回的 token/event

- `RequestStartEvent`：一次 API 请求开始

- `TombstoneMessage`：通知 UI 删除无效的残缺消息

- `ToolUseSummaryMessage`：工具批次的人类可读摘要



# Sandbox

## sandbox是在bashtool执行阶段开始的调用链

模型产生 Bash tool\_use

↓

toolExecution\.ts

↓ tool\.call\(\.\.\.\)

BashTool\.call\(\)

↓

runShellCommand\(\)

↓

exec\(\.\.\., \{ shouldUseSandbox \}\)

↓

Shell\.ts 判断 shouldUseSandbox

↓ true

SandboxManager\.wrapWithSandbox\(\)

↓

BaseSandboxManager\.wrapWithSandbox\(\)

↓

spawn\(\) 执行包装后的命令

`containsExcludedCommand` 用来判断一条命令是否被配置为“不要在 sandbox 中运行”。

如果返回 `true`，`shouldUseSandbox()` 就会返回 `false`，命令将走非沙箱执行流程；它并不是禁止执行命令。

判断来源有两类：

- 内部动态配置（仅 `USER_TYPE === 'ant'`）：匹配禁用命令或字符串。

- 用户配置 `settings.sandbox.excludedCommands`：

    - 精确匹配：`npm run lint`

    - 前缀匹配：`npm run test:*`

    - 通配符匹配：如 `docker *`

它还会：

- 拆分 `cmd1 && cmd2` 等复合命令，逐个检查。

- 去掉环境变量前缀和安全包装器，例如让 `timeout 30 FOO=bar bazel run` 也能匹配 `bazel:*`。

- 解析失败时采用宽松回退，避免 UI 或校验流程崩溃。

核心调用关系是：

```Plain Text
if (containsExcludedCommand(input.command)) {
  return false // 不使用 sandbox
}
```

需要注意：`excludedCommands` 只是便利配置，不是安全边界。真正的安全控制仍然是权限系统和用户确认。



# Prompt工程

## 总体架构

这个项目的 Prompt 工程不是“一段巨大的 System Prompt”，而是一个分层、动态组装、面向 Prompt Cache 优化的系统：

```Plain Text
基础 System Prompt
+ 会话动态 System Sections
+ 自定义/Agent Prompt
+ System Context
+ User Context Reminder
+ 历史 Messages
+ Runtime Attachments
+ Tool Description Prompts
+ Tool JSON Schema
```

最终 API 大致收到：

```Plain Text
{
  system: [...systemPromptBlocks],
  messages: [
    userContextReminder,
    ...conversationMessages,
  ],
  tools: [
    {
      name,
      description: await tool.prompt(),
      input_schema,
    },
  ],
}
```

---

## Prompt 的总入口

REPL 在 \[REPL\.tsx \(line 2762\)\]\(E:/claude code/src/screens/REPL\.tsx:2762\) 并行生成：

```Plain Text
getSystemPrompt(...)
getUserContext()
getSystemContext()
```

然后使用 \[systemPrompt\.ts \(line 41\)\]\(E:/claude code/src/utils/systemPrompt\.ts:41\)：

```Plain Text
buildEffectiveSystemPrompt(...)
```

最后交给：

\[REPL\.tsx \(line 2787\)\]\(E:/claude code/src/screens/REPL\.tsx:2787\)

```Plain Text
query({
  messages,
  systemPrompt,
  userContext,
  systemContext,
  toolUseContext,
})
```

所以 Prompt 的实际主链是：

```Plain Text
constants/prompts.ts
    ↓ getSystemPrompt()
utils/systemPrompt.ts
    ↓ buildEffectiveSystemPrompt()
query.ts
    ↓ appendSystemContext / prependUserContext
services/api/claude.ts
    ↓ system blocks + messages + tool schemas
Anthropic Messages API
```

---

## 基础 System Prompt

核心生成函数位于：

\[prompts\.ts \(line 444\)\]\(E:/claude code/src/constants/prompts\.ts:444\)

```Plain Text
export async function getSystemPrompt(
  tools,
  model,
  additionalWorkingDirectories,
  mcpClients,
): Promise<string[]>
```

注意它返回的是 `string[]`，而不是单个字符串。这使每个 Prompt section 可以：

- 独立生成

- 独立缓存

- 按条件启用

- 在 API 层设置不同 cache scope

- 进行 token 分析

基础静态部分在 \[prompts\.ts \(line 560\)\]\(E:/claude code/src/constants/prompts\.ts:560\)：

```Plain Text
return [
  getSimpleIntroSection(outputStyleConfig),
  getSimpleSystemSection(),
  getSimpleDoingTasksSection(),
  getActionsSection(),
  getUsingYourToolsSection(enabledTools),
  getSimpleToneAndStyleSection(),
  getOutputEfficiencySection(),
  SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
  ...resolvedDynamicSections,
]
```

### 静态段的职责

#### Identity

\[prompts\.ts \(line 175\)\]\(E:/claude code/src/constants/prompts\.ts:175\)

定义模型身份：

```Plain Text
You are an interactive agent...
Use the instructions below and the tools available...
```

同时加入网络安全与 URL 生成限制。

#### System behavior

\[prompts\.ts \(line 186\)\]\(E:/claude code/src/constants/prompts\.ts:186\)

告诉模型：

- 普通文本直接展示给用户

- 工具可能需要用户授权

- 不要重复执行被拒绝的工具

- `system-reminder` 是系统注入内容

- 工具结果可能包含 Prompt Injection

- 对话会自动压缩

#### Software engineering policy

\[prompts\.ts \(line 199\)\]\(E:/claude code/src/constants/prompts\.ts:199\)

这一段决定 Codex/Claude Code 的主要工程行为：

- 先读代码再修改

- 不扩大任务范围

- 不做投机性抽象

- 不创建不必要的文件

- 不盲目重试失败命令

- 不虚报测试成功

- 注意 XSS、SQL 注入、命令注入等安全问题

#### Action safety

\[prompts\.ts \(line 255\)\]\(E:/claude code/src/constants/prompts\.ts:255\)

按照可逆性和影响范围划分操作：

```Plain Text
本地、可逆操作 → 可以主动执行
破坏性、共享状态、对外可见操作 → 先确认
```

这属于 Prompt 层的软安全策略。真正的硬安全仍由 permission、sandbox 和工具实现负责。

#### Tool routing

\[prompts\.ts \(line 269\)\]\(E:/claude code/src/constants/prompts\.ts:269\)

规定模型如何选择工具：

```Plain Text
读文件 → Read
编辑文件 → Edit
创建文件 → Write
搜索文件 → Glob
搜索内容 → Grep
系统命令 → Bash
```

并指导独立工具调用并行、依赖调用串行。

---

## 动态 System Prompt Sections

动态部分从 \[prompts\.ts \(line 491\)\]\(E:/claude code/src/constants/prompts\.ts:491\) 开始：

```Plain Text
const dynamicSections = [
  session_guidance,
  memory,
  ant_model_override,
  env_info_simple,
  language,
  output_style,
  mcp_instructions,
  scratchpad,
  frc,
  summarize_tool_results,
]
```

分别负责：

### Section 缓存

实现位于：

\[systemPromptSections\.ts \(line 20\)\]\(E:/claude code/src/constants/systemPromptSections\.ts:20\)

```Plain Text
systemPromptSection(name, compute)
```

普通 section：

- 第一次计算

- 缓存到会话状态

- 后续轮次复用

- `/clear` 或 `/compact` 后清理

只有明确声明为：

```Plain Text
DANGEROUS_uncachedSystemPromptSection(...)
```

才每轮重新计算。

“DANGEROUS”不是因为内容不安全，而是因为动态修改 System Prompt 会破坏 Prompt Cache。

---

## System Prompt 的替换优先级

`getSystemPrompt()` 生成默认 Prompt 后，还要经过：

\[systemPrompt\.ts \(line 41\)\]\(E:/claude code/src/utils/systemPrompt\.ts:41\)

优先级是：

```Plain Text
overrideSystemPrompt
    ↓ 否则
Coordinator Prompt
    ↓ 否则
Main-thread Agent Prompt
    ↓ 否则
--system-prompt 自定义 Prompt
    ↓ 否则
默认 Claude Code Prompt
```

最后追加：

```Plain Text
--append-system-prompt
```

但如果设置了 `overrideSystemPrompt`，其他内容都会被完全替换。

普通情况下：

```Plain Text
return asSystemPrompt([
  ...(agentPrompt
    ? [agentPrompt]
    : customPrompt
      ? [customPrompt]
      : defaultSystemPrompt),
  ...(appendSystemPrompt ? [appendSystemPrompt] : []),
])
```

因此：

- `--system-prompt` 是替换默认 Prompt

- `--append-system-prompt` 是补充默认 Prompt

- Agent Prompt 通常也会替换默认 Prompt

- Proactive 模式例外：Agent Prompt 会追加到默认 Prompt

---

## System Context 与 User Context

两者名字相似，但注入位置完全不同。

### System Context

在 \[query\.ts \(line 449\)\]\(E:/claude code/src/query\.ts:449\)：

```Plain Text
const fullSystemPrompt = asSystemPrompt(
  appendSystemContext(systemPrompt, systemContext),
)
```

`systemContext` 来源包括：

- Git status

- cache breaker

- 会话级环境信息

它直接追加进 System Prompt。

### User Context

调用模型前：

\[query\.ts \(line 658\)\]\(E:/claude code/src/query\.ts:658\)

```Plain Text
callModel({
  messages: prependUserContext(messagesForQuery, userContext),
  systemPrompt: fullSystemPrompt,
})
```

`userContext` 在 \[api\.ts \(line 449\)\]\(E:/claude code/src/utils/api\.ts:449\) 中变成：

```Plain Text
<system-reminder>
As you answer the user's questions, you can use the following context:

# claudeMd
...

# currentDate
Today's date is ...

IMPORTANT: this context may or may not be relevant...
</system-reminder>
```

所以 CLAUDE\.md 实际上是一个位于消息历史头部的 meta user message，而不是 System Prompt。

这种设计有两个好处：

- System Prompt 保持稳定，Prompt Cache 不容易失效

- 项目上下文可以作为用户域信息与通用系统政策分离

---

## Tool Prompt 是第二套 Prompt 系统

每个 Tool 都有自己的：

```Plain Text
async prompt()
```

API schema 构建位置：

\[api\.ts \(line 119\)\]\(E:/claude code/src/utils/api\.ts:119\)

```Plain Text
base = {
  name: tool.name,
  description: await tool.prompt(...),
  input_schema,
}
```

因此 Tool 的 `prompt()` 最终成为 API 工具的 `description`。

### BashTool 示例

\[BashTool/prompt\.ts \(line 275\)\]\(E:/claude code/src/tools/BashTool/prompt\.ts:275\)

它不只是描述“运行命令”，还指导模型：

- 文件搜索不要用 `find`

- 文件读取不要用 `cat`

- 编辑不要用 `sed`

- 路径包含空格时加引号

- 独立命令并行

- 依赖命令使用 `&&`

- 不盲目 sleep/poll

- Git 不跳过 hook

- 如何使用后台任务

- 沙箱目录和网络限制

沙箱配置也会动态进入 BashTool description：

\[BashTool/prompt\.ts \(line 263\)\]\(E:/claude code/src/tools/BashTool/prompt\.ts:263\)

```Plain Text
## Command sandbox
By default, your command will be run in a sandbox...
```

因此模型是否知道 `dangerouslyDisableSandbox`、$TMPDIR 和 sandbox 限制，主要由 BashTool Prompt 控制。

### FileReadTool 示例

\[FileReadTool\.ts \(line 347\)\]\(E:/claude code/src/tools/FileReadTool/FileReadTool\.ts:347\)

Tool Prompt 会根据运行配置动态调整：

- 文件大小上限

- offset/limit 使用策略

- 行号格式

- targeted range 提示

也就是说，工具 Prompt 同时承担“API 文档”和“行为引导”两个职责。

---

## Runtime Prompt：system\-reminder

项目中大量运行时信息不放进 System Prompt，而是转换成：

```Plain Text
<system-reminder>
...
</system-reminder>
```

包装函数：

\[messages\.ts \(line 3096\)\]\(E:/claude code/src/utils/messages\.ts:3096\)

```Plain Text
export function wrapInSystemReminder(content: string) {
  return `<system-reminder>\n${content}\n</system-reminder>`
}
```

常见 runtime reminder 包括：

- CLAUDE\.md

- Plan mode

- 当前 output style

- IDE diagnostics

- 文件被用户修改

- Hook 附加上下文

- MCP instructions delta

- Agent listing delta

- Deferred tools delta

- Skill discovery

- 后台任务完成通知

- 最近读取文件

- compact 后恢复的 Plan/Skill

例如 output style reminder：

\[messages\.ts \(line 3796\)\]\(E:/claude code/src/utils/messages\.ts:3796\)

```Plain Text
Explanatory output style is active.
Remember to follow the specific guidelines for this style.
```

这种设计避免了每次状态改变都修改 System Prompt。

---

## Agent 和 Skill Prompt

Agent Prompt 有两层。

### AgentTool description

\[AgentTool/prompt\.ts \(line 200\)\]\(E:/claude code/src/tools/AgentTool/prompt\.ts:200\)

告诉主模型：

- Agent 适合复杂、多步骤任务

- 什么情况下用 Agent

- 什么情况下直接 Read/Glob

- 可用 Agent 类型

- fork 是否继承完整上下文

Agent 列表可以不直接放在 Tool description，而通过 `agent_listing_delta` reminder 注入：

```Plain Text
Available agent types are listed in
<system-reminder> messages...
```

这样 Agent 列表改变时不会导致整个 Tool schema cache 失效。

### 子 Agent System Prompt

启动 Agent 后，它有自己的专门 System Prompt，例如：

- Explore Agent

- Plan Agent

- Verification Agent

- Statusline Setup Agent

- 用户 `.claude/agents/*.md`

- Plugin Agent

因此子 Agent 不是简单继承一句角色描述，而是获得专用的任务协议、工具集合和系统提示词。

Skill 则主要通过：

- SkillTool description

- skill discovery reminder

- Skill 文件内容

- compact 后的 invoked\-skill reinjection

共同控制。

---

## Prompt Cache 是核心设计约束

这个项目的 Prompt 工程很大一部分不是“怎么把提示词写漂亮”，而是“怎么防止 Prompt 每轮发生无意义变化”。

### 静态/动态边界

\[prompts\.ts \(line 114\)\]\(E:/claude code/src/constants/prompts\.ts:114\)

```Plain Text
SYSTEM_PROMPT_DYNAMIC_BOUNDARY
```

边界前：

```Plain Text
身份、通用政策、工具使用、语气
```

边界后：

```Plain Text
环境、语言、MCP、Memory、Session Guidance
```

API 层在 \[api\.ts \(line 321\)\]\(E:/claude code/src/utils/api\.ts:321\) 将其拆为：

```Plain Text
Attribution Header       不缓存
CLI Prefix               单独处理
Static Prompt            global cache
Dynamic Prompt           不使用 global cache
```

### Tool schema 缓存

\[api\.ts \(line 136\)\]\(E:/claude code/src/utils/api\.ts:136\)

工具的：

- name

- description

- input schema

- strict

- eager streaming

会按 session 缓存，防止 feature flag 或动态状态导致序列化字节变化。

### Delta Attachment

以下内容尽量不直接修改 System Prompt 或 Tool description：

- MCP server instructions

- Agent list

- Deferred tool list

而是使用增量 reminder：

```Plain Text
mcp_instructions_delta
agent_listing_delta
deferred_tools_delta
```

核心目标是：

> 让 Prompt 的大前缀保持字节级稳定，只让真正变化的小尾部变化。
> 
> 

---

## 专用 Prompt 系统

主 Query Prompt 之外，项目还有许多小型专用 Prompt：

它们不会盲目复用主 System Prompt，而是使用针对任务的极窄 Prompt。

例如 compact prompt 在 \[compact/prompt\.ts \(line 19\)\]\(E:/claude code/src/services/compact/prompt\.ts:19\) 开头就强调：

```Plain Text
CRITICAL: Respond with TEXT ONLY.
Do NOT call any tools.
```

然后规定固定摘要结构：

- 用户意图

- 技术概念

- 文件和代码

- 错误与修复

- 当前工作

- 待办任务

- 下一步

这体现了一个重要原则：

> 主 Agent 使用开放式行动 Prompt；辅助模型使用封闭式、结构化、单目标 Prompt。
> 
> 

---

## 最终组装顺序

完整顺序可以概括为：

```Plain Text
getSystemPrompt()
│
├── 静态行为政策
├── 安全与权限原则
├── 工具选择规则
├── 输出风格
├── Prompt cache boundary
└── 动态会话 sections
        ↓
buildEffectiveSystemPrompt()
│
├── override
├── coordinator
├── agent
├── custom
└── append
        ↓
query()
│
├── appendSystemContext()
├── prependUserContext()
├── runtime attachments
└── conversation history
        ↓
claude.ts
│
├── attribution / CLI prefix
├── prompt cache blocks
├── normalize messages
├── tool.prompt() descriptions
└── JSON schemas
        ↓
Messages API
```

这个项目 Prompt 工程最值得借鉴的不是某一句提示词，而是三个架构思想：

1. 将稳定政策、动态上下文和工具说明彻底分层。

2. 把 Prompt Cache 稳定性视为一等工程约束。

3. 用 `system-reminder` 和 delta attachments 注入短期状态，避免频繁改写巨大 System Prompt。

14:40

Prompt Cache是什么，用 system\-reminder 和 delta attachments 注入短期状态，避免频繁改写巨大 System Prompt。这个是什么

15:00

## Prompt Cache 是什么

调用模型时，客户端通常每轮都要重新发送完整上下文：

```Plain Text
System Prompt
+ Tools Schema
+ CLAUDE.md
+ 历史消息
+ 本轮用户输入
```

这些内容可能有几十万 token，但前面大部分内容与上一轮完全相同。

Prompt Cache 的作用是：服务端缓存相同 Prompt 前缀的处理结果。下一轮如果前缀完全一致，就不用重新计算这部分内容。

```Plain Text
第一轮：
[巨大 System Prompt][Tools][历史][问题1]
 └──────── 缓存前缀 ────────┘

第二轮：
[相同 System Prompt][相同 Tools][相同历史][回答1][问题2]
 └──────── 命中缓存 ──────────────┘
```

它通常可以降低：

- 首 token 延迟

- 输入 token 计算量

- cache creation 成本

- 服务端重复推理工作

关键条件是：

> 缓存依赖前缀稳定。前面任何内容发生变化，变化点之后的缓存都可能失效。
> 
> 

---

## Prompt Cache 不是本地 Map 缓存

项目里其实有两种缓存。

### 本地生成缓存

例如：

\[systemPromptSections\.ts \(line 20\)\]\(E:/claude code/src/constants/systemPromptSections\.ts:20\)

```Plain Text
systemPromptSection(name, compute)
```

它避免每轮重新读取 Memory、环境信息和配置。

工具 schema 也有本地缓存：

\[api\.ts \(line 151\)\]\(E:/claude code/src/utils/api\.ts:151\)

```Plain Text
const cache = getToolSchemaCache()
```

### 模型 API Prompt Cache

这是服务端对 System Prompt、Tools、Messages 前缀的缓存。

项目通过 `cache_control`、`cacheScope` 等配置控制：

\[api\.ts \(line 321\)\]\(E:/claude code/src/utils/api\.ts:321\)

```Plain Text
type SystemPromptBlock = {
  text: string
  cacheScope: 'global' | 'org' | null
}
```

---

## 为什么 System Prompt 变化会有问题

假设 System Prompt 是一段 30k token 的字符串：

```Plain Text
身份规则
+ 编程规范
+ 工具说明
+ Memory
+ 当前目录
+ MCP Server 列表
+ 当前 Agent 列表
```

如果某个 MCP Server 中途连接，直接重新生成 System Prompt：

```Plain Text
MCP Servers:
  - github
+ - chrome
```

虽然只增加了一行，但整个 System Prompt 的序列化内容变了。

结果可能是：

```Plain Text
上一轮缓存键：
hash(SystemPrompt-A)

下一轮缓存键：
hash(SystemPrompt-B)
```

于是发生 cache miss，巨大的前缀需要重新处理。

---

## 第一种解决方案：静态/动态边界

项目在 System Prompt 中插入：

\[prompts\.ts \(line 114\)\]\(E:/claude code/src/constants/prompts\.ts:114\)

```Plain Text
SYSTEM_PROMPT_DYNAMIC_BOUNDARY
```

形成：

```Plain Text
静态部分
├── 身份
├── 安全规则
├── 编程规范
├── 工具使用原则
└── 输出风格
──────── Dynamic Boundary ────────
动态部分
├── 当前环境
├── Memory
├── Language
├── MCP instructions
└── Session guidance
```

API 层把它拆为两个块：

\[api\.ts \(line 362\)\]\(E:/claude code/src/utils/api\.ts:362\)

```Plain Text
staticBlocks  → cacheScope: 'global'
dynamicBlocks → cacheScope: null
```

这样动态部分变化时，静态大前缀仍然可以复用。

---

## 第二种解决方案：system\-reminder

对于频繁变化的短期状态，项目不修改 System Prompt，而是把状态放进消息历史：

```Plain Text
<system-reminder>
Chrome MCP Server has connected.
The following tools are now available...
</system-reminder>
```

包装函数在：

\[messages\.ts \(line 3096\)\]\(E:/claude code/src/utils/messages\.ts:3096\)

```Plain Text
export function wrapInSystemReminder(content: string): string {
  return `<system-reminder>\n${content}\n</system-reminder>`
}
```

它实际上是一个 meta user message：

```Plain Text
createUserMessage({
  content: '<system-reminder>...</system-reminder>',
  isMeta: true,
})
```

注意：

> `system-reminder` 并不是 Anthropic API 的 system role，它仍然属于 user message，只是项目用特殊标签告诉模型“这是系统动态注入的信息”。
> 
> 

主 System Prompt 也专门教模型识别它：

\[prompts\.ts \(line 131\)\]\(E:/claude code/src/constants/prompts\.ts:131\)

```Plain Text
Tool results and user messages may include
<system-reminder> tags.

They are automatically added by the system...
```

---

## 为什么放到消息尾部更利于缓存

假设原上下文是：

```Plain Text
[System Prompt][Tools][历史 A B C]
```

现在 Chrome MCP 连接。

### 修改 System Prompt

```Plain Text
[新的 System Prompt][Tools][历史 A B C]
 ↑ 很靠前的位置改变
```

变化点之后的缓存都可能无法复用。

### 追加 system\-reminder

```Plain Text
[原 System Prompt][原 Tools][历史 A B C][Chrome connected reminder]
 └────────── 旧前缀完全不变 ──────────┘
```

只需要处理最后新增加的 reminder。

这就是“避免频繁改写巨大 System Prompt”的含义。

---

## Delta Attachment 是什么

Delta 就是“增量”。

不用每轮重复发送完整状态：

```Plain Text
当前 Agent：
- Explore
- Plan
- General
- Verification
```

而是只发送变化：

```Plain Text
新增 Agent：
+ Verification
```

项目中的典型 delta attachment 包括：

```Plain Text
agent_listing_delta
deferred_tools_delta
mcp_instructions_delta
```

### Agent 示例

AgentTool 的 Prompt 中写着：

\[AgentTool/prompt\.ts \(line 189\)\]\(E:/claude code/src/tools/AgentTool/prompt\.ts:189\)

```Plain Text
Available agent types are listed in
<system-reminder> messages in the conversation.
```

Agent 列表不再直接写进 `AgentTool.description`。

当 Agent 列表发生变化时，系统追加：

```Plain Text
<system-reminder>
<agent-listing-delta>
新增或变化的 Agent 信息
</agent-listing-delta>
</system-reminder>
```

这样可以避免 Tool description 变化。

---

## 为什么 Tool description 也必须稳定

API 请求中的 Tool schema 类似：

```Plain Text
{
  "name": "Agent",
  "description": "Available agents: Explore, Plan...",
  "input_schema": {}
}
```

如果动态 Agent 列表写在 description 中：

```Plain Text
- Available agents: Explore, Plan
+ Available agents: Explore, Plan, Verification
```

整个 Tools 前缀发生变化，Tools Prompt Cache 可能失效。

因此项目改成：

```Plain Text
{
  "name": "Agent",
  "description": "Available agent types are listed in system-reminder messages.",
  "input_schema": {}
}
```

这个 description 永远稳定。具体 Agent 列表通过后面的 delta message 提供。

---

## Delta Attachment 的生命周期

假设初始状态：

```Plain Text
MCP：github
```

第一次发送：

```Plain Text
<mcp-instructions-delta>
github 的完整说明
</mcp-instructions-delta>
```

中途 Chrome MCP 连接，只追加：

```Plain Text
<mcp-instructions-delta>
新增 chrome 的说明
</mcp-instructions-delta>
```

模型通过消息历史能够看到两个增量，从而还原当前状态：

```Plain Text
github + chrome
```

压缩后，旧 delta 消息可能被清除，所以 compact 会重新公布完整状态：

\[compact\.ts \(line 563\)\]\(E:/claude code/src/services/compact/compact\.ts:563\)

```Plain Text
getDeferredToolsDeltaAttachment(..., [])
getAgentListingDeltaAttachment(context, [])
getMcpInstructionsDeltaAttachment(..., [])
```

这里传入空历史，相当于：

```Plain Text
压缩已经吃掉旧增量
→ 与空状态做 diff
→ 重新发送当前完整状态
```

---

## 一个完整例子

假设会话开始时有：

```Plain Text
System Prompt：20k token
Tools Schema：10k token
CLAUDE.md：5k token
历史：50k token
```

Chrome MCP 中途连接。

### 不使用增量设计

重新生成：

```Plain Text
System Prompt：20k，发生变化
Tools Schema：10k，发生变化
历史：50k
```

大量前缀可能重新计算。

### 使用 reminder \+ delta

保持：

```Plain Text
System Prompt：原样
Tools Schema：原样
CLAUDE.md：原样
历史：原样
```

只追加：

```Plain Text
<system-reminder>
<mcp-instructions-delta>
Chrome MCP 已连接，新增以下工具……
</mcp-instructions-delta>
</system-reminder>
```

这条可能只有几百 token。

---

## 需要注意的边界

`system-reminder` 是 Prompt 工程机制，不是安全边界。

它不能替代：

- Tool permissions

- Sandbox

- 文件系统限制

- 网络访问限制

- Hook enforcement

因为在 API 层它最终仍是文本。真正是否允许执行危险操作，必须由 permission 和 sandbox 代码决定。

可以把它理解为：

```Plain Text
system-reminder
    = 告诉模型当前状态的软指令

permissions / sandbox
    = 即使模型不听，也无法越过的硬限制
```

所以这套设计的核心是：

> 稳定的大块内容留在可缓存前缀中；经常变化的小块状态，以带标签的增量消息追加到上下文尾部。
> 
> 



# 持久化

这三个概念分别是：

```Plain Text
Session Storage
= 当前会话的身份、路径和附属状态

Transcript
= 一个 Session 的 append-only JSONL 事件日志

Resume
= 读取 Transcript，重建当前有效消息链和运行状态，然后继续写入
```

你当前打开的：

```Plain Text
~/.claude/projects/.../<session-id>.jsonl
```

就是一个完整会话的 Transcript。

而：

```Plain Text
~/.claude/history.jsonl
```

只是输入框历史，不是完整会话。

---

## 磁盘结构

大致结构如下：

```Plain Text
~/.claude/
├── history.jsonl
│   └── 输入框历史，用于 ↑ 和 Ctrl+R
│
└── projects/
    └── <sanitized-project-path>/
        ├── <session-id>.jsonl
        │   └── 主会话 transcript
        │
        └── <session-id>/
            ├── subagents/
            │   ├── agent-<agent-id>.jsonl
            │   └── agent-<agent-id>.meta.json
            │
            └── remote-agents/
                └── remote-agent-<task-id>.meta.json
```

主会话路径由：

\[sessionStorage\.ts \(line 202\)\]\(E:/claude code/src/utils/sessionStorage\.ts:202\)

```Plain Text
return join(projectDir, `${getSessionId()}.jsonl`)
```

生成。

子 Agent 使用独立 sidechain：

\[sessionStorage\.ts \(line 247\)\]\(E:/claude code/src/utils/sessionStorage\.ts:247\)

```Plain Text
<sessionId>/subagents/agent-<agentId>.jsonl
```

---

## Session Storage 保存什么

Session 不只是消息数组，还包括：

- `sessionId`

- 项目目录

- 当前 CWD

- Git branch

- Claude Code version

- 自定义标题和 tag

- Agent mode

- permission mode

- worktree 状态

- PR 链接

- token/cost 状态

- file\-history snapshots

- attribution snapshots

- tool\-result replacement 状态

- context\-collapse 状态

- subagent metadata

当前 Session ID 在：

\[bootstrap/state\.ts \(line 466\)\]\(E:/claude code/src/bootstrap/state\.ts:466\)

```Plain Text
switchSession(sessionId, projectDir)
```

这里会原子更新：

```Plain Text
STATE.sessionId = sessionId
STATE.sessionProjectDir = projectDir
```

这样跨项目、worktree Resume 时，Session ID 和 JSONL 所在目录不会错位。

---

## Transcript 的记录结构

一条消息大致会被写成：

```Plain Text
{
  "type": "user",
  "uuid": "user-2",
  "parentUuid": "assistant-1",
  "sessionId": "...",
  "cwd": "...",
  "gitBranch": "main",
  "version": "...",
  "message": {
    "role": "user",
    "content": "解释这段代码"
  }
}
```

下一条助手消息：

```Plain Text
{
  "type": "assistant",
  "uuid": "assistant-2",
  "parentUuid": "user-2",
  "sessionId": "...",
  "message": {
    "role": "assistant",
    "content": []
  }
}
```

所以 Transcript 同时具有两种顺序：

```Plain Text
物理顺序：JSONL 中的写入先后
逻辑顺序：parentUuid 指向的会话关系
```

---

## 写入流程

主写入入口是：

\[sessionStorage\.ts \(line 1405\)\]\(E:/claude code/src/utils/sessionStorage\.ts:1405\)

```Plain Text
recordTranscript(messages)
```

它首先清理不需要持久化的消息，然后根据 UUID 去重：

```Plain Text
const messageSet = await getSessionMessages(sessionId)

if (!messageSet.has(message.uuid)) {
  newMessages.push(message)
}
```

这是因为调用方经常传入不断增长的完整数组：

```Plain Text
第一次：[A, B]
第二次：[A, B, C]
第三次：[A, B, C, D]
```

实际写入只会是：

```Plain Text
第一次：A, B
第二次：C
第三次：D
```

然后调用：

```Plain Text
insertMessageChain(newMessages)
```

---

## 首条真实消息才创建 Session 文件

项目不会因为启动了 CLI 就立刻创建空 Session。

\[sessionStorage\.ts \(line 975\)\]\(E:/claude code/src/utils/sessionStorage\.ts:975\)

只有出现第一条 `user` 或 `assistant` 消息时：

```Plain Text
if (
  sessionFile === null &&
  messages.some(m => m.type === 'user' || m.type === 'assistant')
) {
  await materializeSessionFile()
}
```

之前产生的 metadata、hook attachment 等先放在：

```Plain Text
pendingEntries
```

文件创建后再统一 flush。

这样 `/resume` 列表里不会出现大量只启动过、但没有真实对话的空会话。

---

## parentUuid 如何建立消息链

写入时：

\[sessionStorage\.ts \(line 992\)\]\(E:/claude code/src/utils/sessionStorage\.ts:992\)

```Plain Text
let parentUuid = startingParentUuid ?? null

for (const message of messages) {
  write({
    ...message,
    parentUuid,
  })

  parentUuid = message.uuid
}
```

形成：

```Plain Text
user-1
  ↓
assistant-1
  ↓
tool-result-1
  ↓
assistant-2
```

Tool Result 有特殊处理：

```Plain Text
sourceToolAssistantUUID
```

它会直接指向产生对应 `tool_use` 的 Assistant Message，而不是单纯指向最后写入的记录。

这使并行工具调用在磁盘上更像 DAG：

```Plain Text
tool_use-A → tool_result-A
assistant ──┤
             tool_use-B → tool_result-B
```

Resume 时会专门恢复这些并行 sibling。

---

## JSONL 中不只有消息

同一个文件还会追加很多事件：

```Plain Text
{"type":"user", ...}
{"type":"assistant", ...}
{"type":"custom-title", ...}
{"type":"tag", ...}
{"type":"file-history-snapshot", ...}
{"type":"attribution-snapshot", ...}
{"type":"content-replacement", ...}
{"type":"system","subtype":"compact_boundary", ...}
```

读取时，不同类型进入不同 Map：

\[sessionStorage\.ts \(line 3620\)\]\(E:/claude code/src/utils/sessionStorage\.ts:3620\)

```Plain Text
messages             → UUID → Message
summaries            → leafUUID → summary
customTitles         → sessionId → title
tags                 → sessionId → tag
worktreeStates       → sessionId → worktree
contentReplacements  → sessionId → replacement records
```

后写入的 metadata 通常覆盖同一 Session 的旧值，相当于 append\-only 下的 last\-write\-wins。

---

## 写入不是每条都立即落盘

项目维护了每个文件独立的写队列：

\[sessionStorage\.ts \(line 605\)\]\(E:/claude code/src/utils/sessionStorage\.ts:605\)

```Plain Text
writeQueues.get(filePath).push(entry)
```

默认以 100ms 为间隔 drain：

```Plain Text
FLUSH_INTERVAL_MS = 100
```

最终使用：

\[sessionStorage\.ts \(line 633\)\]\(E:/claude code/src/utils/sessionStorage\.ts:633\)

```Plain Text
fsAppendFile(filePath, data, { mode: 0o600 })
```

这带来几个效果：

- 保持同一文件的写入顺序

- 合并频繁的小写入

- 避免模型流式输出时疯狂触发磁盘 I/O

- 文件权限限制为当前用户

代价是进程极端崩溃时，最后极短的一段缓冲可能尚未写入。正常退出会通过 graceful shutdown 尽量 flush。

---

## history\.jsonl 与 Transcript 的区别

`history.jsonl` 位于：

```Plain Text
~/.claude/history.jsonl
```

它只服务于：

- 输入框按 ↑

- Ctrl\+R 搜索

- 最近 prompt

- 粘贴内容引用恢复

相关读取：

\[history\.ts \(line 106\)\]\(E:/claude code/src/history\.ts:106\)

```Plain Text
readLinesReverse(historyPath)
```

写入：

\[history\.ts \(line 315\)\]\(E:/claude code/src/history\.ts:315\)

```Plain Text
await appendFile(historyPath, jsonLines.join(''))
```

一条记录类似：

```Plain Text
{
  "display": "解释这个函数",
  "timestamp": 123456789,
  "project": "E:\\project",
  "sessionId": "...",
  "pastedContents": {}
}
```

它不保存 Assistant 回答、Tool Use、Tool Result，所以不能用于 Resume。

---

## Resume 的第一步：快速列出 Session

打开 `/resume` 时，不会立刻完整解析所有大型 JSONL。

项目会优先读取文件：

- stat 信息

- 文件头部

- 文件尾部

- 标题

- tag

- 最后 prompt

- Session ID

- 修改时间

这样可以快速展示 Resume 列表。

用户选定某个 Session 后，才执行完整加载：

\[sessionStorage\.ts \(line 2944\)\]\(E:/claude code/src/utils/sessionStorage\.ts:2944\)

```Plain Text
loadFullLog(log)
```

---

## 完整加载 Transcript

真正解析 JSONL：

\[sessionStorage\.ts \(line 3467\)\]\(E:/claude code/src/utils/sessionStorage\.ts:3467\)

```Plain Text
loadTranscriptFile(filePath)
```

主要步骤：

```Plain Text
读取 JSONL
  ↓
解析每一行 Entry
  ↓
消息进入 messages Map
  ↓
metadata 进入对应 Map
  ↓
处理 compact boundary
  ↓
处理 snip removals
  ↓
计算所有有效 leaf
  ↓
选择最新 leaf
```

对大文件还有优化：

- 跳过已被 compact 的旧前缀

- 在 parse 前裁掉死分支

- 避免解析已经不可达的 rewind/fork 历史

- 保留 pre\-compact metadata

---

## 如何从 JSONL 重建当前对话

选定最新的用户/助手叶节点后，调用：

\[sessionStorage\.ts \(line 2065\)\]\(E:/claude code/src/utils/sessionStorage\.ts:2065\)

```Plain Text
buildConversationChain(messages, leafMessage)
```

算法很直接：

```Plain Text
current = leaf

while (current) {
  transcript.push(current)
  current = messages.get(current.parentUuid)
}

transcript.reverse()
```

也就是：

```Plain Text
从最新消息开始
→ 沿 parentUuid 向前走
→ 直到 parentUuid = null
→ 再反转
```

如果 JSONL 中包含多个分支：

```Plain Text
A2
         /
A1 → B1
         \
          C2 → C3
```

Resume 最新的 `C3` 时，只重建：

```Plain Text
A1 → B1 → C2 → C3
```

`A2` 仍然物理存在，但不属于当前分支。

---

## 并行 Tool Result 恢复

普通 parent 链是 linked list，但并行工具调用会形成 DAG。

因此 `buildConversationChain()` 后面还会执行：

```Plain Text
recoverOrphanedParallelToolResults(...)
```

它根据相同的 API `message.id` 找回：

- 同一次 Assistant Response 拆出的 sibling blocks

- 每个 Tool Use 对应的 Tool Result

- 旧版本中被 progress 分支隔断的结果

然后把它们插回正确位置。

否则 Resume 后可能出现：

```Plain Text
tool_use 有了
tool_result 丢了
```

最终导致 API 报 tool pairing 错误。

---

## Compact 如何影响 Resume

Compact 并不物理重写旧 JSONL，而是追加：

```Plain Text
旧消息
旧消息
compact_boundary
summary
新消息
```

Compact boundary 写入时：

\[sessionStorage\.ts \(line 1024\)\]\(E:/claude code/src/utils/sessionStorage\.ts:1024\)

```Plain Text
parentUuid: null
logicalParentUuid: oldParentUuid
```

含义是：

```Plain Text
parentUuid = null
→ 新模型上下文从这里开始

logicalParentUuid
→ 保留它在完整历史中的来源关系
```

加载时会执行：

\[sessionStorage\.ts \(line 3698\)\]\(E:/claude code/src/utils/sessionStorage\.ts:3698\)

```Plain Text
applyPreservedSegmentRelinks(messages)
applySnipRemovals(messages)
```

从内存视图中去掉不再有效的旧消息，并把需要保留的最近片段接到 summary 后面。

因此：

```Plain Text
磁盘：
A B C D boundary summary E

Resume 后有效消息：
boundary summary E
```

---

## Tool Result 替换状态也必须恢复

大型 Tool Result 可能已经落盘，消息上下文中只留下预览：

```Plain Text
完整输出已保存到 /path/tool-result.txt
预览：前 2000 字符...
```

这个选择必须跨 Resume 保持稳定，否则恢复后可能突然把完整输出重新发给模型，导致：

- Prompt Cache miss

- 上下文暴涨

- Prompt Too Long

所以 JSONL 会追加：

```Plain Text
{
  "type": "content-replacement",
  "sessionId": "...",
  "replacements": [...]
}
```

Resume 时在 \[sessionStorage\.ts \(line 3676\)\]\(E:/claude code/src/utils/sessionStorage\.ts:3676\) 收集这些记录并重建 replacement state。

---

## Resume 后恢复运行状态

消息加载完成后，进入：

\[sessionRestore\.ts \(line 409\)\]\(E:/claude code/src/utils/sessionRestore\.ts:409\)

```Plain Text
processResumedConversation(...)
```

它会恢复：

- Session ID

- Session 项目目录

- Coordinator/普通模式

- Main Agent 定义

- cost state

- custom title/tag

- mode

- worktree CWD

- file history

- content replacements

- attribution

- Agent 状态

普通 Resume 会复用原 Session ID：

```Plain Text
switchSession(originalSessionId, transcriptDirectory)
```

并继续向原 JSONL 追加新记录。

---

## Resume 与 Fork Session

### 普通 Resume

```Plain Text
读取 session-A.jsonl
→ sessionId 切换到 A
→ 恢复消息
→ 后续继续写 session-A.jsonl
```

### Fork Session

```Plain Text
读取 session-A.jsonl
→ 保持新 sessionId B
→ 把有效消息链复制到 session-B.jsonl
→ 后续只写 B
```

原 Session A 不受影响。

Fork 时还要复制 `content-replacement` 状态：

\[sessionRestore\.ts \(line 452\)\]\(E:/claude code/src/utils/sessionRestore\.ts:452\)

否则新 Session 会错误地恢复完整大型 Tool Result。

Worktree 所有权也不会直接继承，避免 Fork 退出时删除原 Session 仍在使用的 worktree。

---

## 总流程

```Plain Text
用户输入
  ↓
REPL / QueryEngine 更新 messages
  ↓
recordTranscript(messages)
  ↓
UUID 去重
  ↓
建立 parentUuid
  ↓
追加 JSONL
  ↓
Session 退出
```

Resume：

```Plain Text
扫描 Session 文件
  ↓
用户选择 Session
  ↓
loadTranscriptFile()
  ↓
解析消息和 metadata
  ↓
应用 compact / snip / replacement
  ↓
寻找最新 leaf
  ↓
沿 parentUuid 重建有效链
  ↓
恢复 Session ID、worktree、mode、cost、Agent 状态
  ↓
继续 query()
  ↓
继续向同一个 JSONL 追加
```

核心思想是：

> Transcript 保存“发生过的一切”，Resume 负责从这些 append\-only 事件中计算出“当前仍然有效的会话状态”。
> 
> 



# 子agent

子 Agent 的本质是：在同一个 Claude Code 进程里，创建一套独立的 `ToolUseContext`，然后再次启动一个新的 `query()` 循环。它通常不是新 Node 进程。

## 整体调用链

```Plain Text
主 Agent 的 query()
  │
  ├─ 模型输出 Agent tool_use
  │
  ▼
runTools()
  ▼
runToolUse()
  ▼
AgentTool.call()
  │
  ├─ 选择 Agent 类型、模型、工具和权限
  ├─ 构造子 Agent Prompt / Messages
  ├─ 创建独立 ToolUseContext
  ▼
runAgent()
  ▼
子 Agent 的 query()
  │
  ├─ 调用 Read / Bash / Edit 等工具
  ├─ 工具结果继续加入子 Agent 上下文
  └─ 直到输出最终答案
  ▼
AgentTool 将结果返回主 Agent
```

入口在主 `query()` 对工具调用的处理：

- \[query\.ts \(line 1376\)\]\(E:/claude code/src/query\.ts:1376\) 调用 `runTools(...)`

- \[toolExecution\.ts \(line 337\)\]\(E:/claude code/src/services/tools/toolExecution\.ts:337\) 根据工具名找到 `AgentTool`

- \[toolExecution\.ts \(line 1207\)\]\(E:/claude code/src/services/tools/toolExecution\.ts:1207\) 执行 `tool.call(...)`

- 最终进入 \[AgentTool\.tsx \(line 239\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:239\)

## AgentTool 负责调度

模型产生的调用大致是：

```Plain Text
{
  "name": "Agent",
  "input": {
    "description": "分析登录流程",
    "prompt": "找到登录请求的完整调用链",
    "subagent_type": "Explore",
    "run_in_background": false
  }
}
```

`AgentTool.call()` 首先选择 Agent 定义：

\[AgentTool\.tsx \(line 318\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:318\)

```Plain Text
const effectiveType =
  subagent_type ??
  (isForkSubagentEnabled()
    ? undefined
    : GENERAL_PURPOSE_AGENT.agentType)
```

Agent 定义决定：

- 使用哪个 system prompt

- 能使用哪些工具

- 使用哪个模型

- 权限模式

- 最大执行轮数

- 是否后台运行

- 是否使用 worktree 隔离

## 普通子 Agent 默认不继承父对话

普通子 Agent 的初始消息只有任务描述：

\[AgentTool\.tsx \(line 513\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:513\)

```Plain Text
promptMessages = [
  createUserMessage({
    content: prompt,
  }),
]
```

同时使用该 Agent 自己的 System Prompt：

```Plain Text
const agentPrompt = selectedAgent.getSystemPrompt({
  toolUseContext,
})
```

因此普通子 Agent看到的是：

```Plain Text
Agent 专属 System Prompt
+
父 Agent 分配的任务 prompt
```

它看不到父 Agent 完整的聊天历史。父 Agent必须把必要背景写进 `prompt`。

## Fork 子 Agent 会继承父上下文

Fork 模式是另一条路径：

\[AgentTool\.tsx \(line 483\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:483\)

它复用：

- 父 Agent 已渲染好的 System Prompt

- 父 Agent 完整消息历史

- 父 Agent 完整工具定义

- 父 Agent thinking 配置

调用参数在：

\[AgentTool\.tsx \(line 603\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:603\)

```Plain Text
{
  override: {
    systemPrompt: forkParentSystemPrompt,
  },
  availableTools: toolUseContext.options.tools,
  forkContextMessages: toolUseContext.messages,
  useExactTools: true,
}
```

`buildForkedMessages()` 会复制当前 assistant 消息，并为所有并行的 `tool_use` 构造占位 `tool_result`：

\[forkSubagent\.ts \(line 107\)\]\(E:/claude code/src/tools/AgentTool/forkSubagent\.ts:107\)

这样多个 Fork 子 Agent 的请求前缀几乎完全相同，有利于 Prompt Cache 命中。

所以：

## 创建隔离的 ToolUseContext

真正的“子 Agent 身份”由 `createSubagentContext()` 创建：

\[utils/forkedAgent\.ts \(line 345\)\]\(E:/claude code/src/utils/forkedAgent\.ts:345\)

它会创建：

- 新的 `agentId`

- 新的 query tracking chain

- 克隆后的文件读取缓存

- 独立的权限拒绝状态

- 独立或共享的 AbortController

- 独立消息数组

- 禁用控制父 UI 的回调

例如：

```Plain Text
agentId: overrides?.agentId ?? createAgentId(),

queryTracking: {
  chainId: randomUUID(),
  depth: (parentContext.queryTracking?.depth ?? -1) + 1,
}
```

这属于逻辑隔离，不一定是文件系统隔离。

默认情况下，父子 Agent仍然访问相同工作目录。如果指定：

```Plain Text
{
  "isolation": "worktree"
}
```

才会创建独立 Git worktree：

\[AgentTool\.tsx \(line 582\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:582\)

## 子 Agent 再次运行 query\(\)

核心位于：

\[runAgent\.ts \(line 748\)\]\(E:/claude code/src/tools/AgentTool/runAgent\.ts:748\)

```Plain Text
for await (const message of query({
  messages: initialMessages,
  systemPrompt: agentSystemPrompt,
  userContext: resolvedUserContext,
  systemContext: resolvedSystemContext,
  canUseTool,
  toolUseContext: agentToolUseContext,
  querySource,
  maxTurns,
})) {
  yield message
}
```

所以主 Agent 和子 Agent使用的是同一个 `query()` 引擎。

区别只是传入的数据不同：

```Plain Text
主 Agent query
  messages = 主会话
  tools = 主工具池
  context = 主 ToolUseContext

子 Agent query
  messages = 子任务上下文
  tools = 子 Agent 工具池
  context = 子 ToolUseContext
```

子 Agent调用 Bash、Read、Edit 时，也会再次经过：

```Plain Text
query()
 → runTools()
 → runToolUse()
 → tool.call()
```

因此它本身就是一套完整的 Agent 循环。

## 同步和后台执行

同步子 Agent：

```Plain Text
主 Agent暂停
→ 子 Agent执行完成
→ 子 Agent最终答案作为 Agent tool_result
→ 主 Agent继续推理
```

后台子 Agent：

```Plain Text
AgentTool立即返回 agentId
→ runAgent在后台继续
→ 完成后 enqueueAgentNotification()
→ 主 query收到 <task-notification>
→ 主 Agent处理结果
```

后台启动位于 \[AgentTool\.tsx \(line 713\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:713\)，完成通知位于 \[AgentTool\.tsx \(line 978\)\]\(E:/claude code/src/tools/AgentTool/AgentTool\.tsx:978\)。

## 子 Agent 的 Transcript

子 Agent不会把每次工具调用全部塞进主会话，而是写入独立 sidechain JSONL：

\[sessionStorage\.ts \(line 247\)\]\(E:/claude code/src/utils/sessionStorage\.ts:247\)

```Plain Text
<session-id>/
└─ subagents/
   ├─ agent-<agentId>.jsonl
   └─ agent-<agentId>.meta.json
```

JSONL 保存子 Agent完整过程；主会话通常只保存 AgentTool 的最终结果。这样可以避免子 Agent大量工具输出污染主上下文。

恢复子 Agent 时：

\[resumeAgent\.ts \(line 42\)\]\(E:/claude code/src/tools/AgentTool/resumeAgent\.ts:42\)

```Plain Text
读取 agent-<id>.jsonl
→ 恢复 messages
→ 恢复 agent 类型和 worktree
→ 追加新的用户 prompt
→ 再次调用 runAgent()
```

一句话总结：

> 子 Agent = 独立 Prompt \+ 独立 Messages \+ 独立 ToolUseContext \+ 独立 `query()` 循环；AgentTool 负责创建、调度、持久化，并把最终结果送回主 Agent。
> 
> 

# QueryEngine vs queryLoop

如果要仿照 Claude Code 搭一个 coding agent，最适合作为 v1 主线的是 `query()` 循环，而不是 REPL UI 或完整插件系统。

## 两层职责

`QueryEngine` 更像会话外壳，负责：

- 接收用户输入
- 处理 slash command / 本地命令 / 附件
- 构造 system prompt、user context、system context
- 初始化 `ToolUseContext`
- 写入 transcript
- 把内部消息转换成 SDK 或 CLI 输出

`queryLoop` 更像 agentic turn 状态机，负责：

- 在每轮请求前整理上下文
- 调用模型
- 收集 assistant 返回的 `tool_use`
- 执行工具
- 把 `tool_result` 作为 user message 放回上下文
- 决定继续下一轮还是结束

可以理解为：

```Plain Text
QueryEngine
  ├─ 准备输入、prompt、工具上下文、持久化
  └─ queryLoop
       ├─ model call
       ├─ tool_use collection
       ├─ tool execution
       ├─ tool_result injection
       └─ terminal decision
```

## queryLoop 状态转移

核心循环可以抽象成：

```Plain Text
messages
  ↓
裁剪/压缩/预算处理
  ↓
callModel(systemPrompt, messages, tools)
  ↓
AssistantMessage
  ├─ 没有 tool_use → completed
  └─ 有 tool_use
        ↓
      runTools()
        ↓
      UserMessage(tool_result)
        ↓
      messages = messages + assistant + tool_result
        ↓
      下一轮 callModel
```

这说明 `query()` 不是一次普通问答，而是一个异步生成器驱动的多轮工具循环。

## 终止原因

一个成熟 query loop 不应该只有 “正常结束” 一种状态，至少要区分：

- `completed`：assistant 没有继续调用工具
- `max_turns`：工具轮次超过上限
- `aborted_streaming` / `aborted_tools`：用户中断或 abort signal 触发
- `prompt_too_long`：上下文超过模型限制，且恢复失败
- `max_output_tokens` recovery exhausted：输出被截断后多次续写仍失败
- `permission_denied`：工具权限拒绝后模型无法继续
- `hook_stopped`：hook 明确阻止继续
- `token_budget_stop`：预算策略认为继续收益不足

这些 terminal reason 很适合作为自己项目里的测试断言。

## 工具配对不变量

工具协议里最重要的不变量是：

```Plain Text
AssistantMessage: tool_use(id = X)
        ↓
UserMessage: tool_result(tool_use_id = X)
```

每个 `tool_use` 都必须有匹配的 `tool_result`。如果 resume 后发现缺失，不应该悄悄忽略，因为下一次 API 请求可能直接失败。

所以自研 agent 的 v1 就应该加入严格校验：

- assistant 有 `tool_use`
- 下一条 user message 必须包含对应 `tool_result`
- 多个并行 tool use 可以合并到一个 user message 中
- transcript resume 时重新校验这些配对

## 工程化边界

从 query loop 入手实现时，可以先做这些最小闭环：

- `ModelClient` 抽象接口，先用 mock 模型驱动测试
- `Tool` 基类，包含 name、description、input schema、call
- 工具输入校验，不把模型生成的 JSON 直接信任为合法参数
- workspace path guard，默认禁止访问工作区外路径
- 大工具结果预算，超限内容写入文件，上下文只保留路径和预览
- append-only transcript，用 JSONL 保存消息和 terminal 事件
- resume 时重建 messages，并校验 tool_use/tool_result 配对

我在 `coding_agent/` 中实现的 Python v1 就是按这个切面搭建：先证明 query 循环、工具协议、路径权限、持久化和测试是闭环的，再逐步接真实模型、写工具和子 agent。


