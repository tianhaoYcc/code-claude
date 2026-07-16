from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, List, Optional, Sequence, Tuple, Union

from .context_manager import (
    COMPACTION_SYSTEM_PROMPT,
    ContextConfig,
    ContextManager,
)
from .model_client import ModelClient, ModelRequestError
from .models import (
    AgentEvent,
    AssistantMessage,
    CompactionEvent,
    Message,
    RequestStartEvent,
    TerminalResult,
    ToolEvent,
    ToolUseBlock,
    UserMessage,
)
from .tool_orchestration import ToolBatchResult, ToolOrchestrator
from .tool_registry import ToolRegistry, default_registry
from .tools import (
    PermissionCallback,
    PermissionPolicy,
    Tool,
    ToolContext,
)
from .transcript import Transcript


@dataclass
class QueryLoopConfig:
    max_turns: int = 8
    max_inline_tool_result_chars: int = 4000
    max_bad_tool_input_attempts: int = 3
    max_tool_concurrency: int = 10
    shell_timeout_seconds: int = 30
    shell_max_output_chars: int = 30000
    read_permission: str = "allow"
    write_permission: str = "deny"
    shell_permission: str = "deny"
    permission_callback: Optional[PermissionCallback] = None
    disabled_tools: Tuple[str, ...] = ()
    context: ContextConfig = field(default_factory=ContextConfig)
    system_prompt: str = (
        "You are a coding agent. Use tools when they are useful. "
        "Every tool_use must be answered with a matching tool_result. "
        "Tool input must be a valid JSON object matching the tool schema. "
        "If a tool_result is_error=true because of invalid input, retry with "
        "corrected arguments instead of repeating the same call."
    )


class QueryLoop:
    """Claude Code-style agentic query loop."""

    def __init__(
        self,
        model_client: ModelClient,
        workspace_root: Path,
        tools: Optional[Sequence[Tool]] = None,
        registry: Optional[ToolRegistry] = None,
        transcript: Optional[Transcript] = None,
        config: Optional[QueryLoopConfig] = None,
        initial_messages: Optional[Sequence[Message]] = None,
        summary_model_client: Optional[ModelClient] = None,
    ):
        if tools is not None and registry is not None:
            raise ValueError("Pass either tools or registry, not both")
        self.model_client = model_client
        self.summary_model_client = summary_model_client or model_client
        self.workspace_root = Path(workspace_root).resolve()
        self.transcript = transcript
        self.config = config or QueryLoopConfig()
        self.messages: List[Message] = list(initial_messages or [])
        self.bad_tool_input_attempts = 0
        self.context_manager = ContextManager(self.config.context)
        self.registry = registry or (
            ToolRegistry(tools) if tools is not None else default_registry()
        )
        for tool_name in self.config.disabled_tools:
            self.registry.disable(tool_name)
        self.tool_context = ToolContext(
            workspace_root=self.workspace_root,
            output_dir=self.workspace_root / ".agent_outputs",
            permission_policy=PermissionPolicy(
                read=self.config.read_permission,
                write=self.config.write_permission,
                shell=self.config.shell_permission,
                callback=self.config.permission_callback,
            ),
            shell_timeout_seconds=self.config.shell_timeout_seconds,
            shell_max_output_chars=self.config.shell_max_output_chars,
        )
        self.orchestrator = ToolOrchestrator(
            registry=self.registry,
            context=self.tool_context,
            max_concurrency=self.config.max_tool_concurrency,
            max_inline_tool_result_chars=self.config.max_inline_tool_result_chars,
        )

    @property
    def tools(self) -> List[Tool]:
        return list(self.registry.all_tools())

    def available_tools(self) -> List[Tool]:
        return list(
            self.registry.available_tools(self.tool_context.permission_policy)
        )

    def register_tool(self, tool: Tool, replace: bool = False) -> None:
        self.registry.register(tool, replace=replace)

    def cancel(self) -> None:
        self.tool_context.cancel_event.set()

    def active_messages(self) -> List[Message]:
        return self.context_manager.project(self.messages)

    async def compact(
        self,
        trigger: str = "manual",
    ) -> AsyncIterator[CompactionEvent]:
        async for event in self._compact(trigger=trigger, force_full=True):
            yield event

    async def run(self, prompt: Optional[str] = None) -> AsyncIterator[AgentEvent]:
        if prompt is not None:
            user_message = UserMessage(content=prompt)
            self.messages.append(user_message)
            self._record(user_message)
            yield user_message

        turn_count = 1
        while True:
            if self.tool_context.cancel_event.is_set():
                terminal = TerminalResult(
                    reason="aborted",
                    turn_count=max(0, turn_count - 1),
                    is_error=True,
                    message="Query loop was cancelled",
                )
                self._record(terminal)
                yield terminal
                return

            async for compaction_event in self._maybe_auto_compact():
                yield compaction_event

            request_event = RequestStartEvent(turn_count=turn_count)
            self._record(request_event)
            yield request_event

            assistant_messages: List[AssistantMessage] = []
            prompt_too_long_retried = False
            while True:
                pending_messages: List[AssistantMessage] = []
                try:
                    async for assistant_message in self.model_client.stream(
                        tuple(self.active_messages()),
                        tuple(self.available_tools()),
                        self.config.system_prompt,
                    ):
                        pending_messages.append(assistant_message)
                except ModelRequestError as exc:
                    if exc.prompt_too_long and not prompt_too_long_retried:
                        compacted = False
                        async for compaction_event in self._compact(
                            trigger="prompt_too_long",
                            force_full=True,
                        ):
                            if compaction_event.status == "completed":
                                compacted = True
                            yield compaction_event
                        if compacted:
                            prompt_too_long_retried = True
                            continue

                    reason = "prompt_too_long" if exc.prompt_too_long else "model_error"
                    terminal = TerminalResult(
                        reason=reason,
                        turn_count=turn_count,
                        is_error=True,
                        message=str(exc),
                    )
                    self._record(terminal)
                    yield terminal
                    return

                assistant_messages = pending_messages
                for assistant_message in assistant_messages:
                    self.messages.append(assistant_message)
                    self._record(assistant_message)
                    yield assistant_message
                break

            if self.tool_context.cancel_event.is_set():
                terminal = TerminalResult(
                    reason="aborted",
                    turn_count=turn_count,
                    is_error=True,
                    message="Query loop was cancelled",
                )
                self._record(terminal)
                yield terminal
                return

            tool_uses: List[ToolUseBlock] = []
            source_assistant_uuid: Optional[str] = None
            for assistant_message in assistant_messages:
                uses = assistant_message.tool_uses()
                if uses and source_assistant_uuid is None:
                    source_assistant_uuid = assistant_message.uuid
                tool_uses.extend(uses)

            if not tool_uses:
                terminal = TerminalResult(reason="completed", turn_count=turn_count)
                self._record(terminal)
                yield terminal
                return

            result_message: Optional[UserMessage] = None
            async for update in self._run_tool_batch(
                tool_uses, source_assistant_uuid
            ):
                if isinstance(update, ToolEvent):
                    yield update
                else:
                    result_message = update
            if result_message is None:
                raise RuntimeError("Tool batch did not produce a result message")
            self.messages.append(result_message)
            self._record(result_message)
            yield result_message

            if self.tool_context.cancel_event.is_set():
                terminal = TerminalResult(
                    reason="aborted",
                    turn_count=turn_count,
                    is_error=True,
                    message="Query loop was cancelled",
                )
                self._record(terminal)
                yield terminal
                return

            if (
                self.config.max_bad_tool_input_attempts
                and self.bad_tool_input_attempts
                >= self.config.max_bad_tool_input_attempts
            ):
                terminal = TerminalResult(
                    reason="bad_tool_arguments",
                    turn_count=turn_count,
                    is_error=True,
                    message=(
                        "Reached max_bad_tool_input_attempts=%d"
                        % self.config.max_bad_tool_input_attempts
                    ),
                )
                self._record(terminal)
                yield terminal
                return

            next_turn_count = turn_count + 1
            if self.config.max_turns and next_turn_count > self.config.max_turns:
                terminal = TerminalResult(
                    reason="max_turns",
                    turn_count=next_turn_count,
                    is_error=True,
                    message="Reached max_turns=%d" % self.config.max_turns,
                )
                self._record(terminal)
                yield terminal
                return

            turn_count = next_turn_count

    async def _maybe_auto_compact(self) -> AsyncIterator[CompactionEvent]:
        token_count = self.context_manager.current_token_count(
            self.messages,
            self.available_tools(),
            self.config.system_prompt,
        )
        if not self.context_manager.should_auto_compact(token_count):
            return
        async for event in self._compact(trigger="auto", force_full=False):
            yield event

    async def _compact(
        self,
        trigger: str,
        force_full: bool,
    ) -> AsyncIterator[CompactionEvent]:
        available_tools = self.available_tools()
        before_tokens = self.context_manager.current_token_count(
            self.messages,
            available_tools,
            self.config.system_prompt,
        )
        started = CompactionEvent(
            status="started",
            trigger=trigger,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )
        self._record(started)
        yield started

        micro_boundary = self.context_manager.build_microcompact_boundary(
            self.messages,
            available_tools,
            self.config.system_prompt,
        )
        if micro_boundary is not None:
            self.messages.append(micro_boundary)
            self._record(micro_boundary)
            after_micro_tokens = self.context_manager.current_token_count(
                self.messages,
                available_tools,
                self.config.system_prompt,
            )
            micro_event = CompactionEvent(
                status="microcompacted",
                trigger=trigger,
                before_tokens=before_tokens,
                after_tokens=after_micro_tokens,
                message="Compacted %d old tool result(s)"
                % len(micro_boundary.metadata.get("tool_use_ids") or []),
            )
            self._record(micro_event)
            yield micro_event

            if (
                not force_full
                and after_micro_tokens
                < self.context_manager.config.auto_compact_threshold
            ):
                self.context_manager.note_compaction_success()
                completed = CompactionEvent(
                    status="completed",
                    trigger=trigger,
                    before_tokens=before_tokens,
                    after_tokens=after_micro_tokens,
                    message="Microcompact reduced context below the threshold",
                )
                self._record(completed)
                yield completed
                return

        try:
            plan = self.context_manager.build_full_compaction_plan(
                self.messages,
                available_tools,
                self.config.system_prompt,
            )
            summary_attempts = 0
            while True:
                summary_messages: List[AssistantMessage] = []
                try:
                    async for summary_message in self.summary_model_client.stream(
                        tuple(self.context_manager.summary_request_messages(plan)),
                        (),
                        COMPACTION_SYSTEM_PROMPT,
                    ):
                        summary_messages.append(summary_message)
                except ModelRequestError as exc:
                    if exc.prompt_too_long and summary_attempts < 3:
                        truncated_plan = (
                            self.context_manager.truncate_plan_for_prompt_too_long(
                                plan
                            )
                        )
                        if truncated_plan is not None:
                            plan = truncated_plan
                            summary_attempts += 1
                            continue
                    raise
                break

            if not summary_messages:
                raise RuntimeError("Compaction model returned no message")
            if any(message.tool_uses() for message in summary_messages):
                raise RuntimeError("Compaction model attempted to call a tool")
            summary_text = "\n".join(
                message.text_content() for message in summary_messages
                if message.text_content()
            )
            summary_usage = next(
                (
                    message.usage
                    for message in reversed(summary_messages)
                    if message.usage is not None
                ),
                None,
            )
            boundary, summary = self.context_manager.commit_full_compaction(
                self.messages,
                plan,
                summary_text,
                available_tools,
                self.config.system_prompt,
                summary_usage,
            )
            self.messages.extend([boundary, summary])
            self._record(boundary)
            self._record(summary)
            after_tokens = self.context_manager.current_token_count(
                self.messages,
                available_tools,
                self.config.system_prompt,
            )
        except Exception as exc:
            self.context_manager.note_compaction_failure()
            failed = CompactionEvent(
                status="failed",
                trigger=trigger,
                before_tokens=before_tokens,
                after_tokens=self.context_manager.current_token_count(
                    self.messages,
                    available_tools,
                    self.config.system_prompt,
                ),
                message=str(exc),
            )
            self._record(failed)
            yield failed
            return

        self.context_manager.note_compaction_success()
        completed = CompactionEvent(
            status="completed",
            trigger=trigger,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            message="Conversation summary and compact boundary were appended",
        )
        self._record(completed)
        yield completed

    async def _run_tool_batch(
        self,
        tool_uses: Sequence[ToolUseBlock],
        source_assistant_uuid: Optional[str],
    ) -> AsyncIterator[Union[ToolEvent, UserMessage]]:
        batch_result: Optional[ToolBatchResult] = None
        async for update in self.orchestrator.run(tool_uses):
            if isinstance(update, ToolEvent):
                self._record(update)
                yield update
            else:
                batch_result = update

        if batch_result is None:
            raise RuntimeError("Tool orchestrator did not return a batch result")
        self.bad_tool_input_attempts += batch_result.bad_input_count
        raw_results = {
            outcome.tool_use.id: outcome.raw_result
            for outcome in batch_result.outcomes
        }
        yield UserMessage(
            content=[outcome.content_block for outcome in batch_result.outcomes],
            is_meta=True,
            tool_use_result=raw_results,
            source_tool_assistant_uuid=source_assistant_uuid,
        )

    def _record(self, event: AgentEvent) -> None:
        if self.transcript is not None:
            self.transcript.append_event(event)
