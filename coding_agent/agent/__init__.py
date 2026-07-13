"""Minimal Claude Code-style coding agent core."""

from .models import AssistantMessage, TerminalResult, ToolUseBlock, UserMessage
from .openai_model import OpenAICompatibleModelClient
from .powershell_tool import PowerShellTool
from .query_loop import QueryLoop, QueryLoopConfig
from .tool_registry import ToolRegistry, default_registry
from .tools import default_tools
from .transcript import Transcript

__all__ = [
    "AssistantMessage",
    "OpenAICompatibleModelClient",
    "PowerShellTool",
    "QueryLoop",
    "QueryLoopConfig",
    "TerminalResult",
    "ToolUseBlock",
    "Transcript",
    "ToolRegistry",
    "UserMessage",
    "default_tools",
    "default_registry",
]
