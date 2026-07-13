from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.mock_model import ScriptedModelClient
from agent.models import AssistantMessage, TerminalResult, ToolEvent, ToolUseBlock, UserMessage
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.tools import Tool, ToolCancelledError, ToolContext, ToolResult


class ProbeTool(Tool):
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name, state, delay=0.01, concurrency_safe=True):
        self.name = name
        self.description = "Probe %s" % name
        self.state = state
        self.delay = delay
        self.concurrency_safe = concurrency_safe

    def is_concurrency_safe(self, input_data):
        return self.concurrency_safe

    async def call(self, input_data, context: ToolContext) -> ToolResult:
        self.state["active"] += 1
        self.state["max_active"] = max(
            self.state["max_active"], self.state["active"]
        )
        self.state["timeline"].append("start:" + self.name)
        try:
            await asyncio.sleep(self.delay)
            return ToolResult("result:" + self.name)
        finally:
            self.state["timeline"].append("end:" + self.name)
            self.state["active"] -= 1


class CancelProbeTool(Tool):
    name = "wait_for_cancel"
    description = "Wait until the query loop is cancelled"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, started):
        self.started = started

    async def call(self, input_data, context: ToolContext) -> ToolResult:
        self.started.set()
        await context.cancel_event.wait()
        raise ToolCancelledError("cancel observed")


async def collect_events(loop: QueryLoop, prompt: str):
    events = []
    async for event in loop.run(prompt):
        events.append(event)
    return events


def new_state():
    return {"active": 0, "max_active": 0, "timeline": []}


class ToolOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrency_safe_tools_overlap_but_results_keep_input_order(self):
        state = new_state()
        slow = ProbeTool("slow", state, delay=0.08)
        fast = ProbeTool("fast", state, delay=0.01)
        first = AssistantMessage.from_tool_uses(
            [
                ToolUseBlock("call_slow", "slow", {}),
                ToolUseBlock("call_fast", "fast", {}),
            ]
        )
        model = ScriptedModelClient([first, AssistantMessage.text("done")])

        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(
                model,
                Path(tmp),
                tools=[slow, fast],
                config=QueryLoopConfig(max_tool_concurrency=2),
            )
            events = await collect_events(loop, "run both")

        self.assertEqual(state["max_active"], 2)
        result_message = next(
            event
            for event in events
            if isinstance(event, UserMessage) and event.is_meta
        )
        self.assertEqual(
            [block["tool_use_id"] for block in result_message.content],
            ["call_slow", "call_fast"],
        )
        tool_events = [event for event in events if isinstance(event, ToolEvent)]
        self.assertEqual([event.status for event in tool_events[:2]], ["started", "started"])
        self.assertEqual(
            [event.tool_name for event in tool_events if event.status == "finished"],
            ["fast", "slow"],
        )

    async def test_non_concurrency_safe_tool_splits_batches(self):
        state = new_state()
        first_read = ProbeTool("first_read", state, concurrency_safe=True)
        write = ProbeTool("write", state, concurrency_safe=False)
        second_read = ProbeTool("second_read", state, concurrency_safe=True)
        response = AssistantMessage.from_tool_uses(
            [
                ToolUseBlock("one", "first_read", {}),
                ToolUseBlock("two", "write", {}),
                ToolUseBlock("three", "second_read", {}),
            ]
        )
        model = ScriptedModelClient([response, AssistantMessage.text("done")])

        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(model, Path(tmp), tools=[first_read, write, second_read])
            await collect_events(loop, "ordered")

        self.assertEqual(state["max_active"], 1)
        self.assertEqual(
            state["timeline"],
            [
                "start:first_read",
                "end:first_read",
                "start:write",
                "end:write",
                "start:second_read",
                "end:second_read",
            ],
        )

    async def test_query_loop_cancel_stops_tool_and_returns_aborted_terminal(self):
        started = asyncio.Event()
        tool = CancelProbeTool(started)
        model = ScriptedModelClient([AssistantMessage.tool_use(tool.name, {})])

        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(model, Path(tmp), tools=[tool])
            task = asyncio.create_task(collect_events(loop, "wait"))
            await asyncio.wait_for(started.wait(), timeout=1)
            loop.cancel()
            events = await asyncio.wait_for(task, timeout=1)

        self.assertIsInstance(events[-1], TerminalResult)
        self.assertEqual(events[-1].reason, "aborted")
        self.assertIn(
            "cancelled",
            [event.status for event in events if isinstance(event, ToolEvent)],
        )


if __name__ == "__main__":
    unittest.main()
