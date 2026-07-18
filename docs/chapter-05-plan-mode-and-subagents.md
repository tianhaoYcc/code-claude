# 第五章 Plan Mode 与子 Agent：先规划，再协作

前四章已经让 Agent 拥有 query loop、工具、安全边界、上下文压缩和记忆。第五章解决两个新的问题：

1. 面对大改动时，怎样让 Agent 先调研和写计划，经过用户批准后再执行？
2. 一个 Agent 不适合独自完成全部搜索时，怎样把明确的小任务委派给隔离的子 Agent？

本章不是再造两套主循环。Plan mode 复用主 `QueryLoop`，只改变提示词、工具集合和权限策略；子 Agent 则为每次委派创建独立、受限的 `QueryLoop`。

需要按函数调用顺序阅读时，可以配合 [第五章重点函数流程图谱](chapter-05-function-flow-diagrams.md)。其中包含 CLI、QueryLoop、Plan 状态机、计划审批、写入安全、compact/resume、子 Agent、并发和取消的 Mermaid 图。

```text
主 QueryLoop
├── execute mode：可以按原权限读、写、运行命令
├── plan mode：只能调研并修改本 session 的 plan.md
└── agent tool
    ├── explore 子 QueryLoop
    ├── plan 子 QueryLoop
    └── general-purpose 子 QueryLoop
```

## 1. 为什么 Plan mode 不是另一套循环

普通执行模式和规划模式都遵循同一个 agentic turn：

```text
组装 system prompt 和 messages
  ↓
调用模型
  ↓
收集 tool_use
  ↓
执行工具并返回 tool_result
  ↓
继续或终止
```

它们真正不同的是运行时策略：

| 项目 | execute mode | plan mode |
| --- | --- | --- |
| system prompt | 基础执行提示词 | 基础提示词 + 规划约束 |
| 模型可见工具 | 原有工具、`enter_plan_mode`、`agent` | 读/搜索、计划文件写入、`agent`、`exit_plan_mode` |
| 文件写入 | 服从原有 write 权限 | 只能写当前 session 的 `plan.md` |
| Shell | 服从原有 shell 权限 | 完全隐藏且执行时拒绝 |
| 无工具最终回答 | 正常结束 | 注入 meta 提醒并继续规划 |
| 离开模式 | 不适用 | 计划落盘并通过用户审批 |

因此本项目把模式看成 `QueryLoop` 的状态，而不是新建 `PlanQueryLoop`。这样工具配对、transcript、compact、memory 和终止条件都可以继续复用。

## 2. Claude Code 源码中的对应机制

本章主要对照以下 Claude Code 源码：

- `src/tools/EnterPlanModeTool/EnterPlanModeTool.ts`
- `src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts`
- `src/utils/plans.ts`
- `src/tools/AgentTool/AgentTool.tsx`
- `src/tools/AgentTool/runAgent.ts`
- `src/tools/AgentTool/built-in/exploreAgent.ts`
- `src/tools/AgentTool/built-in/planAgent.ts`
- `src/tools/AgentTool/built-in/generalPurposeAgent.ts`

Claude Code 的 `EnterPlanModeTool` 使用空参数 schema，并把 permission mode 切换为 `plan`。其返回内容会提醒模型进行代码探索、设计方案，并在完成后调用 ExitPlanMode。

`ExitPlanModeV2Tool` 会先校验当前是否真的处于 plan mode，再读取计划文件并进入用户确认流程。`src/utils/plans.ts` 负责计划目录、session plan slug、resume 和 fork 时的计划文件处理。

`AgentTool` 与 `runAgent()` 则负责根据 Agent definition 解析工具、模型、消息和取消控制器，构建 Agent 专属上下文，再运行独立查询过程。同步子 Agent 可以共享父级取消信号，工具集合则由角色定义重新解析。

我们的 Python 版保留这些核心思想，但有意缩小范围：只做前台三角色，不做后台 Agent、resume、worktree、MCP 和自定义 Agent 文件。

## 3. Plan mode 的持久化状态

实现位于 `coding_agent/agent/plan_mode.py`。每个 session 使用独立目录：

```text
.agent_plans/
└── <session-id>/
    ├── plan.md
    └── state.json
```

`PlanState` 保存：

- `mode`：`execute` 或 `plan`
- `plan_version`：当前计划版本
- `approved_version`：最近批准的版本
- `active_plan`：批准计划是否仍在执行
- `source_message_uuid`：本次规划来自哪条用户消息
- `updated_at`：状态更新时间

`PlanStore` 负责路径约束、计划读取和校验，以及通过临时文件加 `os.replace()` 原子更新 `state.json`。计划必须非空、不得超过约 12K tokens，并在读取和批准前经过敏感信息过滤。

状态损坏时不会猜测之前是否已经批准。系统会失效旧批准并降级到 Plan mode，让用户重新确认。这是一种 fail-closed 策略：宁可多规划一次，也不在未知状态下继续修改项目。

## 4. 进入与退出的状态迁移

`PlanManager` 管理模式转换：

```text
execute
  │ enter_plan_mode + 用户同意
  ▼
plan
  │ 写入 plan.md
  │ exit_plan_mode
  ├── 用户拒绝 ──> 保持 plan，反馈进入 tool_result
  └── 用户批准 ──> execute + active approved plan
                         │
                         └── 无工具最终回答后标记完成
```

执行模式下，模型可调用：

```json
{"name": "enter_plan_mode", "input": {}}
```

CLI 注入的审批回调决定是否允许切换。用户显式传入 `--plan` 时，表示已经作出进入决定，因此启动时直接进入 Plan mode，不再重复询问。

计划完成后，模型调用：

```json
{"name": "exit_plan_mode", "input": {}}
```

`ExitPlanModeTool` 不相信模型声称的计划内容，而是从受保护的磁盘路径重新读取 `plan.md`。审批结果有三种：

- 批准：记录版本，切回 execute，并把批准计划注入后续 system prompt。
- 拒绝：保持 plan，将用户反馈作为匹配的 error `tool_result` 返回模型。
- 无回调、空计划或校验失败：返回 error `tool_result`，状态不变。

只有批准计划之后的正常无工具最终回答，才会把 `active_plan` 标记完成。abort、模型错误和 `max_turns` 都不会误报计划完成。

## 5. Plan mode 的三层约束

只在提示词里写“不要改代码”是不够的。模型可能误调用隐藏工具，也可能构造错误路径。本项目使用三层约束。

### 第一层：模式提示词

`effective_system_prompt()` 根据当前模式组合：

```text
基础 system prompt
+ Plan mode 工作约束或已批准计划
+ 本轮 memory recall
```

Plan mode 明确要求先探索、把最终方案写入计划文件，并以 `exit_plan_mode` 结束。如果模型直接输出普通最终回答，`QueryLoop` 不会结束，而是追加一条 meta user message 提醒它继续完成计划。

### 第二层：模型可见工具过滤

`QueryLoop.available_tools()` 在每次模型请求前动态计算工具集合：

- execute：隐藏 `exit_plan_mode`。
- plan：隐藏 `enter_plan_mode`、`powershell` 和普通项目写入能力。
- plan：只允许 `explore` 与 `plan` 子 Agent，不允许 `general-purpose`。

模型拿到的 OpenAI tool schema 也会随模式变化。`agent.subagent_type` 的 `enum` 只列出当前真正可用的角色。

### 第三层：执行时权限复查

隐藏工具不等于安全。恶意或异常模型仍可能伪造一个未出现在本轮 schema 里的 `tool_use`。

`ToolOrchestrator.run()` 因此接收本轮 `allowed_tool_names`，执行前再次确认工具是否可用。`ToolContext.permission_override` 还会复查文件路径：Plan mode 中，`write_file` 和 `edit_file` 只能操作当前 session 的 `plan.md`，项目文件、其他 session 计划、Shell 和越界路径都会返回权限错误。计划内容会在真正落盘前完成敏感信息过滤、非空检查和 12K token 上限检查。

进入和退出模式还必须独占一条 assistant 工具消息，不能与其他 `tool_use` 混在同一批次。这对应 Claude Code 模式工具的 deferred 语义，也避免模型在审批前生成的项目写入参数，在审批后沿用新权限继续执行。

这让“提示词约束、工具曝光、执行授权”形成完整防线。

## 6. PlanEvent、resume 与 compact

模式变化会产生 `PlanEvent`：

- `entered`
- `approved`
- `rejected`
- `completed`
- `invalidated`
- `failed`

事件会像其他 Agent event 一样写入 append-only transcript，CLI 也会打印模式、版本和说明。

resume 时，`PlanManager` 从 `.agent_plans/<session-id>/state.json` 恢复当前模式和批准版本。只要批准计划仍然有效，后续模型请求会继续收到该计划；若计划文件丢失或内容失效，旧批准会立即失效。

上下文压缩不把完整计划复制进 boundary，但会在 `compact_boundary` 中记录计划路径、模式和版本。恢复投影后，Plan manager 仍以磁盘状态为准重新注入计划，避免摘要成为权限状态的唯一来源。

## 7. 子 Agent 为什么需要独立 QueryLoop

子 Agent 不是给主 Agent 增加一段角色提示词。每个子 Agent 都应该有独立任务、工具集合、消息历史和终止原因，否则会出现三个问题：

1. 父会话中的无关内容消耗子任务上下文。
2. 子 Agent 可能继承不该拥有的写入或 Shell 能力。
3. 父子工具调用和 transcript 难以审计。

`SubagentManager` 因此为每次 `agent` 调用创建一个新的 `QueryLoop`。它只收到委派 prompt，并附加少量必要上下文：workspace、当前批准计划或规划状态、本轮召回的项目记忆。

它不会复制父 Agent 的完整 messages，也不会启用 memory、Plan mode 或 `agent` 工具，因此不能递归创建子 Agent。

## 8. 三类内置子 Agent

实现位于 `coding_agent/agent/subagents.py`。

| 类型 | 工具 | 典型用途 | 调度方式 |
| --- | --- | --- | --- |
| `explore` | read/list/glob/grep | 快速定位文件、调用链和已有模式 | 可并发 |
| `plan` | read/list/glob/grep | 比较方案、列关键文件和验证步骤 | 可并发 |
| `general-purpose` | 继承父级 read/write/shell 权限 | 执行一个明确的修改任务 | 串行 |

模型调用格式是：

```json
{
  "name": "agent",
  "input": {
    "description": "定位 Plan mode 的权限入口",
    "prompt": "阅读相关文件，返回关键类、函数和调用链，不要修改代码。",
    "subagent_type": "explore"
  }
}
```

`description` 用于父级日志和 transcript，`prompt` 是子 Agent 的完整任务，`subagent_type` 必须通过动态 schema enum 和执行时角色校验。

Plan mode 只能调用 `explore` 和 `plan`。`general-purpose` 会被 schema 隐藏，而且即使伪造调用也会在执行前拒绝，不会创建子循环。

## 9. 上下文、权限和 transcript 隔离

每个子 Agent 生成唯一 `agent-id`，并保存独立 transcript：

```text
.agent_sessions/
└── subagents/
    └── <session-id>/
        └── <agent-id>.jsonl
```

子 Agent 与父 Agent 共享 workspace root 和路径 guard，但工具注册表由角色重新创建：

- `explore` / `plan` 没有写工具和 PowerShell。
- `general-purpose` 只继承父 Agent 已配置的 read/write/shell 权限，不会自动升级权限。
- 所有子 QueryLoop 都关闭 memory、Plan mode 和 subagents。

子 Agent 结束后，父 Agent 不直接拼接它的完整消息历史，而是得到一个严格配对的 `tool_result`，其中包含：

- 最终文本
- agent id
- subagent type
- terminal reason
- transcript 路径

这样主 query loop 仍保持 `assistant tool_use -> user tool_result` 的协议不变量。

## 10. 并发、顺序、取消与失败降级

`AgentTool.is_concurrency_safe()` 根据角色动态返回：

- `explore` / `plan` 为并发安全。
- `general-purpose` 为非并发安全。

现有 `partition_tool_calls()` 会把连续的只读 Agent 调用放入同一并发批次，并通过 semaphore 把默认并发数限制为 3。结果即使完成顺序不同，也会按原始 `tool_use` 顺序返回。

通用子 Agent 可能写文件或运行命令，因此始终进入串行批次，避免共享工作区上的并发写冲突。

父 `cancel_event` 会传播到前台子 QueryLoop。每个子 Agent 默认最多 8 turns，并有整体超时。取消、超时、空输出、模型异常和 `max_turns` 都只转换成当前 `agent` 调用的 error `tool_result`，不会让主 QueryLoop 崩溃。过长结果继续复用 `.agent_outputs/` 的预览和落盘预算机制。

## 11. CLI 使用方式

从规划模式启动：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m agent.cli "请先调研并制定实现计划" --workspace . --model-client openai --plan
```

让模型在执行模式中自行判断是否进入 Plan mode：

```powershell
& "E:\Anconda\python.exe" -m agent.cli "实现一个任务依赖系统，复杂时先进入规划模式" --workspace . --model-client openai --write-permission ask --shell-permission ask
```

配置子 Agent：

```powershell
& "E:\Anconda\python.exe" -m agent.cli "并行调研 query loop 和工具编排，再汇总方案" --workspace . --model-client openai --plan --max-subagents 3 --subagent-max-turns 8 --subagent-model-id your-model-id
```

相关参数：

- `--plan`：显式从 Plan mode 启动。
- `--no-plan-mode`：不注册进入/退出规划工具。
- `--no-subagents`：不注册 `agent` 工具。
- `--subagent-model-id`：指定子 Agent 模型。
- `--subagent-max-turns`：限制每个子 Agent 的轮数。
- `--max-subagents`：限制并发只读子 Agent 数量。

模型选择顺序为：`--subagent-model-id`、`LLM_SUBAGENT_MODEL_ID`、主模型。

观察计划和子 Agent transcript：

```powershell
Get-ChildItem ".agent_plans" -Recurse -File
Get-ChildItem ".agent_sessions\subagents" -Recurse -File
```

## 12. 测试覆盖

第五章测试位于 `coding_agent/tests/test_plan_and_subagents.py`，覆盖：

- execute、plan、批准、拒绝和完成的完整状态迁移
- 空计划、回调缺失和损坏状态的安全降级
- Plan mode 项目写入、Shell、越界路径和伪造隐藏工具调用拒绝
- 普通文本不能绕过 `exit_plan_mode`
- compact boundary 计划元数据和 resume 恢复
- 三类 Agent 的工具集合、上下文隔离和独立 transcript
- 禁止嵌套 Agent 和 Plan mode 中禁止 general-purpose
- 只读 Agent 真并发、结果顺序稳定和通用 Agent 串行
- 父级取消、子 Agent 超时和错误降级

运行完整测试：

```powershell
cd /d "E:\code claude\coding_agent"
& "E:\Anconda\python.exe" -m unittest discover -s tests -v
```

## 13. 当前边界

这一版刻意没有实现：

- 后台 Agent 与进度轮询
- 子 Agent resume
- 嵌套 Agent
- 任务列表、依赖 DAG 和 owner
- worktree 隔离与并行写入
- 自定义 Agent Markdown 文件
- Agent 专属 MCP server
- 父子 token、cost 和调用树可视化

当前实现已经形成最小完整闭环：主 Agent 可以进入受硬权限保护的 Plan mode，调用多个只读子 Agent 调研，把方案写入 `plan.md`，在用户批准后切回执行模式；执行阶段还可以把明确任务交给串行的通用子 Agent。下一章可以在这个基础上增加任务系统与后台 Agent，让协作从一次工具调用扩展为可追踪的长期任务图。
