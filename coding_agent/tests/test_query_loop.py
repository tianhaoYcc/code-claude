from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.mock_model import ScriptedModelClient
from agent.models import AssistantMessage, TerminalResult, UserMessage
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.transcript import Transcript


async def collect_events(loop: QueryLoop, prompt: str):
    events = []
    async for event in loop.run(prompt):
        events.append(event)
    return events


def last_tool_result_text(messages):
    for message in reversed(messages):
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            parts = []
            for block in message.content:
                if block.get("type") == "tool_result":
                    parts.append(str(block.get("content", "")))
            return "\n".join(parts)
    return ""


class QueryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "hello.txt").write_text(
            "alpha\nbeta needle\ngamma\n",
            encoding="utf-8",
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_plain_answer_completes_without_tools(self):
        model = ScriptedModelClient([AssistantMessage.text("hello")])
        loop = QueryLoop(model, self.workspace)

        events = await collect_events(loop, "hi")

        self.assertEqual(model.calls, 1)
        self.assertIsInstance(events[-1], TerminalResult)
        self.assertEqual(events[-1].reason, "completed")

    async def test_single_tool_call_feeds_result_into_next_turn(self):
        def final(messages, tools, system_prompt):
            return AssistantMessage.text("saw: " + last_tool_result_text(messages))

        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"}),
                final,
            ]
        )
        loop = QueryLoop(model, self.workspace)

        events = await collect_events(loop, "read hello")

        self.assertEqual(model.calls, 2)
        self.assertIn("beta needle", last_tool_result_text(loop.messages))
        self.assertEqual(events[-1].reason, "completed")

    async def test_multi_round_read_then_grep(self):
        def grep_next(messages, tools, system_prompt):
            return AssistantMessage.tool_use(
                "grep",
                {"pattern": "needle", "path": ".", "glob": "*.txt"},
            )

        def final(messages, tools, system_prompt):
            return AssistantMessage.text("final:\n" + last_tool_result_text(messages))

        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"}),
                grep_next,
                final,
            ]
        )
        loop = QueryLoop(model, self.workspace)

        events = await collect_events(loop, "investigate")

        self.assertEqual(model.calls, 3)
        self.assertIn("hello.txt:2:beta needle", last_tool_result_text(loop.messages))
        self.assertEqual(events[-1].turn_count, 3)

    async def test_unknown_tool_returns_error_tool_result(self):
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("missing_tool", {"x": 1}),
                AssistantMessage.text("done"),
            ]
        )
        loop = QueryLoop(model, self.workspace)

        await collect_events(loop, "use bad tool")

        self.assertIn("No such tool available", last_tool_result_text(loop.messages))

    async def test_schema_error_returns_error_tool_result(self):
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {}),
                AssistantMessage.text("done"),
            ]
        )
        loop = QueryLoop(model, self.workspace)

        await collect_events(loop, "bad schema")

        self.assertIn("Missing required field", last_tool_result_text(loop.messages))

    async def test_path_outside_workspace_is_denied(self):
        outside = self.workspace.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {"file_path": str(outside)}),
                AssistantMessage.text("done"),
            ]
        )
        loop = QueryLoop(model, self.workspace)

        await collect_events(loop, "read outside")

        self.assertIn("outside workspace", last_tool_result_text(loop.messages))

    async def test_max_turns_stops_before_next_model_call(self):
        model = ScriptedModelClient(
            [AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"})]
        )
        loop = QueryLoop(
            model,
            self.workspace,
            config=QueryLoopConfig(max_turns=1),
        )

        events = await collect_events(loop, "read")

        self.assertEqual(model.calls, 1)
        self.assertEqual(events[-1].reason, "max_turns")

    async def test_transcript_resume_loads_valid_messages(self):
        transcript_path = self.workspace / "session.jsonl"
        transcript = Transcript(transcript_path)
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"}),
                AssistantMessage.text("done"),
            ]
        )
        loop = QueryLoop(model, self.workspace, transcript=transcript)

        await collect_events(loop, "read")
        loaded = transcript.load_messages(strict=True)

        self.assertGreaterEqual(len(loaded), 4)
        resumed_model = ScriptedModelClient([AssistantMessage.text("resumed")])
        resumed = QueryLoop(
            resumed_model,
            self.workspace,
            transcript=transcript,
            initial_messages=loaded,
        )
        events = await collect_events(resumed, "continue")

        self.assertEqual(events[-1].reason, "completed")

    async def test_large_tool_output_is_written_to_file(self):
        big_text = "\n".join("line %03d needle" % idx for idx in range(200))
        (self.workspace / "big.txt").write_text(big_text, encoding="utf-8")
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {"file_path": "big.txt", "limit": 300}),
                AssistantMessage.text("done"),
            ]
        )
        loop = QueryLoop(
            model,
            self.workspace,
            config=QueryLoopConfig(max_inline_tool_result_chars=120),
        )

        await collect_events(loop, "read big")
        result_text = last_tool_result_text(loop.messages)

        self.assertIn("Full output written to:", result_text)
        output_files = list((self.workspace / ".agent_outputs").glob("*.txt"))
        self.assertEqual(len(output_files), 1)
        self.assertIn("line 199 needle", output_files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
