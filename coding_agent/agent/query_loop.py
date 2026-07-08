from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

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
    tool_result_block,
)
from .tools import (
    Tool,
    ToolContext,
    ToolError,
    apply_tool_result_budget,
    default_tools,
    find_tool,
)
from .transcript import Transcript


@dataclass
class QueryLoopConfig:
    max_turns: int = 8
    max_inline_tool_result_chars: int = 4000
    system_prompt: str = (
        "You are a coding agent. Use tools when they are useful. "
        "Every tool_use must be answered with a matching tool_result."
    )


class QueryLoop:
    """Claude Code-style agentic query loop."""

    def __init__(
        self,
        model_client: ModelClient,
        workspace_root: Path,
        tools: Optional[Sequence[Tool]] = None,
        transcript: Optional[Transcript] = None,
        config: Optional[QueryLoopConfig] = None,
        initial_messages: Optional[Sequence[Message]] = None,
    ):
        self.model_client = model_client
        self.workspace_root = Path(workspace_root).resolve()
        self.tools = list(tools or default_tools())
        self.transcript = transcript
        self.config = config or QueryLoopConfig()
        self.messages: List[Message] = list(initial_messages or [])
        self.tool_context = ToolContext(
            workspace_root=self.workspace_root,
            output_dir=self.workspace_root / ".agent_outputs",
        )

    async def run(self, prompt: Optional[str] = None) -> AsyncIterator[AgentEvent]:
        if prompt is not None:
            user_message = UserMessage(content=prompt)
            self.messages.append(user_message)
            self._record(user_message)
            yield user_message

        turn_count = 1
        while True:
            request_event = RequestStartEvent(turn_count=turn_count)
            self._record(request_event)
            yield request_event

            assistant_messages: List[AssistantMessage] = []
            async for assistant_message in self.model_client.stream(
                tuple(self.messages),
                tuple(self.tools),
                self.config.system_prompt,
            ):
                assistant_messages.append(assistant_message)
                self.messages.append(assistant_message)
                self._record(assistant_message)
                yield assistant_message

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

            result_message = await self._run_tool_batch(
                tool_uses,
                source_assistant_uuid,
            )
            self.messages.append(result_message)
            self._record(result_message)
            yield result_message

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
    ) -> UserMessage:
        content_blocks: List[Dict[str, Any]] = []
        raw_results: Dict[str, Any] = {}

        for tool_use in tool_uses:
            start_event = ToolEvent(
                tool_use_id=tool_use.id,
                tool_name=tool_use.name,
                status="started",
            )
            self._record(start_event)

            tool = find_tool(self.tools, tool_use.name)
            if tool is None:
                error = "No such tool available: %s" % tool_use.name
                content_blocks.append(tool_result_block(tool_use.id, error, is_error=True))
                raw_results[tool_use.id] = {"error": error}
                self._record(
                    ToolEvent(tool_use.id, tool_use.name, "errored", message=error)
                )
                continue

            try:
                result = await tool.execute(tool_use.input, self.tool_context)
                content, raw = apply_tool_result_budget(
                    result,
                    tool_use.id,
                    self.tool_context,
                    self.config.max_inline_tool_result_chars,
                )
                content_blocks.append(tool_result_block(tool_use.id, content))
                raw_results[tool_use.id] = raw
                self._record(ToolEvent(tool_use.id, tool_use.name, "finished"))
            except ToolError as exc:
                error = "%s: %s" % (exc.__class__.__name__, exc)
                content_blocks.append(tool_result_block(tool_use.id, error, is_error=True))
                raw_results[tool_use.id] = {"error": error}
                self._record(
                    ToolEvent(tool_use.id, tool_use.name, "errored", message=error)
                )
            except Exception as exc:
                error = "Unexpected tool error: %s" % exc
                content_blocks.append(tool_result_block(tool_use.id, error, is_error=True))
                raw_results[tool_use.id] = {"error": error}
                self._record(
                    ToolEvent(tool_use.id, tool_use.name, "errored", message=error)
                )

        return UserMessage(
            content=content_blocks,
            is_meta=True,
            tool_use_result=raw_results,
            source_tool_assistant_uuid=source_assistant_uuid,
        )

    def _record(self, event: AgentEvent) -> None:
        if self.transcript is not None:
            self.transcript.append_event(event)
