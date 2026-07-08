from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class ToolError(Exception):
    pass


class InputValidationError(ToolError):
    pass


class PermissionError(ToolError):
    pass


def _schema_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return True


def validate_input(input_schema: Dict[str, Any], data: Dict[str, Any]) -> None:
    if input_schema.get("type") != "object":
        raise InputValidationError("Tool input schema must be an object schema")
    if not isinstance(data, dict):
        raise InputValidationError("Tool input must be an object")

    required = input_schema.get("required") or []
    for key in required:
        if key not in data:
            raise InputValidationError("Missing required field: %s" % key)

    properties = input_schema.get("properties") or {}
    for key, value in data.items():
        spec = properties.get(key)
        if not spec:
            continue
        expected_type = spec.get("type")
        if isinstance(expected_type, list):
            if not any(_schema_type_matches(value, typ) for typ in expected_type):
                raise InputValidationError("Field %s has invalid type" % key)
        elif expected_type and not _schema_type_matches(value, expected_type):
            raise InputValidationError("Field %s must be %s" % (key, expected_type))


def resolve_workspace_path(workspace_root: Path, user_path: str) -> Path:
    root = workspace_root.resolve()
    candidate = Path(user_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PermissionError(
            "Path is outside workspace: %s (workspace: %s)" % (resolved, root)
        )
    return resolved


@dataclass
class ToolResult:
    content: str
    raw: Any = None


class Tool:
    name = ""
    description = ""
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    def is_concurrency_safe(self, input_data: Dict[str, Any]) -> bool:
        return False

    def is_read_only(self, input_data: Dict[str, Any]) -> bool:
        return True

    async def call(self, input_data: Dict[str, Any], context: "ToolContext") -> ToolResult:
        raise NotImplementedError

    async def execute(
        self,
        input_data: Dict[str, Any],
        context: "ToolContext",
    ) -> ToolResult:
        validate_input(self.input_schema, input_data)
        return await self.call(input_data, context)

    def schema_for_model(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolContext:
    workspace_root: Path
    output_dir: Path
    allow_writes: bool = False

    def resolve_path(self, user_path: str) -> Path:
        return resolve_workspace_path(self.workspace_root, user_path)


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace"
    input_schema = {
        "type": "object",
        "required": ["file_path"],
        "properties": {
            "file_path": {"type": "string"},
            "start_line": {"type": "integer"},
            "limit": {"type": "integer"},
        },
    }

    def is_concurrency_safe(self, input_data: Dict[str, Any]) -> bool:
        return True

    async def call(self, input_data: Dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(str(input_data["file_path"]))
        if not path.exists():
            raise ToolError("File does not exist: %s" % path)
        if not path.is_file():
            raise ToolError("Path is not a file: %s" % path)

        start_line = int(input_data.get("start_line") or 1)
        limit = int(input_data.get("limit") or 400)
        if start_line < 1:
            raise InputValidationError("start_line must be >= 1")
        if limit < 1:
            raise InputValidationError("limit must be >= 1")

        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[start_line - 1 : start_line - 1 + limit]
        numbered = [
            "%4d | %s" % (line_no, line)
            for line_no, line in enumerate(selected, start=start_line)
        ]
        content = "File: %s\n%s" % (path, "\n".join(numbered))
        return ToolResult(
            content=content,
            raw={
                "file_path": str(path),
                "start_line": start_line,
                "line_count": len(selected),
                "content": "\n".join(selected),
            },
        )


class ListDirTool(Tool):
    name = "list_dir"
    description = "List files and directories under a workspace path"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        },
    }

    def is_concurrency_safe(self, input_data: Dict[str, Any]) -> bool:
        return True

    async def call(self, input_data: Dict[str, Any], context: ToolContext) -> ToolResult:
        rel_path = str(input_data.get("path") or ".")
        limit = int(input_data.get("limit") or 200)
        path = context.resolve_path(rel_path)
        if not path.exists():
            raise ToolError("Path does not exist: %s" % path)
        if not path.is_dir():
            raise ToolError("Path is not a directory: %s" % path)
        entries = []
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            suffix = "/" if child.is_dir() else ""
            entries.append(child.name + suffix)
            if len(entries) >= limit:
                break
        return ToolResult(
            content="Directory: %s\n%s" % (path, "\n".join(entries)),
            raw={"path": str(path), "entries": entries},
        )


class GlobTool(Tool):
    name = "glob"
    description = "Find workspace files matching a glob pattern"
    input_schema = {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        },
    }

    def is_concurrency_safe(self, input_data: Dict[str, Any]) -> bool:
        return True

    async def call(self, input_data: Dict[str, Any], context: ToolContext) -> ToolResult:
        base = context.resolve_path(str(input_data.get("path") or "."))
        pattern = str(input_data["pattern"])
        limit = int(input_data.get("limit") or 200)
        if not base.exists() or not base.is_dir():
            raise ToolError("Search path is not a directory: %s" % base)
        matches = []
        for path in sorted(base.rglob(pattern)):
            if path.is_file():
                matches.append(str(path.relative_to(context.workspace_root.resolve())))
                if len(matches) >= limit:
                    break
        return ToolResult(
            content="Glob pattern: %s\n%s" % (pattern, "\n".join(matches)),
            raw={"pattern": pattern, "matches": matches},
        )


class GrepTool(Tool):
    name = "grep"
    description = "Search text files in the workspace"
    input_schema = {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "limit": {"type": "integer"},
        },
    }

    def is_concurrency_safe(self, input_data: Dict[str, Any]) -> bool:
        return True

    async def call(self, input_data: Dict[str, Any], context: ToolContext) -> ToolResult:
        base = context.resolve_path(str(input_data.get("path") or "."))
        pattern = str(input_data["pattern"])
        file_glob = str(input_data.get("glob") or "*")
        limit = int(input_data.get("limit") or 100)
        if not base.exists() or not base.is_dir():
            raise ToolError("Search path is not a directory: %s" % base)

        root = context.workspace_root.resolve()
        matches: List[str] = []
        for path in sorted(base.rglob("*")):
            if len(matches) >= limit:
                break
            if not path.is_file() or not fnmatch.fnmatch(path.name, file_glob):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_no, line in enumerate(lines, start=1):
                if pattern in line:
                    rel = str(path.relative_to(root))
                    matches.append("%s:%d:%s" % (rel, line_no, line))
                    if len(matches) >= limit:
                        break
        return ToolResult(
            content="Grep pattern: %s\n%s" % (pattern, "\n".join(matches)),
            raw={"pattern": pattern, "matches": matches},
        )


def default_tools() -> List[Tool]:
    return [ReadFileTool(), ListDirTool(), GlobTool(), GrepTool()]


def find_tool(tools: Sequence[Tool], name: str) -> Optional[Tool]:
    for tool in tools:
        if tool.name == name:
            return tool
    return None


def apply_tool_result_budget(
    result: ToolResult,
    tool_use_id: str,
    context: ToolContext,
    max_inline_chars: int,
) -> Tuple[str, Any]:
    content = result.content
    if max_inline_chars <= 0 or len(content) <= max_inline_chars:
        return content, result.raw

    context.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = context.output_dir / ("%s.txt" % tool_use_id)
    output_path.write_text(content, encoding="utf-8")
    preview = content[:max_inline_chars]
    budgeted = (
        "Tool result exceeded inline budget.\n"
        "Full output written to: %s\n"
        "Preview (%d chars):\n%s"
        % (output_path, max_inline_chars, preview)
    )
    return budgeted, {
        "truncated": True,
        "output_file": str(output_path),
        "preview_chars": max_inline_chars,
    }
