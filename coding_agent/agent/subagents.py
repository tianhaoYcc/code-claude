from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .context_manager import ContextConfig
from .memory import MemoryConfig, sanitize_memory_text
from .model_client import ModelClient
from .models import AssistantMessage, TerminalResult, new_uuid
from .plan_mode import (
    AGENT_TOOL_NAME,
    ENTER_PLAN_MODE_TOOL_NAME,
    EXIT_PLAN_MODE_TOOL_NAME,
    PlanConfig,
    PlanManager,
)
from .tool_registry import ToolRegistry
from .tools import (
    InputValidationError,
    PermissionPolicy,
    Tool,
    ToolContext,
    ToolResult,
    resolve_workspace_path,
)
from .transcript import Transcript


SUBAGENT_TYPES = ("explore", "plan", "general-purpose")
READ_ONLY_TOOL_NAMES = ("read_file", "list_dir", "glob", "grep")


EXPLORE_SYSTEM_PROMPT = """You are a read-only codebase exploration agent.
Search broadly, inspect relevant files, trace concrete code paths, and return a
concise factual report with file paths and important symbols. You cannot modify
files, run shell commands, enter plan mode, or spawn another agent. Do not claim
that work was completed when you only inspected it.
"""


PLAN_SYSTEM_PROMPT = """You are a read-only software planning agent.
Explore the codebase and return a concrete implementation proposal, including
the approach, critical files, sequencing, risks, and tests. You cannot modify
files, run shell commands, enter plan mode, or spawn another agent. Return the
candidate plan to the parent agent; do not attempt to save the parent's plan.
"""


GENERAL_SYSTEM_PROMPT = """You are a foreground general-purpose coding agent.
Complete the delegated task within the available tools and permissions. Inspect
the code before changing it, keep edits scoped, run relevant verification, and
return a concise report of work and remaining risks. You cannot spawn another
agent or change the parent's plan mode.
"""


@dataclass
class SubagentConfig:
    enabled: bool = False
    max_turns: int = 8
    max_concurrency: int = 3
    timeout_seconds: float = 120.0
    transcript_root: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.transcript_root is not None:
            self.transcript_root = Path(self.transcript_root)
        if self.max_turns <= 0:
            raise ValueError("subagent max_turns must be greater than zero")
        if self.max_concurrency <= 0:
            raise ValueError("subagent max_concurrency must be greater than zero")
        if self.timeout_seconds <= 0:
            raise ValueError("subagent timeout_seconds must be greater than zero")


@dataclass(frozen=True)
class AgentDefinition:
    agent_type: str
    description: str
    system_prompt: str
    allowed_tools: Optional[Tuple[str, ...]]
    read_only: bool


BUILTIN_AGENTS: Tuple[AgentDefinition, ...] = (
    AgentDefinition(
        agent_type="explore",
        description="Read-only specialist for searching and understanding code",
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        allowed_tools=READ_ONLY_TOOL_NAMES,
        read_only=True,
    ),
    AgentDefinition(
        agent_type="plan",
        description="Read-only architect for producing implementation proposals",
        system_prompt=PLAN_SYSTEM_PROMPT,
        allowed_tools=READ_ONLY_TOOL_NAMES,
        read_only=True,
    ),
    AgentDefinition(
        agent_type="general-purpose",
        description="Foreground coding agent that may edit using parent permissions",
        system_prompt=GENERAL_SYSTEM_PROMPT,
        allowed_tools=None,
        read_only=False,
    ),
)


class SubagentManager:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        model_client: ModelClient,
        config: SubagentConfig,
        tool_provider: Callable[[], Sequence[Tool]],
        parent_permission_policy: PermissionPolicy,
        plan_manager: Optional[PlanManager] = None,
        memory_context_provider: Optional[Callable[[], str]] = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = _safe_session_id(session_id)
        self.model_client = model_client
        self.config = config
        self.tool_provider = tool_provider
        self.parent_permission_policy = parent_permission_policy
        self.plan_manager = plan_manager
        self.memory_context_provider = memory_context_provider or (lambda: "")
        configured_root = config.transcript_root or Path(
            ".agent_sessions/subagents"
        )
        self.transcript_root = resolve_workspace_path(
            self.workspace_root, str(configured_root)
        ) / self.session_id
        self.definitions = {
            definition.agent_type: definition for definition in BUILTIN_AGENTS
        }
        self.semaphore = asyncio.Semaphore(config.max_concurrency)

    def allowed_agent_types(self) -> Tuple[str, ...]:
        if self.plan_manager is not None and self.plan_manager.is_planning:
            return ("explore", "plan")
        return SUBAGENT_TYPES

    async def run(
        self,
        description: str,
        prompt: str,
        agent_type: str,
        parent_context: ToolContext,
    ) -> ToolResult:
        if agent_type not in self.allowed_agent_types():
            return ToolResult(
                content=(
                    "Subagent type %s is not allowed in mode %s"
                    % (agent_type, self._current_mode())
                ),
                raw={"agent_type": agent_type, "error": "agent type not allowed"},
                is_error=True,
            )
        definition = self.definitions.get(agent_type)
        if definition is None:
            return ToolResult(
                content="Unknown subagent type: %s" % agent_type,
                raw={"agent_type": agent_type, "error": "unknown agent type"},
                is_error=True,
            )
        agent_id = "agent_" + new_uuid().replace("-", "")[:16]
        async with self.semaphore:
            child_task = asyncio.create_task(
                self._run_one(
                    agent_id,
                    definition,
                    description,
                    prompt,
                    parent_context,
                )
            )
            cancel_task = asyncio.create_task(parent_context.cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {child_task, cancel_task},
                    timeout=self.config.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if child_task in done:
                    return await child_task
                child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)
                reason = (
                    "cancelled by parent"
                    if cancel_task in done
                    else "timed out after %.1f seconds" % self.config.timeout_seconds
                )
                return ToolResult(
                    content="Subagent %s %s" % (agent_id, reason),
                    raw={
                        "agent_id": agent_id,
                        "agent_type": agent_type,
                        "transcript_path": str(
                            self.transcript_root / (agent_id + ".jsonl")
                        ),
                        "error": reason,
                    },
                    is_error=True,
                )
            except asyncio.CancelledError:
                child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)
                raise
            finally:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

    async def _run_one(
        self,
        agent_id: str,
        definition: AgentDefinition,
        description: str,
        prompt: str,
        parent_context: ToolContext,
    ) -> ToolResult:
        from .query_loop import QueryLoop, QueryLoopConfig

        transcript_path = self.transcript_root / (agent_id + ".jsonl")
        transcript = Transcript(transcript_path)
        child_tools = self._tools_for(definition)
        context_parts = []
        memory_context = sanitize_memory_text(self.memory_context_provider()).strip()
        if memory_context:
            context_parts.append(memory_context)
        if self.plan_manager is not None:
            plan_context = self.plan_manager.subagent_context().strip()
            if plan_context:
                context_parts.append(plan_context)
        system_prompt = definition.system_prompt.strip()
        if context_parts:
            system_prompt += "\n\n" + "\n\n".join(context_parts)
        delegated_prompt = (
            "Delegated task: %s\n\n%s" % (description.strip(), prompt.strip())
        )

        if definition.read_only:
            write_permission = "deny"
            shell_permission = "deny"
        else:
            write_permission = self.parent_permission_policy.write
            shell_permission = self.parent_permission_policy.shell

        child = QueryLoop(
            model_client=self.model_client,
            workspace_root=self.workspace_root,
            registry=ToolRegistry(child_tools),
            transcript=transcript,
            config=QueryLoopConfig(
                max_turns=self.config.max_turns,
                max_inline_tool_result_chars=4000,
                max_bad_tool_input_attempts=3,
                max_tool_concurrency=self.config.max_concurrency,
                shell_timeout_seconds=parent_context.shell_timeout_seconds,
                shell_max_output_chars=parent_context.shell_max_output_chars,
                read_permission=self.parent_permission_policy.read,
                write_permission=write_permission,
                shell_permission=shell_permission,
                permission_callback=self.parent_permission_policy.callback,
                context=ContextConfig(),
                memory=MemoryConfig(enabled=False),
                plan=PlanConfig(enabled=False),
                subagents=SubagentConfig(enabled=False),
                system_prompt=system_prompt,
            ),
            session_id=agent_id,
            cancel_event=parent_context.cancel_event,
        )
        terminal: Optional[TerminalResult] = None
        final_text = ""
        try:
            async for event in child.run(delegated_prompt):
                if isinstance(event, AssistantMessage) and event.text_content():
                    final_text = event.text_content()
                elif isinstance(event, TerminalResult):
                    terminal = event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                content="Subagent %s failed: %s" % (agent_id, exc),
                raw={
                    "agent_id": agent_id,
                    "agent_type": definition.agent_type,
                    "transcript_path": str(transcript_path),
                    "error": str(exc),
                },
                is_error=True,
            )
        finally:
            await child.aclose()

        if terminal is None:
            return ToolResult(
                content="Subagent %s returned no terminal result" % agent_id,
                raw={
                    "agent_id": agent_id,
                    "agent_type": definition.agent_type,
                    "transcript_path": str(transcript_path),
                    "error": "missing terminal result",
                },
                is_error=True,
            )
        raw = {
            "agent_id": agent_id,
            "agent_type": definition.agent_type,
            "terminal_reason": terminal.reason,
            "turn_count": terminal.turn_count,
            "transcript_path": str(transcript_path),
        }
        if terminal.is_error or terminal.reason != "completed":
            return ToolResult(
                content=(
                    "Subagent %s stopped with reason=%s. Transcript: %s"
                    % (agent_id, terminal.reason, transcript_path)
                ),
                raw=raw,
                is_error=True,
            )
        if not final_text.strip():
            raw["error"] = "empty final response"
            return ToolResult(
                content="Subagent %s completed without a final response" % agent_id,
                raw=raw,
                is_error=True,
            )
        return ToolResult(
            content=(
                "Subagent %s (%s) completed.\nTranscript: %s\n\n%s"
                % (agent_id, definition.agent_type, transcript_path, final_text.strip())
            ),
            raw=raw,
        )

    def _tools_for(self, definition: AgentDefinition) -> List[Tool]:
        excluded = {
            AGENT_TOOL_NAME,
            ENTER_PLAN_MODE_TOOL_NAME,
            EXIT_PLAN_MODE_TOOL_NAME,
        }
        tools = [
            tool for tool in self.tool_provider() if tool.name not in excluded
        ]
        if definition.allowed_tools is None:
            return tools
        allowed = set(definition.allowed_tools)
        return [tool for tool in tools if tool.name in allowed]

    def _current_mode(self) -> str:
        if self.plan_manager is None:
            return "execute"
        return self.plan_manager.mode


class AgentTool(Tool):
    name = AGENT_TOOL_NAME
    description = "Delegate a focused task to an isolated foreground subagent"
    input_schema = {
        "type": "object",
        "required": ["description", "prompt", "subagent_type"],
        "properties": {
            "description": {"type": "string"},
            "prompt": {"type": "string"},
            "subagent_type": {"type": "string", "enum": list(SUBAGENT_TYPES)},
        },
    }

    def __init__(self, manager: SubagentManager):
        self.manager = manager

    def prepare_input(self, input_data: Dict[str, object]) -> Dict[str, object]:
        normalized = super().prepare_input(input_data)
        for name in ("description", "prompt"):
            normalized[name] = str(normalized[name]).strip()
            if not normalized[name]:
                raise InputValidationError("Field %s must not be empty" % name)
        return normalized

    def schema_for_model(self) -> Dict[str, object]:
        schema = copy.deepcopy(self.input_schema)
        schema["properties"]["subagent_type"]["enum"] = list(
            self.manager.allowed_agent_types()
        )
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    def is_concurrency_safe(self, input_data: Dict[str, object]) -> bool:
        return input_data.get("subagent_type") in {"explore", "plan"}

    def is_read_only(self, input_data: Dict[str, object]) -> bool:
        return input_data.get("subagent_type") in {"explore", "plan"}

    async def call(self, input_data, context: ToolContext) -> ToolResult:
        return await self.manager.run(
            description=str(input_data["description"]),
            prompt=str(input_data["prompt"]),
            agent_type=str(input_data["subagent_type"]),
            parent_context=context,
        )


def _safe_session_id(session_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id).strip())
    if not normalized or normalized in {".", ".."}:
        raise ValueError("session_id must contain a safe path component")
    return normalized
