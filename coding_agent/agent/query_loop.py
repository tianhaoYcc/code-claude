from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional, Sequence, Tuple, Union

from .model_client import ModelClient
from .models import (
    AgentEvent,
    AssistantMessage,
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
    ):
        if tools is not None and registry is not None:
            raise ValueError("Pass either tools or registry, not both")
        self.model_client = model_client
        self.workspace_root = Path(workspace_root).resolve()
        self.transcript = transcript
        self.config = config or QueryLoopConfig()
        self.messages: List[Message] = list(initial_messages or [])
        self.bad_tool_input_attempts = 0
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

            request_event = RequestStartEvent(turn_count=turn_count)
            self._record(request_event)
            yield request_event

            assistant_messages: List[AssistantMessage] = []
            async for assistant_message in self.model_client.stream(
                tuple(self.messages),
                tuple(self.available_tools()),
                self.config.system_prompt,
            ):
                assistant_messages.append(assistant_message)
                self.messages.append(assistant_message)
                self._record(assistant_message)
                yield assistant_message

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
