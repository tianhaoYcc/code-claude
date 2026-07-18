from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .context_manager import ContextConfig
from .memory import (
    MEMORY_TYPES,
    SESSION_MEMORY_HEADINGS,
    MemoryCheckpoint,
    MemoryConfig,
    MemoryError,
    MemoryRetriever,
    MemoryStateError,
    MemoryStore,
    last_memory_message_uuid,
    messages_after_cursor,
    sanitize_memory_text,
    serialize_messages_for_memory,
)
from .model_client import ModelClient
from .models import AssistantMessage, MemoryEvent, Message, TerminalResult
from .tool_registry import ToolRegistry
from .tools import (
    EditFileTool,
    GlobTool,
    GrepTool,
    InputValidationError,
    ListDirTool,
    PermissionError,
    ReadFileTool,
    ToolContext,
    ToolError,
    ToolResult,
    WriteFileTool,
    make_unified_diff,
)


SESSION_MEMORY_SYSTEM_PROMPT = """You are an isolated session-memory agent.
Maintain only summary.md. Use read_file, write_file, or edit_file to update it.
Do not call shell commands. Do not invent work, files, decisions, or test results.
Never store credentials, .env values, or raw secrets.
Finish with a short confirmation after the file is valid.
"""

DURABLE_MEMORY_SYSTEM_PROMPT = """You are an isolated durable-memory agent.
Maintain MEMORY.md as an index and store content in topics/*.md.
Use only the provided file tools. Do not call shell commands.
Store only stable user, feedback, project, or reference memory.
Never store credentials, .env values, raw tool output, current-task progress,
temporary errors, or code facts that can be rediscovered by reading the repo.
If there is no durable memory to save, make no file changes and answer NO_MEMORY.
"""


@dataclass(frozen=True)
class MemorySnapshot:
    messages: Tuple[Message, ...]
    token_count: int
    target_uuid: str
    tool_call_total: int = 0


@dataclass(frozen=True)
class WorkerOutcome:
    status: str
    message: str
    advance_cursor: bool = True


class _CoalescingRunner:
    def __init__(
        self,
        handler: Callable[[MemorySnapshot], Awaitable[None]],
    ):
        self.handler = handler
        self.task: Optional[asyncio.Task] = None
        self.pending: Optional[MemorySnapshot] = None

    def schedule(self, snapshot: MemorySnapshot) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain(snapshot))
        else:
            self.pending = snapshot

    async def _drain(self, snapshot: MemorySnapshot) -> None:
        current: Optional[MemorySnapshot] = snapshot
        while current is not None:
            await self.handler(current)
            current = self.pending
            self.pending = None

    async def wait(self) -> None:
        task = self.task
        if task is not None:
            await task

    async def cancel(self) -> None:
        task = self.task
        self.pending = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class SessionMemoryWorker:
    def __init__(
        self,
        store: MemoryStore,
        model_client: ModelClient,
        config: MemoryConfig,
    ):
        self.store = store
        self.model_client = model_client
        self.config = config

    async def run(
        self,
        snapshot: MemorySnapshot,
        cursor_uuid: Optional[str],
    ) -> WorkerOutcome:
        self.store.ensure_summary_template()
        previous = self.store.read_summary() or ""
        previous_for_prompt = sanitize_memory_text(previous)
        cursor_was_stale = False
        try:
            delta = messages_after_cursor(snapshot.messages, cursor_uuid)
        except MemoryStateError:
            delta = messages_after_cursor(snapshot.messages, None)
            cursor_was_stale = True
        if not delta:
            return WorkerOutcome(status="skipped", message="No new session messages")

        headings = "\n".join("- %s" % heading for heading in SESSION_MEMORY_HEADINGS)
        prompt = """Update summary.md as a rolling checkpoint for the main coding agent.

Required headings:
%s

Rules:
- Preserve current state, user constraints, concrete paths, decisions, failures,
  completed tests, pending work, and the next action.
- Merge the new delta into the existing summary; do not append a second template.
- Keep the complete file below %d estimated tokens.
- Use write_file with overwrite=true or edit_file. The only valid path is summary.md.
- Do not include this instruction or conversational filler in the file.

Cursor status: %s

Existing summary.md:
<existing_summary>
%s
</existing_summary>

New transcript delta:
<message_delta>
%s
</message_delta>
""" % (
            headings,
            self.config.summary_max_tokens,
            "stale; rebuild from the supplied messages" if cursor_was_stale else "valid",
            previous_for_prompt,
            serialize_messages_for_memory(delta),
        )
        try:
            terminal, _ = await _run_child_query_loop(
                model_client=self.model_client,
                workspace_root=self.store.session_dir,
                tools=_session_memory_tools(),
                prompt=prompt,
                system_prompt=SESSION_MEMORY_SYSTEM_PROMPT,
                config=self.config,
            )
            if terminal.is_error:
                raise MemoryError(
                    "Session memory worker stopped: %s" % terminal.reason
                )
            updated = self.store.read_summary() or ""
            self.store.validate_summary(updated)
            if updated == previous:
                raise MemoryError("Session memory worker did not update summary.md")
        except BaseException:
            if self.store.summary_path.exists():
                self.store.write_text_atomic(self.store.summary_path, previous)
            raise
        return WorkerOutcome(
            status="updated",
            message="Session checkpoint updated%s"
            % (" after rebuilding a stale cursor" if cursor_was_stale else ""),
        )


class DurableMemoryWorker:
    def __init__(
        self,
        store: MemoryStore,
        model_client: ModelClient,
        config: MemoryConfig,
    ):
        self.store = store
        self.model_client = model_client
        self.config = config

    async def run(
        self,
        snapshot: MemorySnapshot,
        cursor_uuid: Optional[str],
    ) -> WorkerOutcome:
        self.store.ensure_layout()
        try:
            delta = messages_after_cursor(snapshot.messages, cursor_uuid)
        except MemoryStateError:
            delta = messages_after_cursor(snapshot.messages, None)
        if not delta:
            return WorkerOutcome(status="skipped", message="No new durable-memory input")

        before = self.store.persistent_snapshot()
        prompt = """Inspect the transcript delta and update durable project memory when needed.

Storage contract:
- MEMORY.md is only an index. Each entry is one line:
  - [short title](topics/file-name.md): description
- Put actual memory in topics/*.md with this frontmatter:
  ---
  type: user|feedback|project|reference
  keywords: comma, separated, retrieval, terms
  updated_at: ISO-8601 timestamp
  source_session: %s
  ---
- Update an existing topic instead of creating duplicates.
- Keep index entries below %d characters and at most %d topics.
- Stable memory types are: %s.
- If nothing qualifies, do not edit files and answer exactly NO_MEMORY.

New transcript delta:
<message_delta>
%s
</message_delta>
""" % (
            self.store.session_id,
            self.config.index_entry_max_chars,
            self.config.topic_max_files,
            ", ".join(sorted(MEMORY_TYPES)),
            serialize_messages_for_memory(delta),
        )
        try:
            terminal, assistant_text = await _run_child_query_loop(
                model_client=self.model_client,
                workspace_root=self.store.root,
                tools=_durable_memory_tools(),
                prompt=prompt,
                system_prompt=DURABLE_MEMORY_SYSTEM_PROMPT,
                config=self.config,
            )
            if terminal.is_error:
                raise MemoryError(
                    "Durable memory worker stopped: %s" % terminal.reason
                )
            after = self.store.persistent_snapshot()
            if after == before:
                return WorkerOutcome(
                    status="skipped",
                    message=(
                        "No durable memory extracted"
                        if "NO_MEMORY" in assistant_text
                        else "Durable memory worker made no file changes"
                    ),
                )
            self.store.validate_persistent_memory()
        except BaseException:
            self.store.restore_persistent_snapshot(before)
            raise
        return WorkerOutcome(status="updated", message="Durable memory updated")


class MemoryManager:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        model_client: ModelClient,
        config: MemoryConfig,
        transcript_path: Optional[Path] = None,
    ):
        self.config = config
        self.store = MemoryStore(
            workspace_root=workspace_root,
            session_id=session_id,
            config=config,
            transcript_path=transcript_path,
        )
        self.store.ensure_layout()
        self.events: List[MemoryEvent] = []
        try:
            self.state = self.store.load_state()
        except MemoryStateError as exc:
            from .memory import MemoryState

            self.state = MemoryState(session_id=self.store.session_id)
            self._emit("session", "failed", message=str(exc))
        self.session_worker = SessionMemoryWorker(self.store, model_client, config)
        self.durable_worker = DurableMemoryWorker(self.store, model_client, config)
        self.session_runner = _CoalescingRunner(self._run_session_snapshot)
        self.durable_runner = _CoalescingRunner(self._run_durable_snapshot)
        self.state_lock = asyncio.Lock()
        self.closed = False
        self.retriever = MemoryRetriever(self.store, config)
        self._tool_call_total = self.state.tool_calls_since_summary
        self._last_summary_tool_call_total = 0

    @property
    def session_id(self) -> str:
        return self.store.session_id

    def recall(self, prompt: str) -> str:
        try:
            return self.retriever.recall(prompt)
        except Exception as exc:
            self._emit("durable", "failed", message="Memory recall failed: %s" % exc)
            return ""

    def note_tool_calls(self, count: int) -> None:
        if count <= 0:
            return
        self._tool_call_total += count
        self.state.tool_calls_since_summary = max(
            0, self._tool_call_total - self._last_summary_tool_call_total
        )

    def note_compaction(self, token_count: int) -> None:
        self.state.last_summary_token_count = max(0, int(token_count))

    def maybe_schedule_session(
        self,
        messages: Sequence[Message],
        token_count: int,
        safe_point: str,
    ) -> bool:
        if self.closed:
            return False
        target_uuid = last_memory_message_uuid(messages)
        if not target_uuid or target_uuid == self.state.last_session_summary_message_uuid:
            return False
        if self.state.last_session_summary_message_uuid is None:
            eligible = token_count >= self.config.session_start_tokens
        else:
            eligible = (
                token_count - self.state.last_summary_token_count
                >= self.config.session_update_tokens
            )
        if not eligible:
            return False
        if (
            safe_point == "tool_batch"
            and self.state.tool_calls_since_summary
            < self.config.session_min_tool_calls
        ):
            return False
        snapshot = MemorySnapshot(
            messages=tuple(messages),
            token_count=max(0, int(token_count)),
            target_uuid=target_uuid,
            tool_call_total=self._tool_call_total,
        )
        self.session_runner.schedule(snapshot)
        self._emit(
            "session",
            "scheduled",
            from_uuid=self.state.last_session_summary_message_uuid,
            to_uuid=target_uuid,
            message="Session checkpoint scheduled at %s" % safe_point,
        )
        return True

    def schedule_durable(self, messages: Sequence[Message], token_count: int) -> bool:
        if self.closed:
            return False
        target_uuid = last_memory_message_uuid(messages)
        if not target_uuid or target_uuid == self.state.last_durable_memory_message_uuid:
            return False
        snapshot = MemorySnapshot(
            messages=tuple(messages),
            token_count=max(0, int(token_count)),
            target_uuid=target_uuid,
            tool_call_total=self._tool_call_total,
        )
        self.durable_runner.schedule(snapshot)
        self._emit(
            "durable",
            "scheduled",
            from_uuid=self.state.last_durable_memory_message_uuid,
            to_uuid=target_uuid,
            message="Durable memory extraction scheduled",
        )
        return True

    async def prepare_for_compaction(
        self,
        messages: Sequence[Message],
        token_count: int,
    ) -> None:
        self.maybe_schedule_session(messages, token_count, "compact")
        await self.flush(kind="session")

    def checkpoint(self) -> Optional[MemoryCheckpoint]:
        try:
            return self.store.checkpoint()
        except Exception as exc:
            self._emit(
                "session",
                "failed",
                message="Session checkpoint unavailable: %s" % exc,
            )
            return None

    def drain_events(self) -> List[MemoryEvent]:
        drained = list(self.events)
        self.events.clear()
        return drained

    async def flush(self, kind: Optional[str] = None) -> None:
        runners = []
        if kind in (None, "session"):
            runners.append(("session", self.session_runner))
        if kind in (None, "durable"):
            runners.append(("durable", self.durable_runner))
        if not runners:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(runner.wait() for _, runner in runners)),
                timeout=self.config.flush_timeout_seconds,
            )
        except asyncio.TimeoutError:
            for runner_kind, runner in runners:
                task = runner.task
                timed_out = task is not None and (
                    task.cancelled() or not task.done()
                )
                await runner.cancel()
                if timed_out:
                    self._emit(
                        runner_kind,
                        "failed",
                        message="Memory worker timed out after %.1f seconds"
                        % self.config.flush_timeout_seconds,
                    )

    async def close(self) -> None:
        if self.closed:
            return
        await self.flush()
        self.closed = True
        try:
            self.store.save_state(self.state)
        except Exception as exc:
            self._emit("session", "failed", message="Saving memory state failed: %s" % exc)

    async def _run_session_snapshot(self, snapshot: MemorySnapshot) -> None:
        from_uuid = self.state.last_session_summary_message_uuid
        try:
            outcome = await self.session_worker.run(snapshot, from_uuid)
            if outcome.advance_cursor:
                async with self.state_lock:
                    self.state.last_session_summary_message_uuid = snapshot.target_uuid
                    self.state.last_summary_token_count = snapshot.token_count
                    self._last_summary_tool_call_total = snapshot.tool_call_total
                    self.state.tool_calls_since_summary = max(
                        0,
                        self._tool_call_total - self._last_summary_tool_call_total,
                    )
                    self.store.save_state(self.state)
            self._emit(
                "session",
                outcome.status,
                from_uuid=from_uuid,
                to_uuid=snapshot.target_uuid,
                message=outcome.message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(
                "session",
                "failed",
                from_uuid=from_uuid,
                to_uuid=snapshot.target_uuid,
                message=str(exc),
            )

    async def _run_durable_snapshot(self, snapshot: MemorySnapshot) -> None:
        from_uuid = self.state.last_durable_memory_message_uuid
        try:
            outcome = await self.durable_worker.run(snapshot, from_uuid)
            if outcome.advance_cursor:
                async with self.state_lock:
                    self.state.last_durable_memory_message_uuid = snapshot.target_uuid
                    self.store.save_state(self.state)
            self._emit(
                "durable",
                outcome.status,
                from_uuid=from_uuid,
                to_uuid=snapshot.target_uuid,
                message=outcome.message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(
                "durable",
                "failed",
                from_uuid=from_uuid,
                to_uuid=snapshot.target_uuid,
                message=str(exc),
            )

    def _emit(
        self,
        kind: str,
        status: str,
        from_uuid: Optional[str] = None,
        to_uuid: Optional[str] = None,
        message: str = "",
    ) -> None:
        self.events.append(
            MemoryEvent(
                kind=kind,
                status=status,
                session_id=self.store.session_id,
                from_uuid=from_uuid,
                to_uuid=to_uuid,
                message=message,
            )
        )


async def _run_child_query_loop(
    model_client: ModelClient,
    workspace_root: Path,
    tools: Sequence,
    prompt: str,
    system_prompt: str,
    config: MemoryConfig,
) -> Tuple[TerminalResult, str]:
    from .query_loop import QueryLoop, QueryLoopConfig

    child = QueryLoop(
        model_client=model_client,
        workspace_root=workspace_root,
        registry=ToolRegistry(tools),
        transcript=None,
        config=QueryLoopConfig(
            max_turns=config.worker_max_turns,
            max_inline_tool_result_chars=0,
            read_permission="allow",
            write_permission="allow",
            shell_permission="deny",
            context=ContextConfig(auto_compact_enabled=False),
            memory=MemoryConfig(enabled=False),
            system_prompt=system_prompt,
        ),
    )
    terminal: Optional[TerminalResult] = None
    text_parts: List[str] = []
    try:
        async for event in child.run(prompt):
            if isinstance(event, AssistantMessage) and event.text_content():
                text_parts.append(event.text_content())
            elif isinstance(event, TerminalResult):
                terminal = event
    finally:
        await child.aclose()
    if terminal is None:
        raise MemoryError("Memory worker returned no terminal result")
    return terminal, "\n".join(text_parts)


class _ScopedReadFileTool(ReadFileTool):
    def __init__(self, scope: str):
        self.scope = scope

    async def call(self, input_data: Dict, context: ToolContext) -> ToolResult:
        _validate_memory_path(
            context, str(input_data["file_path"]), self.scope, directory=False
        )
        result = await super().call(input_data, context)
        result.content = sanitize_memory_text(result.content)
        if isinstance(result.raw, dict) and "content" in result.raw:
            result.raw["content"] = sanitize_memory_text(str(result.raw["content"]))
        return result


class _ScopedListDirTool(ListDirTool):
    def __init__(self, scope: str):
        self.scope = scope

    async def call(self, input_data: Dict, context: ToolContext) -> ToolResult:
        raw_path = str(input_data.get("path") or ".")
        path = _validate_memory_path(context, raw_path, self.scope, directory=True)
        if self.scope == "durable" and path == context.workspace_root.resolve():
            context.check_permission("read", self.name, path, "list memory directory")
            entries = [name for name in ("MEMORY.md", "topics/") if (path / name.rstrip("/")).exists()]
            return ToolResult(
                content="Directory: %s\n%s" % (path, "\n".join(entries)),
                raw={"path": str(path), "entries": entries},
            )
        return await super().call(input_data, context)


class _ScopedGlobTool(GlobTool):
    def __init__(self, scope: str):
        self.scope = scope

    async def call(self, input_data: Dict, context: ToolContext) -> ToolResult:
        path = _validate_memory_path(
            context, str(input_data.get("path") or "."), self.scope, directory=True
        )
        if path == context.workspace_root.resolve():
            raise PermissionError("glob is restricted to the topics directory")
        return await super().call(input_data, context)


class _ScopedGrepTool(GrepTool):
    def __init__(self, scope: str):
        self.scope = scope

    async def call(self, input_data: Dict, context: ToolContext) -> ToolResult:
        path = _validate_memory_path(
            context, str(input_data.get("path") or "."), self.scope, directory=True
        )
        if path == context.workspace_root.resolve():
            raise PermissionError("grep is restricted to the topics directory")
        return await super().call(input_data, context)


class _AtomicMemoryWriteTool(WriteFileTool):
    def __init__(self, scope: str):
        self.scope = scope

    async def call(self, input_data: Dict, context: ToolContext) -> ToolResult:
        path = _validate_memory_path(
            context, str(input_data["file_path"]), self.scope, directory=False
        )
        context.check_permission("write", self.name, path, "write memory file")
        create_dirs = bool(input_data.get("create_dirs", False))
        overwrite = bool(input_data.get("overwrite", False))
        if path.exists() and not path.is_file():
            raise ToolError("Path is not a file: %s" % path)
        if path.exists() and not overwrite:
            raise ToolError("File already exists and overwrite=false: %s" % path)
        if not path.parent.exists():
            if create_dirs:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                raise ToolError("Parent directory does not exist: %s" % path.parent)
        old_text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        new_text = sanitize_memory_text(str(input_data["content"]))
        MemoryStore.write_text_atomic(path, new_text)
        diff = make_unified_diff(old_text, new_text, path)
        return ToolResult(
            content="Wrote memory file: %s\nDiff:\n%s" % (path, diff),
            raw={"file_path": str(path), "diff": diff},
        )


class _AtomicMemoryEditTool(EditFileTool):
    def __init__(self, scope: str):
        self.scope = scope

    async def call(self, input_data: Dict, context: ToolContext) -> ToolResult:
        path = _validate_memory_path(
            context, str(input_data["file_path"]), self.scope, directory=False
        )
        context.check_permission("write", self.name, path, "edit memory file")
        if not path.exists() or not path.is_file():
            raise ToolError("File does not exist: %s" % path)
        old_text = str(input_data["old_text"])
        if not old_text:
            raise InputValidationError("old_text must not be empty")
        original = path.read_text(encoding="utf-8", errors="replace")
        count = original.count(old_text)
        if count == 0:
            raise ToolError("old_text was not found in file: %s" % path)
        replacements = count if bool(input_data.get("replace_all", False)) else 1
        updated = sanitize_memory_text(
            original.replace(old_text, str(input_data["new_text"]), replacements)
        )
        MemoryStore.write_text_atomic(path, updated)
        diff = make_unified_diff(original, updated, path)
        return ToolResult(
            content="Edited memory file: %s\nDiff:\n%s" % (path, diff),
            raw={"file_path": str(path), "replacements": replacements, "diff": diff},
        )


def _session_memory_tools() -> List:
    return [
        _ScopedReadFileTool("session"),
        _AtomicMemoryWriteTool("session"),
        _AtomicMemoryEditTool("session"),
    ]


def _durable_memory_tools() -> List:
    return [
        _ScopedReadFileTool("durable"),
        _ScopedListDirTool("durable"),
        _ScopedGlobTool("durable"),
        _ScopedGrepTool("durable"),
        _AtomicMemoryWriteTool("durable"),
        _AtomicMemoryEditTool("durable"),
    ]


def _validate_memory_path(
    context: ToolContext,
    raw_path: str,
    scope: str,
    directory: bool,
) -> Path:
    path = context.resolve_path(raw_path)
    root = context.workspace_root.resolve()
    relative = path.relative_to(root)
    parts = relative.parts
    if scope == "session":
        allowed = not directory and relative.as_posix() == "summary.md"
    elif scope == "durable":
        if directory:
            allowed = len(parts) == 0 or relative.as_posix() in {".", "topics"}
        else:
            allowed = relative.as_posix() == "MEMORY.md" or (
                len(parts) == 2
                and parts[0] == "topics"
                and path.suffix.lower() == ".md"
            )
    else:
        allowed = False
    if not allowed:
        raise PermissionError("Memory worker path is outside its scope: %s" % path)
    return path
