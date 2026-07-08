from __future__ import annotations

from typing import Callable, List, Sequence, Union

from .model_client import ModelClient
from .models import AssistantMessage, Message, UserMessage
from .tools import Tool


ScriptItem = Union[
    AssistantMessage,
    Callable[[Sequence[Message], Sequence[Tool], str], AssistantMessage],
]


class ScriptedModelClient(ModelClient):
    """A model client that returns pre-scripted assistant messages."""

    def __init__(self, responses: Sequence[ScriptItem]):
        self._responses = list(responses)
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        if self.calls >= len(self._responses):
            response = AssistantMessage.text("No scripted response left.")
        else:
            item = self._responses[self.calls]
            response = item(messages, tools, system_prompt) if callable(item) else item
        self.calls += 1
        yield response


class HeuristicMockModelClient(ModelClient):
    """Tiny CLI-friendly mock.

    It is not meant to be intelligent. It only demonstrates the query loop by
    calling `read_file`, `list_dir`, or `grep` for obvious prompts, then writing
    a final answer after it receives a tool_result.
    """

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools, system_prompt):
        self.calls += 1
        last = messages[-1] if messages else None
        if isinstance(last, UserMessage) and isinstance(last.content, list):
            yield AssistantMessage.text(self._answer_from_tool_result(last))
            return

        prompt = ""
        if isinstance(last, UserMessage) and isinstance(last.content, str):
            prompt = last.content.strip()
        lowered = prompt.lower()

        if lowered.startswith("read "):
            yield AssistantMessage.tool_use(
                "read_file",
                {"file_path": prompt[5:].strip() or "README.md"},
                text="I will read that file.",
            )
            return
        if lowered.startswith("list"):
            path = prompt[4:].strip() or "."
            yield AssistantMessage.tool_use(
                "list_dir",
                {"path": path},
                text="I will list that directory.",
            )
            return
        if lowered.startswith("grep "):
            pattern = prompt[5:].strip()
            yield AssistantMessage.tool_use(
                "grep",
                {"pattern": pattern, "path": "."},
                text="I will search the workspace.",
            )
            return

        yield AssistantMessage.text(
            "Mock model received: %s\n"
            "Try prompts like `read README.md`, `list .`, or `grep QueryLoop`."
            % (prompt or "<empty>")
        )

    def _answer_from_tool_result(self, message: UserMessage) -> str:
        blocks = message.content if isinstance(message.content, list) else []
        parts: List[str] = []
        for block in blocks:
            if block.get("type") == "tool_result":
                prefix = "Tool %s" % block.get("tool_use_id")
                if block.get("is_error"):
                    prefix += " failed"
                parts.append("%s:\n%s" % (prefix, block.get("content", "")))
        return "Here is the result I received:\n\n" + "\n\n".join(parts)
