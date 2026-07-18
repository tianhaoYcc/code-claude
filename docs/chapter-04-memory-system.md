# 第四章 双层记忆：让 Agent 记得住，也压得动

前三章已经让 agent 能循环调用工具、保护执行边界，并在上下文变长后进行压缩。但仅有 compact 还不够：

```text
compact 解决“这一次长对话怎么继续”；
memory 解决“下一次对话还要记住什么”。
```

如果把这两类信息都塞进一个 `MEMORY.md`，很快就会出现三个问题：当前任务进度污染长期知识、索引越来越大、压缩时找不到可靠的消息边界。因此本章按照 Claude Code 的思路，把记忆拆成两层：

- `summary.md`：当前会话的滚动检查点，服务于 auto/reactive compact。
- `MEMORY.md + topics/*.md`：跨会话长期记忆，服务于后续 query 的召回。

二者都是普通 Markdown，不是 embedding，也不依赖向量数据库。

## 1. Claude Code 里其实有两套记忆

可以对照本地 Claude Code 源码阅读：

- `src/memdir/memdir.ts`：把 `MEMORY.md` 定义成长期记忆索引，而不是正文容器。
- `src/memdir/memoryTypes.ts`：区分 user、feedback、project、reference，并排除当前任务的临时上下文。
- `src/memdir/memoryScan.ts`：扫描 topic 文件和元数据。
- `src/memdir/findRelevantMemories.ts`：从长期记忆 manifest 中选择相关 topic。
- `src/services/extractMemories/extractMemories.ts`：用隔离的 fork agent 增量提取长期记忆。
- `src/services/SessionMemory/prompts.ts`：规定当前会话摘要的结构和预算。
- `src/services/SessionMemory/sessionMemory.ts`：后台更新 session memory，并维护最后总结消息游标。
- `src/services/compact/autoCompact.ts`：自动压缩前优先尝试 session-memory compact。
- `src/services/compact/sessionMemoryCompact.ts`：把会话检查点转换成 compact summary，并保留近期原始消息。

Python 版的对应关系是：

| Claude Code | Python 版 | 作用 |
| --- | --- | --- |
| `memdir/*` | `agent/memory.py` | 长期索引、topic、召回与格式校验 |
| `extractMemories/*` | `DurableMemoryWorker` | 增量提取稳定长期记忆 |
| `SessionMemory/*` | `SessionMemoryWorker` | 滚动更新当前会话检查点 |
| forked query | 子 `QueryLoop` | 隔离模型调用和工具权限 |
| `sessionMemoryCompact.ts` | `ContextManager.commit_session_memory_compaction()` | 用检查点替换旧上下文 |
| compact/memory analytics | `MemoryEvent` / `CompactionEvent` | 观察调度、成功、跳过和失败 |

这里不是照搬 Claude Code 全部实现，而是保留最值得学习的四条不变量：

1. 长期记忆和当前会话状态分开。
2. 每次只处理游标之后的新消息。
3. 记忆 Worker 只能访问专用目录。
4. 记忆失败只能降级，不能终止主 query loop。

## 2. 运行时目录

启用记忆后，workspace 下会出现：

```text
.agent_memory/
├── MEMORY.md
├── topics/
│   └── <topic>.md
└── sessions/
    └── <session-id>/
        ├── summary.md
        └── state.json
```

`.agent_memory/` 已加入 `.gitignore`。它和 `.agent_sessions/`、`.agent_outputs/` 一样属于本机运行数据，不应该跟代码一起提交。

`session-id` 默认取 transcript 文件名。例如：

```text
.agent_sessions/abc.jsonl
                ↓
.agent_memory/sessions/abc/summary.md
```

因此使用同一个 `--session ... --resume` 时，会恢复同一份会话检查点和游标。

## 3. summary.md 存的是什么

`summary.md` 是模型生成、人在磁盘上可以直接阅读的结构化 Markdown：

```markdown
# Session Memory

## Current State
正在实现双层记忆，query loop 接入已经完成。

## Task Specification
实现 session memory 和 durable memory，并保证失败降级。

## Files and Functions
- agent/query_loop.py：主循环接入点
- agent/memory_manager.py：Worker 和后台调度

## Workflow
先生成检查点，再尝试 session-memory compact。

## Errors and Corrections
指定的 yolov8/python.exe 当前是 0 字节文件。

## Key Discoveries
MEMORY.md 不是压缩兜底文件。

## Key Results
session compact 保持 tool_use/tool_result 原子组。

## Next Steps
运行完整测试。

## Worklog
完成存储、Worker、query loop 和 CLI 接入。
```

它不是以下任何东西：

- 不是原消息数组的 JSON 序列化。
- 不是 token ID。
- 不是 embedding。
- 不是完整 transcript 的替代品。

它更像一个提前维护的“任务状态检查点”。完整历史仍保存在 append-only transcript 中。

## 4. state.json 为什么必须存在

只保存摘要文本还不够，runtime 还必须知道“总结到了哪条消息”。`state.json` 记录：

```json
{
  "schema_version": 1,
  "session_id": "abc",
  "last_session_summary_message_uuid": "...",
  "last_durable_memory_message_uuid": "...",
  "last_summary_token_count": 15000,
  "tool_calls_since_summary": 3,
  "updated_at": "2026-07-16T10:00:00Z"
}
```

两个游标相互独立：

- session cursor 决定下一次更新 `summary.md` 时从哪里开始。
- durable cursor 决定下一次提取长期记忆时从哪里开始。

所有状态写入都使用临时文件加 `os.replace()`。只有 Worker 输出通过校验后才推进游标；失败时下一次仍会重新处理同一段消息。

## 5. SessionMemoryWorker 的触发机制

默认触发参数来自 Claude Code 的滚动检查点思路：

```text
首次初始化：当前上下文达到 10,000 tokens
后续更新：相比上次检查点增长 5,000 tokens
工具安全点：至少累计 3 次工具调用
```

模型不会在任意半截状态下更新摘要，只会选择自然边界：

- 一批工具完整执行并产生 `tool_result` 后。
- assistant 给出无工具最终回答后。
- auto/reactive compact 即将开始时。

每次 Worker 收到的是：

```text
旧 summary.md
        +
last_session_summary_message_uuid 之后的新消息
```

而不是每次重新提交整个 transcript。这既节省 token，也避免旧信息被反复改写。

## 6. 为什么 Worker 要使用子 QueryLoop

两个 Worker 都会创建隔离的子 `QueryLoop`：

- 最多 5 turns。
- 不保存自己的 transcript。
- 关闭 memory，防止“记忆 agent 再启动记忆 agent”。
- 关闭自动 compact。
- 不注册 PowerShell/Shell。

Session worker 只能访问当前 session 的 `summary.md`。Durable worker 只能访问 `MEMORY.md` 和 `topics/*.md`，不能写 `sessions/`。

写工具仍然使用正常的 tool protocol：

```text
memory model 输出 tool_use(write_file/edit_file)
  ↓
路径 scope + schema + write permission 检查
  ↓
原子写入
  ↓
tool_result 返回子 agent
  ↓
子 agent 确认完成
```

这让第四章不仅讲“模型总结”，也复用了第一、二章的 query loop、工具注册和权限边界。

## 7. 后台任务如何避免重复执行

Session worker 和 durable worker 各自只有一个 in-flight task。执行期间再次触发时，不会并行启动第二个同类 Worker，而是覆盖一份 pending snapshot：

```text
正在处理 snapshot A
  ↓ 新触发 B
pending = B
  ↓ 新触发 C
pending = C
  ↓ A 完成
只追加处理 C
```

这叫 coalescing。它保留最新状态，同时避免模型调用堆积。

auto compact 和 CLI 退出前会 flush Worker，默认最多等待 15 秒。超时后任务会被取消并产生 `MemoryEvent(status="failed")`，主流程继续使用旧检查点或旧 compact 路径。

## 8. session-memory compact 的实际消息结构

自动压缩顺序现在变成：

```text
microcompact
  ↓ 仍然超限
等待 SessionMemoryWorker
  ↓
尝试使用 summary.md
  ↓ 不可用/游标错误/仍超限
调用原来的 full compact
```

手动 `--compact` 仍然强制调用现有摘要模型，避免把用户明确要求的“重新压缩”偷偷替换成旧检查点。

检查点有效时，会向完整 transcript 追加：

```text
SystemMessage(subtype="compact_boundary")
UserMessage(is_meta=True, is_compact_summary=True)
```

摘要 user message 大致是：

```text
This session continues from an earlier conversation...

<context_summary source="session_memory">
  summary.md 的内容
</context_summary>

Full transcript: ...jsonl
Recent messages are preserved verbatim...
```

boundary 保存 summary UUID、checkpoint UUID、已总结消息 UUID 和保留消息 UUID。近期消息会向前扩展到至少约 10K tokens 和 5 个文本组，最多 40K tokens；`tool_use/tool_result` 始终作为一个原子组移动。

以下情况会直接回退 full compact：

- `summary.md` 不存在或仍是空模板。
- state 游标在当前活跃消息中找不到。
- 必需章节缺失或摘要包含敏感信息。
- Worker 失败或 15 秒内没有完成。
- 使用检查点后仍达到 auto compact 阈值。

## 9. MEMORY.md 为什么只存索引

长期记忆正文放在 topic 文件：

```markdown
---
type: project
keywords: query loop, tool protocol, pairing
updated_at: 2026-07-16T10:00:00Z
source_session: abc
---

# Tool protocol

每个 assistant tool_use 后必须有匹配的 user tool_result。
```

`MEMORY.md` 只保存一行指针：

```markdown
# Memory Index
- [Tool protocol](topics/tool-protocol.md): query loop 的工具调用配对约束
```

当前支持四种长期记忆：

- `user`：稳定的用户偏好和背景。
- `feedback`：用户对 agent 行为的长期纠正。
- `project`：不能靠重新读一两个文件轻易恢复的项目约定和决定。
- `reference`：以后仍有价值的资料或位置指针。

不会保存：当前任务进度、临时报错、可以重新读取的代码事实、原始大工具输出、API key、token、密码和 `.env` 内容。

## 10. 长期记忆如何召回

每个新用户 turn 开始时：

1. 固定读取 `MEMORY.md` 前 200 行。
2. 根据新 prompt 与 topic 的 keywords、标题和索引描述做词法评分。
3. keywords 命中权重最高，普通词重合次之，更新时间只用于同分排序。
4. 自动注入最多 3 个正相关 topic。
5. 索引和 topic 合计最多约 8,000 tokens。

选择结果作为 `<project_memory>` 追加到本轮 system prompt，并在这一轮所有工具调用中保持不变。下一次用户输入再重新选择。

这里故意没有 embedding：

- v1 不需要向量数据库和额外依赖。
- 确定性检索更容易写单元测试和解释面试题。
- topic 数量较少时，keywords 已经足够。

Claude Code 可以使用额外模型从 manifest 中选出最多若干 topic；Python v1 先采用最多 3 个 topic 的词法选择，这是本章最明确的一处简化。

## 11. 事件与失败降级

`MemoryEvent` 包含：

```text
kind: session | durable
status: scheduled | updated | skipped | failed
session_id
from_uuid
to_uuid
message
```

CLI 输出示例：

```text
[memory] durable scheduled Durable memory extraction scheduled
[terminal] reason=completed turns=1
[memory] durable skipped No durable memory extracted
```

后台完成事件可能出现在 terminal 之后，因为 CLI 会在主 query loop 结束后执行 `QueryLoop.aclose()`。事件仍会追加进同一份 transcript。

## 12. 启动与观察

记忆在 CLI 中默认开启：

```powershell
Set-Location "E:\code claude\coding_agent"
$PY = "E:\Anconda\python.exe"
& $PY -m agent.cli "分析 query loop，并记住这个项目要求 README 使用中文" --workspace . --model-client openai
```

可以给记忆 Worker 使用不同模型：

```dotenv
LLM_MEMORY_MODEL_ID=你的记忆模型ID
```

也可以通过 CLI 覆盖：

```powershell
& $PY -m agent.cli "继续任务" --workspace . --model-client openai --memory-model-id your-memory-model
```

关闭记忆：

```powershell
& $PY -m agent.cli "只运行普通 query loop" --workspace . --model-client openai --no-memory
```

指定 workspace 内的其他记忆目录：

```powershell
& $PY -m agent.cli "继续任务" --workspace . --model-client openai --memory-root ".local_memory"
```

查看最新运行数据：

```powershell
Get-Content ".agent_memory\MEMORY.md" -Encoding utf8
Get-ChildItem ".agent_memory\topics" -File
Get-ChildItem ".agent_memory\sessions" -Recurse -File
```

恢复最近 session：

```powershell
$SESSION = (Get-ChildItem ".agent_sessions\*.jsonl" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1).FullName

& $PY -m agent.cli "继续刚才的任务" --workspace . --model-client openai --session $SESSION --resume
```

运行测试：

```powershell
& $PY -m unittest discover -s tests -v
```

第四章测试覆盖触发阈值、coalescing、Worker 失败和超时、游标推进、resume 增量、目录隔离、长期 topic 写入、敏感信息过滤、词法召回、工具配对和 session-memory compact 回退。

## 13. 当前边界

- 记忆存储在 workspace 内，而不是 Claude Code 的用户级项目目录。
- 召回是 keywords 词法评分，不是 LLM selector，也不是 embedding。
- 长期记忆质量取决于模型是否正确使用受限写工具；runtime 负责格式、范围和敏感信息校验。
- CLI 是短进程，因此退出前需要最多 15 秒 flush；常驻 TUI 可以让 Worker 更自然地后台运行。
- 当前没有全局用户记忆、记忆冲突图、遗忘权重和跨 workspace 共享。

但核心链路已经完整：会话检查点提前生成、自动压缩优先复用、失败回退旧 compact、长期知识独立存储、下一轮按预算召回。基于这份可持续的上下文，[第五章](chapter-05-plan-mode-and-subagents.md) 继续实现 Plan mode 和前台任务子 Agent。
