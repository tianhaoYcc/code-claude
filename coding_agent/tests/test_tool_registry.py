from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.mock_model import ScriptedModelClient
from agent.models import AssistantMessage, TerminalResult
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.tool_registry import ToolRegistry
from agent.tools import PermissionPolicy, Tool, ToolContext, ToolResult


class NamedTool(Tool):
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str, action: str = "read"):
        self.name = name
        self.description = "Test tool %s" % name
        self.permission_action = action

    def is_concurrency_safe(self, input_data):
        return self.permission_action == "read"

    async def call(self, input_data, context: ToolContext) -> ToolResult:
        if self.permission_action:
            context.check_permission(self.permission_action, self.name)
        return ToolResult("ran %s" % self.name)


async def collect_events(loop: QueryLoop, prompt: str):
    events = []
    async for event in loop.run(prompt):
        events.append(event)
    return events


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_register_duplicate_replace_disable_and_unregister(self):
        first = NamedTool("demo")
        second = NamedTool("demo")
        registry = ToolRegistry([first])

        with self.assertRaises(ValueError):
            registry.register(second)

        registry.register(second, replace=True)
        self.assertIs(registry.get("demo"), second)
        registry.disable("demo")
        self.assertTrue(registry.is_disabled("demo"))
        registry.enable("demo")
        self.assertFalse(registry.is_disabled("demo"))
        self.assertIs(registry.unregister("demo"), second)
        self.assertIsNone(registry.get("demo"))

    def test_available_tools_filter_disabled_and_blanket_denied_actions(self):
        registry = ToolRegistry(
            [
                NamedTool("reader", "read"),
                NamedTool("writer", "write"),
                NamedTool("shell", "shell"),
            ]
        )
        registry.disable("reader")
        policy = PermissionPolicy(read="allow", write="deny", shell="ask")

        names = [tool.name for tool in registry.available_tools(policy)]

        self.assertEqual(names, ["shell"])

    async def test_query_loop_exposes_only_allowed_registered_tools(self):
        seen = []

        def capture_tools(messages, tools, system_prompt):
            seen.extend(tool.name for tool in tools)
            return AssistantMessage.text("done")

        registry = ToolRegistry(
            [
                NamedTool("reader", "read"),
                NamedTool("writer", "write"),
                NamedTool("shell", "shell"),
            ]
        )
        model = ScriptedModelClient([capture_tools])
        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(
                model,
                Path(tmp),
                registry=registry,
                config=QueryLoopConfig(
                    read_permission="allow",
                    write_permission="deny",
                    shell_permission="ask",
                ),
            )
            events = await collect_events(loop, "inspect tools")

        self.assertEqual(seen, ["reader", "shell"])
        self.assertIsInstance(events[-1], TerminalResult)

    async def test_tool_can_be_registered_after_query_loop_construction(self):
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("late_tool", {}),
                AssistantMessage.text("done"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(model, Path(tmp), tools=[])
            loop.register_tool(NamedTool("late_tool"))

            await collect_events(loop, "run late tool")

        result_message = loop.messages[-2]
        self.assertIn("ran late_tool", str(result_message.content))

    async def test_disabled_tool_is_not_executed_even_if_model_calls_it(self):
        tool = NamedTool("disabled")
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("disabled", {}),
                AssistantMessage.text("done"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(
                model,
                Path(tmp),
                tools=[tool],
                config=QueryLoopConfig(disabled_tools=("disabled",)),
            )
            await collect_events(loop, "call disabled")

        self.assertIn("Tool is disabled", str(loop.messages[-2].content))


if __name__ == "__main__":
    unittest.main()
