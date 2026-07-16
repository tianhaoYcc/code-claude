from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.context_manager import (
    ContextConfig,
    ContextManager,
    group_atomic_messages,
)
from agent.model_client import ModelClient, ModelRequestError
from agent.mock_model import ScriptedModelClient
from agent.models import (
    AssistantMessage,
    CompactionEvent,
    SystemMessage,
    TerminalResult,
    TokenUsage,
    UserMessage,
    ensure_tool_result_pairing,
    tool_result_block,
)
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.transcript import Transcript


def tool_exchange(tool_use_id: str, content: str):
    return [
        AssistantMessage.tool_use(
            "read_file",
            {"file_path": "%s.txt" % tool_use_id},
            tool_use_id=tool_use_id,
        ),
        UserMessage(
            content=[tool_result_block(tool_use_id, content)],
            is_meta=True,
        ),
    ]


async def collect_run(loop: QueryLoop, prompt: str):
    events = []
    async for event in loop.run(prompt):
        events.append(event)
    return events


async def collect_compact(loop: QueryLoop):
    events = []
    async for event in loop.compact():
        events.append(event)
    return events


class PromptTooLongThenAnswer(ModelClient):
    def __init__(self, fail_count: int = 1):
        self.fail_count = fail_count
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise ModelRequestError("context too long", prompt_too_long=True)
        yield AssistantMessage.text("recovered")


class FailingSummaryModel(ModelClient):
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        if False:
            yield AssistantMessage.text("")
        raise RuntimeError("summary unavailable")


class ContextManagerTests(unittest.TestCase):
    def test_atomic_groups_do_not_split_tool_use_and_result(self):
        messages = [UserMessage("start")]
        messages.extend(tool_exchange("call_1", "result"))
        messages.append(AssistantMessage.text("done"))

        groups = group_atomic_messages(messages)

        self.assertEqual([len(group) for group in groups], [1, 2, 1])
        ensure_tool_result_pairing([message for group in groups for message in group])

    def test_microcompact_only_changes_projected_old_results(self):
        manager = ContextManager(
            ContextConfig(
                microcompact_keep_recent_tool_results=1,
                microcompact_min_chars=10,
            )
        )
        messages = [UserMessage("inspect")]
        messages.extend(tool_exchange("old_call", "old " * 100))
        messages.extend(tool_exchange("new_call", "new " * 100))

        boundary = manager.build_microcompact_boundary(messages)

        self.assertIsNotNone(boundary)
        self.assertEqual(boundary.metadata["tool_use_ids"], ["old_call"])
        full_history = messages + [boundary]
        projected = manager.project(full_history)
        ensure_tool_result_pairing(projected)

        original_old = messages[2].content[0]["content"]
        projected_old = projected[2].content[0]["content"]
        projected_new = projected[4].content[0]["content"]
        self.assertEqual(original_old, "old " * 100)
        self.assertIn("removed by microcompact", projected_old)
        self.assertEqual(projected_new, "new " * 100)

    def test_full_compaction_projects_summary_and_recent_groups(self):
        manager = ContextManager(ContextConfig(preserve_recent_groups=2))
        messages = [
            UserMessage("old question"),
            AssistantMessage.text("old answer"),
            UserMessage("recent question"),
            AssistantMessage.text("recent answer"),
        ]
        plan = manager.build_full_compaction_plan(messages)

        boundary, summary = manager.commit_full_compaction(
            messages,
            plan,
            "Goal: continue the recent task.",
        )
        projected = manager.project(messages + [boundary, summary])

        self.assertTrue(projected[0].is_compact_summary)
        self.assertEqual(
            [message.uuid for message in projected[1:]],
            [messages[2].uuid, messages[3].uuid],
        )
        self.assertNotIn(messages[0].uuid, [message.uuid for message in projected])

    def test_microcompact_savings_adjust_stale_api_usage(self):
        manager = ContextManager(
            ContextConfig(
                microcompact_keep_recent_tool_results=1,
                microcompact_min_chars=10,
            )
        )
        messages = [UserMessage("inspect")]
        messages.extend(tool_exchange("old_call", "old " * 1000))
        messages.extend(tool_exchange("new_call", "new " * 100))
        latest_assistant = messages[3]
        self.assertIsInstance(latest_assistant, AssistantMessage)
        latest_assistant.usage = TokenUsage(
            input_tokens=1800,
            output_tokens=200,
            total_tokens=2000,
        )

        before = manager.current_token_count(messages)
        boundary = manager.build_microcompact_boundary(messages)
        self.assertIsNotNone(boundary)
        after = manager.current_token_count(messages + [boundary])

        self.assertLess(after, before)

    def test_compact_projection_survives_transcript_resume(self):
        manager = ContextManager(ContextConfig(preserve_recent_groups=1))
        messages = [UserMessage("old"), AssistantMessage.text("recent")]
        plan = manager.build_full_compaction_plan(messages)
        boundary, summary = manager.commit_full_compaction(
            messages,
            plan,
            "Resume summary",
        )
        full_history = messages + [boundary, summary]

        with tempfile.TemporaryDirectory() as tmp:
            transcript = Transcript(Path(tmp) / "session.jsonl")
            for message in full_history:
                transcript.append_message(message)
            loaded = transcript.load_messages(strict=True)

        self.assertEqual(
            [message.uuid for message in manager.project(loaded)],
            [message.uuid for message in manager.project(full_history)],
        )
        self.assertTrue(any(isinstance(message, SystemMessage) for message in loaded))


class QueryLoopCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "hello.txt").write_text("hello\n", encoding="utf-8")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_auto_compaction_uses_separate_summary_model(self):
        def answer(messages, tools, system_prompt):
            self.assertTrue(
                any(
                    isinstance(message, UserMessage) and message.is_compact_summary
                    for message in messages
                )
            )
            return AssistantMessage.text("done")

        model = ScriptedModelClient([answer])
        summary_model = ScriptedModelClient(
            [AssistantMessage.text("User goal: handle a long prompt.")]
        )
        loop = QueryLoop(
            model,
            self.workspace,
            summary_model_client=summary_model,
            config=QueryLoopConfig(
                context=ContextConfig(
                    context_window_tokens=40,
                    reserved_output_tokens=0,
                    auto_compact_ratio=0.5,
                    preserve_recent_groups=0,
                    microcompact_min_chars=10000,
                )
            ),
        )

        events = await collect_run(loop, "long prompt " * 100)

        self.assertEqual(model.calls, 1)
        self.assertEqual(summary_model.calls, 1)
        self.assertTrue(
            any(
                isinstance(event, CompactionEvent) and event.status == "completed"
                for event in events
            )
        )
        self.assertEqual(events[-1].reason, "completed")

    async def test_manual_compaction_appends_boundary_and_summary(self):
        summary_model = ScriptedModelClient([AssistantMessage.text("Short summary")])
        loop = QueryLoop(
            ScriptedModelClient([]),
            self.workspace,
            summary_model_client=summary_model,
            config=QueryLoopConfig(
                context=ContextConfig(auto_compact_enabled=False)
            ),
            initial_messages=[UserMessage("old context " * 100)],
        )

        events = await collect_compact(loop)

        self.assertEqual(events[-1].status, "completed")
        self.assertTrue(
            any(
                isinstance(message, SystemMessage)
                and message.subtype == "compact_boundary"
                for message in loop.messages
            )
        )
        self.assertTrue(loop.active_messages()[0].is_compact_summary)

    async def test_prompt_too_long_compacts_and_retries_once(self):
        model = PromptTooLongThenAnswer(fail_count=1)
        summary_model = ScriptedModelClient([AssistantMessage.text("Recovery summary")])
        loop = QueryLoop(
            model,
            self.workspace,
            summary_model_client=summary_model,
            config=QueryLoopConfig(
                context=ContextConfig(auto_compact_enabled=False)
            ),
        )

        events = await collect_run(loop, "large request " * 100)

        self.assertEqual(model.calls, 2)
        self.assertEqual(summary_model.calls, 1)
        self.assertEqual(events[-1].reason, "completed")
        self.assertTrue(
            any(
                isinstance(event, CompactionEvent)
                and event.trigger == "prompt_too_long"
                and event.status == "completed"
                for event in events
            )
        )

    async def test_compaction_request_too_long_drops_old_atomic_group_and_retries(self):
        summary_model = PromptTooLongThenAnswer(fail_count=1)
        initial_messages = [
            UserMessage("old question one"),
            AssistantMessage.text("old answer one"),
            UserMessage("old question two"),
            AssistantMessage.text("old answer two"),
        ]
        loop = QueryLoop(
            ScriptedModelClient([]),
            self.workspace,
            summary_model_client=summary_model,
            config=QueryLoopConfig(
                context=ContextConfig(
                    auto_compact_enabled=False,
                    preserve_recent_groups=1,
                )
            ),
            initial_messages=initial_messages,
        )

        events = await collect_compact(loop)

        self.assertEqual(summary_model.calls, 2)
        self.assertEqual(events[-1].status, "completed")
        boundary = next(
            message
            for message in loop.messages
            if isinstance(message, SystemMessage)
            and message.subtype == "compact_boundary"
        )
        self.assertTrue(boundary.metadata["dropped_message_uuids"])

    async def test_second_prompt_too_long_stops_instead_of_looping(self):
        model = PromptTooLongThenAnswer(fail_count=2)
        summary_model = ScriptedModelClient([AssistantMessage.text("Recovery summary")])
        loop = QueryLoop(
            model,
            self.workspace,
            summary_model_client=summary_model,
            config=QueryLoopConfig(
                context=ContextConfig(auto_compact_enabled=False)
            ),
        )

        events = await collect_run(loop, "large request " * 100)

        self.assertEqual(model.calls, 2)
        self.assertIsInstance(events[-1], TerminalResult)
        self.assertEqual(events[-1].reason, "prompt_too_long")

    async def test_auto_compaction_circuit_breaker_stops_after_three_failures(self):
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"}),
                AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"}),
                AssistantMessage.tool_use("read_file", {"file_path": "hello.txt"}),
                AssistantMessage.text("done"),
            ]
        )
        summary_model = FailingSummaryModel()
        loop = QueryLoop(
            model,
            self.workspace,
            summary_model_client=summary_model,
            config=QueryLoopConfig(
                max_turns=4,
                context=ContextConfig(
                    context_window_tokens=20,
                    reserved_output_tokens=0,
                    auto_compact_ratio=0.5,
                    microcompact_min_chars=10000,
                    max_compaction_failures=3,
                ),
            ),
        )

        events = await collect_run(loop, "long prompt " * 100)

        failures = [
            event
            for event in events
            if isinstance(event, CompactionEvent) and event.status == "failed"
        ]
        self.assertEqual(len(failures), 3)
        self.assertEqual(summary_model.calls, 3)
        self.assertEqual(events[-1].reason, "completed")


if __name__ == "__main__":
    unittest.main()
