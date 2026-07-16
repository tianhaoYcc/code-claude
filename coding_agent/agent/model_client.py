from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Sequence

from .models import AssistantMessage, Message
from .tools import Tool


class ModelRequestError(RuntimeError):
    """Provider request failure with a machine-readable recovery hint."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: str = "",
        prompt_too_long: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.prompt_too_long = prompt_too_long


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
