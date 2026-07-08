from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from .models import AssistantMessage, Message
from .tools import Tool


class ModelClient(ABC):
    """Abstract model interface used by QueryLoop.

    Implementations can wrap a real model API or provide scripted mock output.
    The query loop only depends on assistant messages containing text and/or
    `tool_use` blocks.
    """

    @abstractmethod
    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        system_prompt: str,
    ) -> AsyncIterator[AssistantMessage]:
        raise NotImplementedError
