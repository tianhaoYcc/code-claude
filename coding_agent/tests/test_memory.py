from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.context_manager import ContextConfig, ContextManager, estimate_text_tokens
from agent.memory import (
    SESSION_MEMORY_HEADINGS,
    MemoryConfig,
    MemoryRetriever,
    MemoryStore,
    sanitize_memory_text,
)
from agent.memory_manager import MemoryManager, WorkerOutcome
from agent.mock_model import ScriptedModelClient
from agent.model_client import ModelClient
from agent.models import (
    AssistantMessage,
    MemoryEvent,
    TerminalResult,
    UserMessage,
    ensure_tool_result_pairing,
    tool_result_block,
)
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.transcript import Transcript


def valid_summary(revision: int = 1) -> str:
    parts = ["# Session Memory"]
    for heading in SESSION_MEMORY_HEADINGS:
        parts.append("## %s\nrevision %d: stable checkpoint" % (heading, revision))
    return "\n\n".join(parts) + "\n"


class AdaptiveMemoryModel(ModelClient):
    def __init__(self):
        self.session_calls = 0
        self.durable_calls = 0
        self.revision = 0

    async def stream(self, messages, tools, system_prompt):
        last = messages[-1] if messages else None
        if "durable-memory" in system_prompt:
            self.durable_calls += 1
            yield AssistantMessage.text("NO_MEMORY")
            return
        if "session-memory" in system_prompt:
            self.session_calls += 1
            if isinstance(last, UserMessage) and isinstance(last.content, list):
                yield AssistantMessage.text("summary.md updated")
                return
            self.revision += 1
            yield AssistantMessage.tool_use(
                "write_file",
                {
                    "file_path": "summary.md",
                    "content": valid_summary(self.revision),
                    "overwrite": True,
                },
            )
            return
        yield AssistantMessage.text("unexpected memory prompt")


class QuickWorker:
    def __init__(self):
        self.targets = []

    async def run(self, snapshot, cursor_uuid):
        self.targets.append(snapshot.target_uuid)
        return WorkerOutcome(status="updated", message="updated")


class BlockingWorker:
    def __init__(self):
        self.targets = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, snapshot, cursor_uuid):
        self.targets.append(snapshot.target_uuid)
        if len(self.targets) == 1:
            self.started.set()
            await self.release.wait()
        return WorkerOutcome(status="updated", message="updated")


class FailingWorker:
    async def run(self, snapshot, cursor_uuid):
        raise RuntimeError("worker unavailable")


class FailIfCalledModel(ModelClient):
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        if False:
            yield AssistantMessage.text("")
        raise AssertionError("full compaction model should not be called")


class FailingMemoryModel(ModelClient):
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        if False:
            yield AssistantMessage.text("")
        raise RuntimeError("memory model unavailable")


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_retriever_loads_index_and_only_relevant_topics(self):
        config = MemoryConfig(enabled=True, recall_max_topics=1, recall_max_tokens=500)
        store = MemoryStore(self.workspace, "session-a", config)
        store.ensure_layout()
        store.write_text_atomic(
            store.index_path,
            "# Memory Index\n"
            "- [Query loop](topics/query-loop.md): query loop and tool protocol\n"
            "- [UI colors](topics/ui-colors.md): dashboard color preferences\n",
        )
        store.write_text_atomic(
            store.topics_dir / "query-loop.md",
            "---\n"
            "type: project\n"
            "keywords: query loop, tool_use\n"
            "updated_at: 2026-07-16T10:00:00Z\n"
            "source_session: session-a\n"
            "---\n"
            "# Query loop\nAlways preserve tool_use/tool_result pairs.\n",
        )
        store.write_text_atomic(
            store.topics_dir / "ui-colors.md",
            "---\n"
            "type: user\n"
            "keywords: dashboard, color\n"
            "updated_at: 2026-07-15T10:00:00Z\n"
            "source_session: session-a\n"
            "---\n"
            "# UI colors\nUse a restrained palette.\n",
        )

        context = MemoryRetriever(store, config).recall(
            "How should the query loop preserve tool_use results?"
        )

        self.assertIn("MEMORY.md", context)
        self.assertIn("Always preserve tool_use", context)
        self.assertNotIn("Use a restrained palette", context)
        self.assertLessEqual(estimate_text_tokens(context), config.recall_max_tokens)

    def test_sensitive_values_are_redacted(self):
        text = (
            "LLM_API_KEY=secret-value-123\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        )
        sanitized = sanitize_memory_text(text, secret_values=("secret-value-123",))

        self.assertNotIn("secret-value-123", sanitized)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_state_and_summary_round_trip(self):
        config = MemoryConfig(enabled=True)
        store = MemoryStore(self.workspace, "resume-session", config)
        store.ensure_layout()
        store.write_summary(valid_summary())
        state = store.load_state()
        state.last_session_summary_message_uuid = "message-1"
        state.last_durable_memory_message_uuid = "message-0"
        state.last_summary_token_count = 12345
        store.save_state(state)

        resumed = MemoryStore(self.workspace, "resume-session", config)
        loaded = resumed.load_state()

        self.assertEqual(loaded.last_session_summary_message_uuid, "message-1")
        self.assertEqual(loaded.last_summary_token_count, 12345)
        self.assertEqual(resumed.checkpoint().summary.strip(), valid_summary().strip())


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_session_thresholds_and_tool_safe_point(self):
        config = MemoryConfig(
            enabled=True,
            session_start_tokens=100,
            session_update_tokens=50,
            session_min_tool_calls=3,
        )
        manager = MemoryManager(
            self.workspace,
            "thresholds",
            ScriptedModelClient([]),
            config,
        )
        worker = QuickWorker()
        manager.session_worker = worker
        first = UserMessage("first")

        self.assertFalse(manager.maybe_schedule_session([first], 99, "final"))
        manager.note_tool_calls(2)
        self.assertFalse(manager.maybe_schedule_session([first], 100, "tool_batch"))
        manager.note_tool_calls(1)
        self.assertTrue(manager.maybe_schedule_session([first], 100, "tool_batch"))
        await manager.flush("session")

        second = UserMessage("second")
        messages = [first, second]
        self.assertFalse(manager.maybe_schedule_session(messages, 149, "final"))
        self.assertTrue(manager.maybe_schedule_session(messages, 150, "final"))
        await manager.close()

        self.assertEqual(worker.targets, [first.uuid, second.uuid])

    async def test_coalescing_keeps_only_latest_pending_snapshot(self):
        config = MemoryConfig(
            enabled=True,
            session_start_tokens=0,
            session_update_tokens=0,
            session_min_tool_calls=0,
        )
        manager = MemoryManager(
            self.workspace,
            "coalesce",
            ScriptedModelClient([]),
            config,
        )
        worker = BlockingWorker()
        manager.session_worker = worker
        one = UserMessage("one")
        two = UserMessage("two")
        three = UserMessage("three")

        manager.maybe_schedule_session([one], 10, "final")
        await worker.started.wait()
        manager.maybe_schedule_session([one, two], 20, "final")
        manager.maybe_schedule_session([one, two, three], 30, "final")
        worker.release.set()
        await manager.close()

        self.assertEqual(worker.targets, [one.uuid, three.uuid])
        self.assertEqual(
            manager.state.last_session_summary_message_uuid,
            three.uuid,
        )

    async def test_worker_failure_does_not_advance_cursor(self):
        config = MemoryConfig(
            enabled=True,
            session_start_tokens=0,
            session_min_tool_calls=0,
        )
        manager = MemoryManager(
            self.workspace,
            "failure",
            ScriptedModelClient([]),
            config,
        )
        manager.session_worker = FailingWorker()
        message = UserMessage("remember")

        manager.maybe_schedule_session([message], 10, "final")
        await manager.close()
        events = manager.drain_events()

        self.assertIsNone(manager.state.last_session_summary_message_uuid)
        self.assertTrue(
            any(event.status == "failed" and "unavailable" in event.message for event in events)
        )

    async def test_worker_timeout_is_cancelled_before_close_returns(self):
        config = MemoryConfig(
            enabled=True,
            session_start_tokens=0,
            session_min_tool_calls=0,
            flush_timeout_seconds=0.01,
        )
        manager = MemoryManager(
            self.workspace,
            "timeout",
            ScriptedModelClient([]),
            config,
        )
        worker = BlockingWorker()
        manager.session_worker = worker
        manager.maybe_schedule_session([UserMessage("wait")], 10, "final")
        await worker.started.wait()

        await manager.close()
        events = manager.drain_events()

        self.assertTrue(manager.session_runner.task.done())
        self.assertTrue(
            any(event.status == "failed" and "timed out" in event.message for event in events)
        )

    async def test_real_session_worker_writes_checkpoint_and_advances_cursor(self):
        config = MemoryConfig(
            enabled=True,
            session_start_tokens=0,
            session_min_tool_calls=0,
        )
        model = AdaptiveMemoryModel()
        manager = MemoryManager(self.workspace, "session-worker", model, config)
        message = UserMessage("Implement the memory layer")

        manager.maybe_schedule_session([message], 10, "final")
        await manager.close()

        checkpoint = manager.store.checkpoint()
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint.message_uuid, message.uuid)
        self.assertIn("## Current State", checkpoint.summary)
        self.assertGreaterEqual(model.session_calls, 2)

    async def test_durable_worker_updates_index_and_topic(self):
        topic = (
            "---\n"
            "type: project\n"
            "keywords: memory, query loop\n"
            "updated_at: 2026-07-16T10:00:00Z\n"
            "source_session: durable-worker\n"
            "---\n"
            "# Memory architecture\nUse separate session and durable memory.\n"
        )
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use(
                    "write_file",
                    {"file_path": "topics/memory.md", "content": topic},
                ),
                AssistantMessage.tool_use(
                    "write_file",
                    {
                        "file_path": "MEMORY.md",
                        "content": (
                            "# Memory Index\n"
                            "- [Memory](topics/memory.md): memory and query loop architecture\n"
                        ),
                        "overwrite": True,
                    },
                ),
                AssistantMessage.text("updated"),
            ]
        )
        config = MemoryConfig(enabled=True)
        manager = MemoryManager(
            self.workspace,
            "durable-worker",
            model,
            config,
        )
        message = UserMessage("Remember the two-layer memory architecture")

        manager.schedule_durable([message], 100)
        await manager.close()

        self.assertEqual(manager.state.last_durable_memory_message_uuid, message.uuid)
        self.assertIn(
            "topics/memory.md",
            manager.store.index_path.read_text(encoding="utf-8"),
        )
        manager.store.validate_persistent_memory()

    async def test_durable_worker_cannot_write_session_memory(self):
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use(
                    "write_file",
                    {
                        "file_path": "sessions/hack.md",
                        "content": "must not be written",
                        "create_dirs": True,
                    },
                ),
                AssistantMessage.text("NO_MEMORY"),
            ]
        )
        manager = MemoryManager(
            self.workspace,
            "scope",
            model,
            MemoryConfig(enabled=True),
        )
        message = UserMessage("temporary task detail")

        manager.schedule_durable([message], 10)
        await manager.close()

        self.assertFalse((manager.store.root / "sessions" / "hack.md").exists())
        self.assertEqual(manager.state.last_durable_memory_message_uuid, message.uuid)

    async def test_resume_session_worker_receives_only_messages_after_cursor(self):
        config = MemoryConfig(
            enabled=True,
            session_start_tokens=0,
            session_update_tokens=0,
            session_min_tool_calls=0,
        )
        old_message = UserMessage("OLD_ONLY_9A4E")
        first = MemoryManager(
            self.workspace,
            "resume-delta",
            AdaptiveMemoryModel(),
            config,
        )
        first.maybe_schedule_session([old_message], 10, "final")
        await first.close()

        captured = []

        def write_summary(messages, tools, system_prompt):
            captured.append(str(messages[-1].content))
            return AssistantMessage.tool_use(
                "write_file",
                {
                    "file_path": "summary.md",
                    "content": valid_summary(99),
                    "overwrite": True,
                },
            )

        second_model = ScriptedModelClient(
            [write_summary, AssistantMessage.text("updated")]
        )
        second = MemoryManager(
            self.workspace,
            "resume-delta",
            second_model,
            config,
        )
        new_message = UserMessage("NEW_ONLY_7C2B")
        second.maybe_schedule_session(
            [old_message, new_message],
            20,
            "final",
        )
        await second.close()

        self.assertEqual(second.state.last_session_summary_message_uuid, new_message.uuid)
        self.assertIn("NEW_ONLY_7C2B", captured[0])
        self.assertNotIn("OLD_ONLY_9A4E", captured[0])


class SessionMemoryCompactionTests(unittest.TestCase):
    def test_checkpoint_becomes_user_summary_and_preserves_tool_pair(self):
        manager = ContextManager(ContextConfig(auto_compact_enabled=False))
        old_user = UserMessage("old question")
        old_answer = AssistantMessage.text("old answer")
        checkpoint = UserMessage("checkpoint message")
        tool_use = AssistantMessage.tool_use(
            "read_file",
            {"file_path": "README.md"},
            tool_use_id="call-memory",
        )
        tool_result = UserMessage(
            [tool_result_block("call-memory", "result")],
            is_meta=True,
        )
        messages = [old_user, old_answer, checkpoint, tool_use, tool_result]

        result = manager.commit_session_memory_compaction(
            messages,
            valid_summary(),
            checkpoint.uuid,
            "session.jsonl",
            min_preserved_tokens=0,
            min_text_groups=0,
            max_preserved_tokens=10000,
        )

        self.assertIsNotNone(result)
        boundary, summary = result
        projected = manager.project(messages + [boundary, summary])
        self.assertTrue(projected[0].is_compact_summary)
        self.assertIn("source=\"session_memory\"", projected[0].content)
        self.assertNotIn(old_user.uuid, [message.uuid for message in projected])
        ensure_tool_result_pairing(projected)

    def test_missing_checkpoint_cursor_returns_none(self):
        manager = ContextManager()
        result = manager.commit_session_memory_compaction(
            [UserMessage("hello")],
            valid_summary(),
            "missing-uuid",
        )
        self.assertIsNone(result)


class QueryLoopMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_auto_compact_prefers_session_memory_over_full_summary(self):
        def main_answer(messages, tools, system_prompt):
            self.assertTrue(
                any(
                    isinstance(message, UserMessage) and message.is_compact_summary
                    for message in messages
                )
            )
            return AssistantMessage.text("continued")

        main_model = ScriptedModelClient([main_answer])
        memory_model = AdaptiveMemoryModel()
        full_summary_model = FailIfCalledModel()
        transcript = Transcript(self.workspace / ".agent_sessions" / "memory.jsonl")
        loop = QueryLoop(
            main_model,
            self.workspace,
            transcript=transcript,
            summary_model_client=full_summary_model,
            memory_model_client=memory_model,
            config=QueryLoopConfig(
                context=ContextConfig(
                    context_window_tokens=2000,
                    reserved_output_tokens=0,
                    auto_compact_ratio=0.5,
                    microcompact_min_chars=10000,
                ),
                memory=MemoryConfig(
                    enabled=True,
                    session_start_tokens=0,
                    session_update_tokens=0,
                    session_min_tool_calls=0,
                    session_compact_min_tokens=0,
                    session_compact_min_text_groups=0,
                ),
            ),
        )
        events = []
        async for event in loop.run("long prompt " * 500):
            events.append(event)
        events.extend(await loop.aclose())

        self.assertEqual(full_summary_model.calls, 0)
        terminal = next(event for event in events if isinstance(event, TerminalResult))
        self.assertEqual(terminal.reason, "completed")
        self.assertTrue(
            any(
                isinstance(event, MemoryEvent)
                and event.kind == "session"
                and event.status == "updated"
                for event in events
            )
        )
        boundary = next(
            message
            for message in loop.messages
            if getattr(message, "subtype", "") == "compact_boundary"
        )
        self.assertEqual(boundary.metadata.get("source"), "session_memory")

    async def test_memory_worker_failure_falls_back_to_full_compaction(self):
        main_model = ScriptedModelClient([AssistantMessage.text("continued")])
        summary_model = ScriptedModelClient(
            [AssistantMessage.text("Fallback full summary")]
        )
        memory_model = FailingMemoryModel()
        loop = QueryLoop(
            main_model,
            self.workspace,
            summary_model_client=summary_model,
            memory_model_client=memory_model,
            config=QueryLoopConfig(
                context=ContextConfig(
                    context_window_tokens=2000,
                    reserved_output_tokens=0,
                    auto_compact_ratio=0.5,
                    preserve_recent_groups=0,
                    microcompact_min_chars=10000,
                ),
                memory=MemoryConfig(
                    enabled=True,
                    session_start_tokens=0,
                    session_update_tokens=0,
                    session_min_tool_calls=0,
                    session_compact_min_tokens=0,
                    session_compact_min_text_groups=0,
                ),
            ),
        )
        events = []
        async for event in loop.run("long prompt " * 500):
            events.append(event)
        events.extend(await loop.aclose())

        self.assertEqual(summary_model.calls, 1)
        self.assertTrue(
            any(
                isinstance(event, MemoryEvent)
                and event.kind == "session"
                and event.status == "failed"
                for event in events
            )
        )
        boundary = next(
            message
            for message in loop.messages
            if getattr(message, "subtype", "") == "compact_boundary"
        )
        self.assertNotEqual(boundary.metadata.get("source"), "session_memory")

    async def test_recalled_topic_is_added_to_main_system_prompt(self):
        transcript = Transcript(self.workspace / ".agent_sessions" / "recall.jsonl")
        config = MemoryConfig(enabled=True, session_start_tokens=999999)
        store = MemoryStore(self.workspace, transcript.path.stem, config, transcript.path)
        store.ensure_layout()
        store.write_text_atomic(
            store.index_path,
            "# Memory Index\n"
            "- [Protocol](topics/protocol.md): query loop pairing rule\n",
        )
        store.write_text_atomic(
            store.topics_dir / "protocol.md",
            "---\n"
            "type: project\n"
            "keywords: query loop, pairing\n"
            "updated_at: 2026-07-16T10:00:00Z\n"
            "source_session: recall\n"
            "---\n"
            "# Protocol\nPAIRING_RULE_FROM_MEMORY\n",
        )

        def answer(messages, tools, system_prompt):
            self.assertIn("<project_memory>", system_prompt)
            self.assertIn("PAIRING_RULE_FROM_MEMORY", system_prompt)
            return AssistantMessage.text("remembered")

        loop = QueryLoop(
            ScriptedModelClient([answer]),
            self.workspace,
            transcript=transcript,
            memory_model_client=AdaptiveMemoryModel(),
            config=QueryLoopConfig(memory=config),
        )
        async for _ in loop.run("Explain the query loop pairing rule"):
            pass
        await loop.aclose()


if __name__ == "__main__":
    unittest.main()
