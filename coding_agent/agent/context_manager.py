from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .models import (
    AssistantMessage,
    Message,
    SystemMessage,
    TokenUsage,
    UserMessage,
)
from .tools import Tool


COMPACTION_SYSTEM_PROMPT = """You summarize coding-agent conversations for continuation.
Return a concise, factual handoff that preserves information needed to keep working.
Do not call tools. Do not invent completed work, files, commands, or test results.
Use these headings when relevant:
- User goal
- Completed work
- Files changed or inspected
- Important decisions and constraints
- Failures and unresolved issues
- Pending tasks
- Recommended next step
"""

COMPACTION_REQUEST = """Summarize the conversation above for a coding agent that will
continue the same task. Preserve concrete file paths, code decisions, tool outcomes,
errors, tests, user constraints, and unfinished work. Omit conversational filler.
"""

MICROCOMPACT_PLACEHOLDER = (
    "[Earlier tool result for {tool_name} was removed by microcompact. "
    "Re-run the tool if the full output is needed.]"
)

COMPACTION_TRUNCATION_MARKER = (
    "[Earlier conversation was omitted because the compaction request itself "
    "exceeded the model context window.]"
)


@dataclass
class ContextConfig:
    auto_compact_enabled: bool = True
    context_window_tokens: int = 128000
    reserved_output_tokens: int = 8192
    auto_compact_ratio: float = 0.8
    preserve_recent_groups: int = 4
    microcompact_keep_recent_tool_results: int = 4
    microcompact_min_chars: int = 800
    max_compaction_failures: int = 3

    def __post_init__(self) -> None:
        if self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be greater than zero")
        if self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens cannot be negative")
        if not 0 < self.auto_compact_ratio <= 1:
            raise ValueError("auto_compact_ratio must be in the range (0, 1]")
        if self.preserve_recent_groups < 0:
            raise ValueError("preserve_recent_groups cannot be negative")
        if self.microcompact_keep_recent_tool_results < 0:
            raise ValueError(
                "microcompact_keep_recent_tool_results cannot be negative"
            )
        if self.microcompact_min_chars < 0:
            raise ValueError("microcompact_min_chars cannot be negative")
        if self.max_compaction_failures <= 0:
            raise ValueError("max_compaction_failures must be greater than zero")

    @property
    def effective_context_tokens(self) -> int:
        return max(1, self.context_window_tokens - self.reserved_output_tokens)

    @property
    def auto_compact_threshold(self) -> int:
        return max(1, int(self.effective_context_tokens * self.auto_compact_ratio))


@dataclass
class CompactionPlan:
    messages_to_summarize: List[Message]
    preserved_messages: List[Message]
    before_tokens: int
    dropped_message_uuids: List[str] = field(default_factory=list)


class ContextManager:
    """Builds the model-visible projection without mutating full history."""

    def __init__(self, config: Optional[ContextConfig] = None):
        self.config = config or ContextConfig()
        self.consecutive_failures = 0

    def project(self, messages: Sequence[Message]) -> List[Message]:
        full_messages = list(messages)
        boundary_index = self._last_compact_boundary_index(full_messages)
        selected: List[Message] = []

        if boundary_index < 0:
            selected = [
                message
                for message in full_messages
                if not isinstance(message, SystemMessage)
            ]
        else:
            boundary = full_messages[boundary_index]
            if not isinstance(boundary, SystemMessage):
                raise ValueError("Invalid compact boundary")
            by_uuid = {message.uuid: message for message in full_messages}
            summary_uuid = str(boundary.metadata.get("summary_uuid") or "")
            if not summary_uuid or summary_uuid not in by_uuid:
                raise ValueError("Compact boundary references a missing summary message")

            selected_ids: Set[str] = set()
            self._append_unique(selected, selected_ids, by_uuid[summary_uuid])
            for raw_uuid in boundary.metadata.get("preserved_message_uuids") or []:
                message_uuid = str(raw_uuid)
                if message_uuid not in by_uuid:
                    raise ValueError(
                        "Compact boundary references missing message %s" % message_uuid
                    )
                self._append_unique(selected, selected_ids, by_uuid[message_uuid])

            for message in full_messages[boundary_index + 1 :]:
                if isinstance(message, SystemMessage):
                    continue
                self._append_unique(selected, selected_ids, message)

        tool_names = self._tool_names(selected)
        compacted_ids = self._microcompacted_tool_use_ids(full_messages)
        return self._replace_tool_results(selected, compacted_ids, tool_names)

    def estimate_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool] = (),
        system_prompt: str = "",
    ) -> int:
        payload: List[Any] = []
        if system_prompt:
            payload.append({"role": "system", "content": system_prompt})
        for message in messages:
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, UserMessage):
                payload.append({"role": "user", "content": message.content})
            elif isinstance(message, AssistantMessage):
                payload.append({"role": "assistant", "content": message.content})
            else:
                payload.append(message.to_record())
        if tools:
            payload.append(
                {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": tool.input_schema,
                        }
                        for tool in tools
                    ]
                }
            )
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return estimate_text_tokens(serialized)

    def current_token_count(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool] = (),
        system_prompt: str = "",
    ) -> int:
        active = self.project(messages)
        estimated = self.estimate_tokens(active, tools, system_prompt)

        if any(
            isinstance(message, UserMessage) and message.is_compact_summary
            for message in active
        ):
            return estimated

        for index in range(len(active) - 1, -1, -1):
            message = active[index]
            if not isinstance(message, AssistantMessage) or message.usage is None:
                continue
            trailing = self.estimate_tokens(active[index + 1 :])
            microcompact_savings = self._microcompact_tokens_saved_after(
                messages,
                message.uuid,
            )
            usage_based = max(
                0,
                message.usage.total_tokens + trailing - microcompact_savings,
            )
            return max(estimated, usage_based)
        return estimated

    def should_auto_compact(self, token_count: int) -> bool:
        return (
            self.config.auto_compact_enabled
            and self.consecutive_failures < self.config.max_compaction_failures
            and token_count >= self.config.auto_compact_threshold
        )

    def build_microcompact_boundary(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool] = (),
        system_prompt: str = "",
    ) -> Optional[SystemMessage]:
        active = self.project(messages)
        tool_names = self._tool_names(active)
        candidate_ids = self._microcompact_candidate_ids(active, tool_names)
        if not candidate_ids:
            return None

        before_tokens = self.estimate_tokens(active, tools, system_prompt)
        compacted = self._replace_tool_results(active, set(candidate_ids), tool_names)
        after_tokens = self.estimate_tokens(compacted, tools, system_prompt)
        return SystemMessage(
            subtype="microcompact_boundary",
            content="Older read-only tool results were removed from active context.",
            metadata={
                "tool_use_ids": candidate_ids,
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "tokens_saved": max(0, before_tokens - after_tokens),
            },
        )

    def build_full_compaction_plan(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool] = (),
        system_prompt: str = "",
    ) -> CompactionPlan:
        active = self.project(messages)
        groups = group_atomic_messages(active)
        if not groups:
            raise ValueError("Cannot compact an empty conversation")

        preserve_count = min(
            self.config.preserve_recent_groups,
            max(0, len(groups) - 1),
        )
        split_at = len(groups) - preserve_count
        summary_groups = groups[:split_at]
        preserved_groups = groups[split_at:]
        to_summarize = [message for group in summary_groups for message in group]
        preserved = [message for group in preserved_groups for message in group]
        if not to_summarize:
            raise ValueError("No messages are available for compaction")

        return CompactionPlan(
            messages_to_summarize=to_summarize,
            preserved_messages=preserved,
            before_tokens=self.estimate_tokens(active, tools, system_prompt),
        )

    def summary_request_messages(self, plan: CompactionPlan) -> List[Message]:
        request_messages: List[Message] = []
        if plan.dropped_message_uuids:
            request_messages.append(
                UserMessage(content=COMPACTION_TRUNCATION_MARKER, is_meta=True)
            )
        request_messages.extend(plan.messages_to_summarize)
        request_messages.append(UserMessage(content=COMPACTION_REQUEST, is_meta=True))
        return request_messages

    def truncate_plan_for_prompt_too_long(
        self,
        plan: CompactionPlan,
    ) -> Optional[CompactionPlan]:
        groups = group_atomic_messages(plan.messages_to_summarize)
        if len(groups) < 2:
            return None
        drop_count = max(1, int(len(groups) * 0.2))
        drop_count = min(drop_count, len(groups) - 1)
        dropped = [message for group in groups[:drop_count] for message in group]
        remaining = [message for group in groups[drop_count:] for message in group]
        return CompactionPlan(
            messages_to_summarize=remaining,
            preserved_messages=list(plan.preserved_messages),
            before_tokens=plan.before_tokens,
            dropped_message_uuids=(
                list(plan.dropped_message_uuids)
                + [message.uuid for message in dropped]
            ),
        )

    def commit_full_compaction(
        self,
        messages: Sequence[Message],
        plan: CompactionPlan,
        summary_text: str,
        tools: Sequence[Tool] = (),
        system_prompt: str = "",
        summary_usage: Optional[TokenUsage] = None,
    ) -> Tuple[SystemMessage, UserMessage]:
        normalized_summary = summary_text.strip()
        if not normalized_summary:
            raise ValueError("Compaction model returned an empty summary")

        summary = UserMessage(
            content=(
                "<context_summary>\n%s\n</context_summary>" % normalized_summary
            ),
            is_meta=True,
            is_compact_summary=True,
        )
        boundary = SystemMessage(
            subtype="compact_boundary",
            content="Conversation before this boundary was summarized.",
            metadata={
                "summary_uuid": summary.uuid,
                "summarized_message_uuids": [
                    message.uuid for message in plan.messages_to_summarize
                ],
                "preserved_message_uuids": [
                    message.uuid for message in plan.preserved_messages
                ],
                "dropped_message_uuids": list(plan.dropped_message_uuids),
                "before_tokens": plan.before_tokens,
                "summary_usage": (
                    summary_usage.to_record() if summary_usage is not None else None
                ),
            },
        )
        candidate = list(messages) + [boundary, summary]
        boundary.metadata["after_tokens"] = self.current_token_count(
            candidate,
            tools,
            system_prompt,
        )
        return boundary, summary

    def note_compaction_success(self) -> None:
        self.consecutive_failures = 0

    def note_compaction_failure(self) -> None:
        self.consecutive_failures += 1

    @staticmethod
    def _append_unique(
        target: List[Message],
        selected_ids: Set[str],
        message: Message,
    ) -> None:
        if message.uuid in selected_ids:
            return
        selected_ids.add(message.uuid)
        target.append(message)

    @staticmethod
    def _last_compact_boundary_index(messages: Sequence[Message]) -> int:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                isinstance(message, SystemMessage)
                and message.subtype == "compact_boundary"
            ):
                return index
        return -1

    @staticmethod
    def _microcompacted_tool_use_ids(messages: Sequence[Message]) -> Set[str]:
        compacted: Set[str] = set()
        for message in messages:
            if (
                isinstance(message, SystemMessage)
                and message.subtype == "microcompact_boundary"
            ):
                compacted.update(
                    str(tool_use_id)
                    for tool_use_id in message.metadata.get("tool_use_ids") or []
                )
        return compacted

    @staticmethod
    def _microcompact_tokens_saved_after(
        messages: Sequence[Message],
        assistant_uuid: str,
    ) -> int:
        assistant_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message.uuid == assistant_uuid
            ),
            -1,
        )
        if assistant_index < 0:
            return 0
        saved = 0
        for message in messages[assistant_index + 1 :]:
            if (
                isinstance(message, SystemMessage)
                and message.subtype == "microcompact_boundary"
            ):
                saved += max(0, int(message.metadata.get("tokens_saved") or 0))
        return saved

    @staticmethod
    def _tool_names(messages: Sequence[Message]) -> Dict[str, str]:
        names: Dict[str, str] = {}
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue
            for tool_use in message.tool_uses():
                names[tool_use.id] = tool_use.name
        return names

    def _microcompact_candidate_ids(
        self,
        messages: Sequence[Message],
        tool_names: Dict[str, str],
    ) -> List[str]:
        results: List[Tuple[str, Dict[str, Any]]] = []
        for message in messages:
            if not isinstance(message, UserMessage) or not isinstance(message.content, list):
                continue
            for block in message.content:
                if block.get("type") == "tool_result":
                    results.append((str(block.get("tool_use_id")), block))

        keep = self.config.microcompact_keep_recent_tool_results
        candidates = results[:-keep] if keep else results
        selected: List[str] = []
        for tool_use_id, block in candidates:
            tool_name = tool_names.get(tool_use_id, "unknown_tool")
            content = str(block.get("content", ""))
            if tool_name in {"write_file", "edit_file"}:
                continue
            if block.get("is_error"):
                continue
            if len(content) < self.config.microcompact_min_chars:
                continue
            if content.startswith("[Earlier tool result for "):
                continue
            selected.append(tool_use_id)
        return selected

    @staticmethod
    def _replace_tool_results(
        messages: Sequence[Message],
        compacted_ids: Set[str],
        tool_names: Dict[str, str],
    ) -> List[Message]:
        projected = copy.deepcopy(list(messages))
        if not compacted_ids:
            return projected
        for message in projected:
            if not isinstance(message, UserMessage) or not isinstance(message.content, list):
                continue
            changed = False
            for block in message.content:
                tool_use_id = str(block.get("tool_use_id") or "")
                if block.get("type") != "tool_result" or tool_use_id not in compacted_ids:
                    continue
                block["content"] = MICROCOMPACT_PLACEHOLDER.format(
                    tool_name=tool_names.get(tool_use_id, "unknown_tool")
                )
                block["compacted"] = True
                changed = True
            if changed:
                message.tool_use_result = None
        return projected


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, int(math.ceil(ascii_chars / 4.0)) + non_ascii_chars)


def group_atomic_messages(messages: Sequence[Message]) -> List[List[Message]]:
    """Keep each assistant tool_use and its user tool_result in one group."""

    groups: List[List[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AssistantMessage):
            tool_use_ids = {tool_use.id for tool_use in message.tool_uses()}
            if tool_use_ids and index + 1 < len(messages):
                next_message = messages[index + 1]
                if isinstance(next_message, UserMessage):
                    result_ids = set(next_message.tool_result_ids())
                    if tool_use_ids.issubset(result_ids):
                        groups.append([message, next_message])
                        index += 2
                        continue
        groups.append([message])
        index += 1
    return groups
