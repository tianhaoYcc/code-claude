from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.context_manager import ContextConfig
from agent.mock_model import ScriptedModelClient
from agent.model_client import ModelClient
from agent.models import (
    AssistantMessage,
    PlanEvent,
    TerminalResult,
    ToolUseBlock,
    UserMessage,
)
from agent.plan_mode import PlanApprovalDecision, PlanConfig, PlanManager
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.subagents import SubagentConfig


async def collect(loop: QueryLoop, prompt: str):
    events = []
    try:
        async for event in loop.run(prompt):
            events.append(event)
    finally:
        events.extend(await loop.aclose())
    return events


def tool_result_texts(messages):
    texts = []
    for message in messages:
        if not isinstance(message, UserMessage) or not isinstance(message.content, list):
            continue
        for block in message.content:
            if block.get("type") == "tool_result":
                texts.append(str(block.get("content") or ""))
    return texts


class ConcurrentFinalModel(ModelClient):
    def __init__(self, delay: float = 0.04):
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.seen_tools = []

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        self.seen_tools.append(tuple(tool.name for tool in tools))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            prompt = ""
            if messages and isinstance(messages[-1], UserMessage):
                prompt = str(messages[-1].content)
            yield AssistantMessage.text("child result: " + prompt.splitlines()[0])
        finally:
            self.active -= 1


class BlockingModel(ModelClient):
    def __init__(self):
        self.started = asyncio.Event()
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        yield AssistantMessage.text("unreachable")


class PlanModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_approval_flow_returns_to_execute_and_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def approve(_request):
                return PlanApprovalDecision(True)

            plan_rel = ".agent_plans/session-a/plan.md"
            model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use("enter_plan_mode", {}),
                    AssistantMessage.tool_use(
                        "write_file",
                        {
                            "file_path": plan_rel,
                            "content": "# Plan\n\n1. Inspect code.\n2. Implement.\n3. Test.\n",
                        },
                    ),
                    AssistantMessage.tool_use("exit_plan_mode", {}),
                    AssistantMessage.text("Implementation is complete."),
                ]
            )
            loop = QueryLoop(
                model_client=model,
                workspace_root=root,
                session_id="session-a",
                config=QueryLoopConfig(
                    max_turns=6,
                    write_permission="deny",
                    plan=PlanConfig(enabled=True, approval_callback=approve),
                ),
            )

            events = await collect(loop, "Make a plan and implement it")

            self.assertTrue((root / plan_rel).exists())
            self.assertEqual(loop.plan_manager.mode, "execute")
            self.assertFalse(loop.plan_manager.state.active_plan)
            statuses = [event.status for event in events if isinstance(event, PlanEvent)]
            self.assertIn("entered", statuses)
            self.assertIn("approved", statuses)
            self.assertIn("completed", statuses)
            self.assertEqual(events[-1].reason, "completed")

    async def test_plan_mode_blocks_project_write_and_forged_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = ScriptedModelClient(
                [
                    AssistantMessage.from_tool_uses(
                        [
                            ToolUseBlock("write-outside", "write_file", {
                                "file_path": "target.txt",
                                "content": "not allowed",
                            }),
                            ToolUseBlock("hidden-shell", "powershell", {
                                "command": "Get-ChildItem"
                            }),
                        ]
                    )
                ]
            )
            loop = QueryLoop(
                model_client=model,
                workspace_root=root,
                session_id="plan-guard",
                config=QueryLoopConfig(
                    max_turns=1,
                    write_permission="allow",
                    shell_permission="allow",
                    plan=PlanConfig(enabled=True, initial_mode="plan"),
                ),
            )

            await collect(loop, "Plan only")

            self.assertFalse((root / "target.txt").exists())
            results = "\n".join(tool_result_texts(loop.messages))
            self.assertIn("active agent mode", results)
            self.assertIn("Permission denied", results)

    async def test_plan_write_is_sanitized_and_size_limited_before_disk_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_rel = ".agent_plans/sanitized/plan.md"
            loop = QueryLoop(
                model_client=ScriptedModelClient(
                    [
                        AssistantMessage.tool_use(
                            "write_file",
                            {
                                "file_path": plan_rel,
                                "content": "# Plan\n\nLLM_API_KEY=secret-value\n1. Test.\n",
                            },
                        )
                    ]
                ),
                workspace_root=root,
                session_id="sanitized",
                config=QueryLoopConfig(
                    max_turns=1,
                    plan=PlanConfig(enabled=True, initial_mode="plan"),
                ),
            )

            await collect(loop, "Write a safe plan")

            stored = (root / plan_rel).read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", stored)
            self.assertNotIn("secret-value", stored)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan_rel = ".agent_plans/oversized/plan.md"
            loop = QueryLoop(
                model_client=ScriptedModelClient(
                    [
                        AssistantMessage.tool_use(
                            "write_file",
                            {
                                "file_path": plan_rel,
                                "content": "word " * 200,
                            },
                        )
                    ]
                ),
                workspace_root=root,
                session_id="oversized",
                config=QueryLoopConfig(
                    max_turns=1,
                    plan=PlanConfig(
                        enabled=True,
                        initial_mode="plan",
                        max_plan_tokens=10,
                    ),
                ),
            )

            await collect(loop, "Write a bounded plan")

            self.assertFalse((root / plan_rel).exists())
            self.assertIn("max_plan_tokens", "\n".join(tool_result_texts(loop.messages)))

    async def test_mode_transition_tool_must_be_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            approval_calls = []

            def approve(request):
                approval_calls.append(request)
                return PlanApprovalDecision(True)

            manager = PlanManager(
                root,
                "exclusive",
                PlanConfig(enabled=True, initial_mode="plan", approval_callback=approve),
            )
            manager.store.write_text_atomic(
                manager.store.plan_path,
                "# Plan\n\n1. Approved work only.\n",
            )
            model = ScriptedModelClient(
                [
                    AssistantMessage.from_tool_uses(
                        [
                            ToolUseBlock("exit", "exit_plan_mode", {}),
                            ToolUseBlock(
                                "write",
                                "write_file",
                                {
                                    "file_path": "project.txt",
                                    "content": "must not run",
                                },
                            ),
                        ]
                    )
                ]
            )
            loop = QueryLoop(
                model_client=model,
                workspace_root=root,
                plan_manager=manager,
                config=QueryLoopConfig(max_turns=1, write_permission="allow"),
            )

            await collect(loop, "Approve, then write")

            self.assertEqual(approval_calls, [])
            self.assertEqual(manager.mode, "plan")
            self.assertFalse((root / "project.txt").exists())
            self.assertIn(
                "must be called alone",
                "\n".join(tool_result_texts(loop.messages)),
            )

    async def test_rejected_plan_stays_in_plan_mode_with_feedback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def reject(request):
                if request.kind == "exit":
                    return PlanApprovalDecision(False, "Add rollback tests")
                return PlanApprovalDecision(True)

            model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "write_file",
                        {
                            "file_path": ".agent_plans/rejected/plan.md",
                            "content": "# Plan\n\nImplement and test.\n",
                        },
                    ),
                    AssistantMessage.tool_use("exit_plan_mode", {}),
                ]
            )
            loop = QueryLoop(
                model_client=model,
                workspace_root=root,
                session_id="rejected",
                config=QueryLoopConfig(
                    max_turns=2,
                    plan=PlanConfig(
                        enabled=True,
                        initial_mode="plan",
                        approval_callback=reject,
                    ),
                ),
            )

            await collect(loop, "Draft a plan")

            self.assertEqual(loop.plan_manager.mode, "plan")
            self.assertFalse(loop.plan_manager.state.active_plan)
            self.assertIn("Add rollback tests", "\n".join(tool_result_texts(loop.messages)))

    async def test_corrupt_state_fails_closed_and_empty_plan_cannot_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / ".agent_plans" / "corrupt" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{broken", encoding="utf-8")

            manager = PlanManager(root, "corrupt", PlanConfig(enabled=True))

            self.assertEqual(manager.mode, "plan")
            self.assertTrue(any(event.status == "failed" for event in manager.events))
            decision = await manager.request_exit()
            self.assertFalse(decision.approved)
            self.assertIn("empty", decision.feedback.lower())

    async def test_normal_answer_cannot_exit_plan_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            loop = QueryLoop(
                model_client=ScriptedModelClient([AssistantMessage.text("A plan")]),
                workspace_root=Path(temp),
                session_id="no-exit",
                config=QueryLoopConfig(
                    max_turns=1,
                    plan=PlanConfig(enabled=True, initial_mode="plan"),
                ),
            )

            events = await collect(loop, "Plan this change")

            reminders = [
                message
                for message in loop.messages
                if isinstance(message, UserMessage) and message.is_meta
            ]
            self.assertTrue(any("still in plan mode" in str(m.content) for m in reminders))
            terminals = [event for event in events if isinstance(event, TerminalResult)]
            self.assertEqual(terminals[-1].reason, "max_turns")
            self.assertEqual(loop.plan_manager.mode, "plan")

    async def test_plan_state_resumes_and_boundary_records_plan_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = PlanConfig(enabled=True)
            manager = PlanManager(root, "resume-plan", config)
            manager.force_enter("user-1")
            manager.store.write_text_atomic(manager.store.plan_path, "# Plan\n\nDo work.\n")
            manager.state.mode = "execute"
            manager.state.approved_version = manager.state.plan_version
            manager.state.active_plan = True
            manager.store.save_state(manager.state)

            resumed = PlanManager(root, "resume-plan", config)
            self.assertEqual(resumed.mode, "execute")
            self.assertTrue(resumed.state.active_plan)

            messages = [
                UserMessage("Earlier request"),
                AssistantMessage.text("Earlier answer"),
                UserMessage("Recent request"),
            ]
            loop = QueryLoop(
                model_client=ScriptedModelClient([AssistantMessage.text("summary")]),
                summary_model_client=ScriptedModelClient([AssistantMessage.text("summary")]),
                workspace_root=root,
                session_id="resume-plan",
                initial_messages=messages,
                plan_manager=resumed,
                config=QueryLoopConfig(
                    context=ContextConfig(preserve_recent_groups=1),
                ),
            )
            try:
                async for _ in loop.compact(trigger="manual"):
                    pass
            finally:
                await loop.aclose()

            boundaries = [
                message
                for message in loop.messages
                if getattr(message, "subtype", "") == "compact_boundary"
            ]
            self.assertTrue(boundaries)
            self.assertEqual(
                boundaries[-1].metadata["plan"]["approved_version"],
                resumed.state.approved_version,
            )


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    async def test_explore_agent_is_isolated_and_writes_own_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child_model = ConcurrentFinalModel(delay=0)
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "agent",
                        {
                            "description": "Find query loop",
                            "prompt": "Locate QueryLoop and report its file.",
                            "subagent_type": "explore",
                        },
                    ),
                    AssistantMessage.text("Parent received the report."),
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=root,
                session_id="subagent-one",
                config=QueryLoopConfig(
                    subagents=SubagentConfig(enabled=True),
                ),
            )

            await collect(loop, "Delegate exploration")

            raw_results = [
                message.tool_use_result
                for message in loop.messages
                if isinstance(message, UserMessage) and message.tool_use_result
            ]
            agent_raw = next(iter(raw_results[0].values()))
            self.assertTrue(Path(agent_raw["transcript_path"]).exists())
            self.assertEqual(agent_raw["agent_type"], "explore")
            self.assertEqual(
                set(child_model.seen_tools[0]),
                {"read_file", "list_dir", "glob", "grep"},
            )
            self.assertNotIn("agent", child_model.seen_tools[0])

    async def test_general_agent_inherits_write_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child_model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "write_file",
                        {"file_path": "child.txt", "content": "written by child"},
                    ),
                    AssistantMessage.text("Child write completed."),
                ]
            )
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "agent",
                        {
                            "description": "Write child file",
                            "prompt": "Create child.txt.",
                            "subagent_type": "general-purpose",
                        },
                    ),
                    AssistantMessage.text("Done."),
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=root,
                session_id="general-write",
                config=QueryLoopConfig(
                    write_permission="allow",
                    subagents=SubagentConfig(enabled=True),
                ),
            )

            await collect(loop, "Delegate a write")

            self.assertEqual((root / "child.txt").read_text(), "written by child")

    async def test_plan_mode_rejects_general_agent_before_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            child_model = ConcurrentFinalModel(delay=0)
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "agent",
                        {
                            "description": "Modify code",
                            "prompt": "Write a file.",
                            "subagent_type": "general-purpose",
                        },
                    )
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=Path(temp),
                session_id="plan-agent-guard",
                config=QueryLoopConfig(
                    max_turns=1,
                    plan=PlanConfig(enabled=True, initial_mode="plan"),
                    subagents=SubagentConfig(enabled=True),
                ),
            )

            await collect(loop, "Plan only")

            self.assertEqual(child_model.calls, 0)
            self.assertIn("not allowed", "\n".join(tool_result_texts(loop.messages)))

    async def test_read_only_subagents_run_concurrently_and_keep_result_order(self):
        with tempfile.TemporaryDirectory() as temp:
            child_model = ConcurrentFinalModel()
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.from_tool_uses(
                        [
                            ToolUseBlock(
                                "agent-a",
                                "agent",
                                {
                                    "description": "Explore A",
                                    "prompt": "Find A",
                                    "subagent_type": "explore",
                                },
                            ),
                            ToolUseBlock(
                                "agent-b",
                                "agent",
                                {
                                    "description": "Plan B",
                                    "prompt": "Plan B",
                                    "subagent_type": "plan",
                                },
                            ),
                        ]
                    ),
                    AssistantMessage.text("Combined."),
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=Path(temp),
                session_id="parallel-agents",
                config=QueryLoopConfig(
                    subagents=SubagentConfig(enabled=True, max_concurrency=2),
                ),
            )

            await collect(loop, "Run two subagents")

            self.assertEqual(child_model.max_active, 2)
            result_message = next(
                message
                for message in loop.messages
                if isinstance(message, UserMessage)
                and isinstance(message.content, list)
                and len(message.content) == 2
            )
            self.assertEqual(
                [block["tool_use_id"] for block in result_message.content],
                ["agent-a", "agent-b"],
            )

    async def test_general_subagents_are_serial(self):
        with tempfile.TemporaryDirectory() as temp:
            child_model = ConcurrentFinalModel()
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.from_tool_uses(
                        [
                            ToolUseBlock(
                                "general-a",
                                "agent",
                                {
                                    "description": "General A",
                                    "prompt": "Inspect A",
                                    "subagent_type": "general-purpose",
                                },
                            ),
                            ToolUseBlock(
                                "general-b",
                                "agent",
                                {
                                    "description": "General B",
                                    "prompt": "Inspect B",
                                    "subagent_type": "general-purpose",
                                },
                            ),
                        ]
                    ),
                    AssistantMessage.text("Combined."),
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=Path(temp),
                session_id="serial-general",
                config=QueryLoopConfig(
                    subagents=SubagentConfig(enabled=True, max_concurrency=2),
                ),
            )

            await collect(loop, "Run two general agents")

            self.assertEqual(child_model.max_active, 1)
            self.assertEqual(child_model.calls, 2)

    async def test_subagent_timeout_becomes_error_tool_result(self):
        with tempfile.TemporaryDirectory() as temp:
            child_model = BlockingModel()
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "agent",
                        {
                            "description": "Slow exploration",
                            "prompt": "Wait forever",
                            "subagent_type": "explore",
                        },
                    ),
                    AssistantMessage.text("Recovered from child timeout."),
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=Path(temp),
                session_id="timeout-child",
                config=QueryLoopConfig(
                    subagents=SubagentConfig(
                        enabled=True,
                        timeout_seconds=0.02,
                    ),
                ),
            )

            events = await collect(loop, "Run a slow child")

            self.assertIn("timed out", "\n".join(tool_result_texts(loop.messages)))
            terminals = [event for event in events if isinstance(event, TerminalResult)]
            self.assertEqual(terminals[-1].reason, "completed")

    async def test_parent_cancel_interrupts_running_subagent(self):
        with tempfile.TemporaryDirectory() as temp:
            child_model = BlockingModel()
            parent_model = ScriptedModelClient(
                [
                    AssistantMessage.tool_use(
                        "agent",
                        {
                            "description": "Cancelable exploration",
                            "prompt": "Wait until cancelled",
                            "subagent_type": "explore",
                        },
                    )
                ]
            )
            loop = QueryLoop(
                model_client=parent_model,
                subagent_model_client=child_model,
                workspace_root=Path(temp),
                session_id="cancel-child",
                config=QueryLoopConfig(
                    subagents=SubagentConfig(enabled=True, timeout_seconds=5),
                ),
            )

            task = asyncio.create_task(collect(loop, "Run a cancelable child"))
            await asyncio.wait_for(child_model.started.wait(), timeout=1)
            loop.cancel()
            events = await asyncio.wait_for(task, timeout=1)

            terminals = [event for event in events if isinstance(event, TerminalResult)]
            self.assertEqual(terminals[-1].reason, "aborted")


if __name__ == "__main__":
    unittest.main()
