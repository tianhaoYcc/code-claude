"""Minimal Claude Code-style coding agent core."""

from .context_manager import ContextConfig, ContextManager
from .models import (
    AssistantMessage,
    CompactionEvent,
    SystemMessage,
    TerminalResult,
    TokenUsage,
    ToolUseBlock,
    UserMessage,
)
from .openai_model import OpenAICompatibleModelClient
from .powershell_tool import PowerShellTool
from .query_loop import QueryLoop, QueryLoopConfig
from .tool_registry import ToolRegistry, default_registry
from .tools import default_tools
from .transcript import Transcript

__all__ = [
    "AssistantMessage",
    "CompactionEvent",
    "ContextConfig",
    "ContextManager",
    "OpenAICompatibleModelClient",
    "PowerShellTool",
    "QueryLoop",
    "QueryLoopConfig",
    "SystemMessage",
    "TerminalResult",
    "TokenUsage",
    "ToolUseBlock",
    "Transcript",
    "ToolRegistry",
    "UserMessage",
    "default_tools",
    "default_registry",
]
