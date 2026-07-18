from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .tools import PermissionPolicy, Tool, default_tools


class ToolRegistry:
    """Mutable source of truth for tools available to a query loop."""

    def __init__(self, tools: Optional[Iterable[Tool]] = None):
        self._tools: Dict[str, Tool] = {}
        self._disabled = set()
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool, replace: bool = False) -> None:
        name = str(tool.name).strip()
        if not name:
            raise ValueError("Tool name must not be empty")
        if name in self._tools and not replace:
            raise ValueError("Tool is already registered: %s" % name)
        self._tools[name] = tool

    def unregister(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError("Tool is not registered: %s" % name)
        self._disabled.discard(name)
        return self._tools.pop(name)

    def enable(self, name: str) -> None:
        self._require_registered(name)
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        self._require_registered(name)
        self._disabled.add(name)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all_tools(self) -> Tuple[Tool, ...]:
        return tuple(self._tools.values())

    def enabled_tools(self) -> Tuple[Tool, ...]:
        enabled: List[Tool] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            try:
                if not tool.is_enabled():
                    continue
            except Exception:
                continue
            enabled.append(tool)
        return tuple(enabled)

    def available_tools(self, permission_policy: PermissionPolicy) -> Tuple[Tool, ...]:
        available: List[Tool] = []
        for tool in self.enabled_tools():
            action = tool.permission_action
            if action and permission_policy.mode_for(action) == "deny":
                continue
            available.append(tool)
        return tuple(available)

    def is_disabled(self, name: str) -> bool:
        self._require_registered(name)
        return name in self._disabled

    def _require_registered(self, name: str) -> None:
        if name not in self._tools:
            raise KeyError("Tool is not registered: %s" % name)


def default_registry() -> ToolRegistry:
    return ToolRegistry(default_tools())
