from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple, Union

from .models import ToolEvent, ToolUseBlock, tool_result_block
from .tool_registry import ToolRegistry
from .tools import (
    InputValidationError,
    PermissionError,
    ToolCancelledError,
    ToolContext,
    ToolError,
    apply_tool_result_budget,
)


@dataclass
class ToolExecutionOutcome:
    index: int
    tool_use: ToolUseBlock
    content_block: Dict[str, Any]
    raw_result: Any
    event: ToolEvent
    bad_input: bool = False


@dataclass
class ToolBatchResult:
    outcomes: List[ToolExecutionOutcome]

    @property
    def bad_input_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.bad_input)


@dataclass
class ToolCallBatch:
    is_concurrency_safe: bool
    calls: List[Tuple[int, ToolUseBlock]]


OrchestrationUpdate = Union[ToolEvent, ToolBatchResult]


class ToolOrchestrator:
    def __init__(
        self,
        registry: ToolRegistry,
        context: ToolContext,
        max_concurrency: int = 10,
        max_inline_tool_result_chars: int = 4000,
    ):
        self.registry = registry
        self.context = context
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_inline_tool_result_chars = max_inline_tool_result_chars

    async def run(
        self,
        tool_uses: Sequence[ToolUseBlock],
        allowed_tool_names: Optional[Sequence[str]] = None,
    ) -> AsyncIterator[OrchestrationUpdate]:
        allowed = (
            set(str(name) for name in allowed_tool_names)
            if allowed_tool_names is not None
            else None
        )
        outcomes: List[Optional[ToolExecutionOutcome]] = [None] * len(tool_uses)
        for batch in partition_tool_calls(tool_uses, self.registry):
            if batch.is_concurrency_safe:
                async for update in self._run_concurrently(batch.calls, allowed):
                    if isinstance(update, ToolExecutionOutcome):
                        outcomes[update.index] = update
                        yield update.event
                    else:
                        yield update
            else:
                for index, tool_use in batch.calls:
                    yield ToolEvent(
                        tool_use_id=tool_use.id,
                        tool_name=tool_use.name,
                        status="started",
                    )
                    outcome = await self._execute_one(index, tool_use, allowed)
                    outcomes[index] = outcome
                    yield outcome.event

        complete = [outcome for outcome in outcomes if outcome is not None]
        if len(complete) != len(tool_uses):
            raise RuntimeError("Tool orchestration did not produce every tool result")
        yield ToolBatchResult(complete)

    async def _run_concurrently(
        self,
        calls: Sequence[Tuple[int, ToolUseBlock]],
        allowed_tool_names,
    ) -> AsyncIterator[Union[ToolEvent, ToolExecutionOutcome]]:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def execute(index: int, tool_use: ToolUseBlock):
            async with semaphore:
                return await self._execute_one(
                    index, tool_use, allowed_tool_names
                )

        tasks = []
        for index, tool_use in calls:
            yield ToolEvent(
                tool_use_id=tool_use.id,
                tool_name=tool_use.name,
                status="started",
            )
            tasks.append(asyncio.create_task(execute(index, tool_use)))

        try:
            for future in asyncio.as_completed(tasks):
                yield await future
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_one(
        self,
        index: int,
        tool_use: ToolUseBlock,
        allowed_tool_names=None,
    ) -> ToolExecutionOutcome:
        tool = self.registry.get(tool_use.name)
        if tool is None:
            return self._error_outcome(
                index,
                tool_use,
                "No such tool available: %s" % tool_use.name,
            )
        if self.registry.is_disabled(tool_use.name):
            return self._error_outcome(
                index,
                tool_use,
                "Tool is disabled: %s" % tool_use.name,
            )
        if (
            allowed_tool_names is not None
            and tool_use.name not in allowed_tool_names
        ):
            return self._error_outcome(
                index,
                tool_use,
                "Permission denied: tool is not available in the active "
                "agent mode: %s" % tool_use.name,
            )

        try:
            if self.context.cancel_event.is_set():
                raise ToolCancelledError("Tool execution was cancelled")
            result = await tool.execute(tool_use.input, self.context)
            content, raw = apply_tool_result_budget(
                result,
                tool_use.id,
                self.context,
                self.max_inline_tool_result_chars,
            )
            status = "errored" if result.is_error else "finished"
            message = "Tool returned an error result" if result.is_error else ""
            return ToolExecutionOutcome(
                index=index,
                tool_use=tool_use,
                content_block=tool_result_block(
                    tool_use.id,
                    content,
                    is_error=result.is_error,
                ),
                raw_result=raw,
                event=ToolEvent(
                    tool_use.id,
                    tool_use.name,
                    status,
                    message=message,
                ),
            )
        except InputValidationError as exc:
            error = (
                "%s: %s. Retry with a JSON object matching the tool schema."
                % (exc.__class__.__name__, exc)
            )
            return self._error_outcome(index, tool_use, error, bad_input=True)
        except ToolCancelledError as exc:
            return self._error_outcome(
                index,
                tool_use,
                "%s: %s" % (exc.__class__.__name__, exc),
                status="cancelled",
            )
        except (PermissionError, ToolError) as exc:
            return self._error_outcome(
                index,
                tool_use,
                "%s: %s" % (exc.__class__.__name__, exc),
            )
        except Exception as exc:
            return self._error_outcome(
                index,
                tool_use,
                "Unexpected tool error: %s" % exc,
            )

    def _error_outcome(
        self,
        index: int,
        tool_use: ToolUseBlock,
        error: str,
        bad_input: bool = False,
        status: str = "errored",
    ) -> ToolExecutionOutcome:
        return ToolExecutionOutcome(
            index=index,
            tool_use=tool_use,
            content_block=tool_result_block(tool_use.id, error, is_error=True),
            raw_result={"error": error},
            event=ToolEvent(
                tool_use.id,
                tool_use.name,
                status,
                message=error,
            ),
            bad_input=bad_input,
        )


def partition_tool_calls(
    tool_uses: Sequence[ToolUseBlock],
    registry: ToolRegistry,
) -> List[ToolCallBatch]:
    batches: List[ToolCallBatch] = []
    for index, tool_use in enumerate(tool_uses):
        is_safe = False
        tool = registry.get(tool_use.name)
        if tool is not None and not registry.is_disabled(tool_use.name):
            try:
                normalized = tool.prepare_input(tool_use.input)
                is_safe = bool(tool.is_concurrency_safe(normalized))
            except Exception:
                is_safe = False

        if is_safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].calls.append((index, tool_use))
        else:
            batches.append(
                ToolCallBatch(
                    is_concurrency_safe=is_safe,
                    calls=[(index, tool_use)],
                )
            )
    return batches
