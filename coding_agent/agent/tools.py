from __future__ import annotations

import asyncio
import difflib
import fnmatch
import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


class ToolError(Exception):
    pass


class InputValidationError(ToolError):
    pass


class PermissionError(ToolError):
    pass


class ToolCancelledError(ToolError):
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


def _coerce_schema_value(value: Any, expected_type: Any) -> Any:
    if isinstance(expected_type, list):
        for typ in expected_type:
            try:
                return _coerce_schema_value(value, typ)
            except InputValidationError:
                continue
        return value
    if expected_type == "integer" and isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("-").isdigit():
            return int(stripped)
    if expected_type == "number" and isinstance(value, str):
        stripped = value.strip()
        try:
            return float(stripped)
        except ValueError:
            return value
    if expected_type == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n"):
            return False
    return value


def normalize_input(
    input_schema: Dict[str, Any],
    data: Dict[str, Any],
    argument_aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise InputValidationError("Tool input must be an object")
    if "_raw_arguments" in data:
        raise InputValidationError(
            "Tool arguments were malformed or not a JSON object: %r"
            % data.get("_raw_arguments")
        )

    normalized = dict(data)
    for alias, canonical in (argument_aliases or {}).items():
        if canonical not in normalized and alias in normalized:
            normalized[canonical] = normalized[alias]

    properties = input_schema.get("properties") or {}
    for key, spec in properties.items():
        if key not in normalized:
            continue
        normalized[key] = _coerce_schema_value(normalized[key], spec.get("type"))
    return normalized


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
        if "enum" in spec and value not in spec["enum"]:
            raise InputValidationError(
                "Field %s must be one of: %s"
                % (key, ", ".join(str(item) for item in spec["enum"]))
            )


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


async def _run_sync(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    call = functools.partial(function, *args, **kwargs)
    return await loop.run_in_executor(None, call)


def _list_dir_entries(path: Path, limit: int) -> List[str]:
    entries: List[str] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
        suffix = "/" if child.is_dir() else ""
        entries.append(child.name + suffix)
        if len(entries) >= limit:
            break
    return entries


def _glob_files(base: Path, pattern: str, root: Path, limit: int) -> List[str]:
    matches: List[str] = []
    for path in sorted(base.rglob(pattern)):
        if path.is_file():
            matches.append(str(path.relative_to(root)))
            if len(matches) >= limit:
                break
    return matches


def _grep_files(
    base: Path,
    root: Path,
    pattern: str,
    file_glob: str,
    limit: int,
) -> List[str]:
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
    return matches


@dataclass
class ToolResult:
    content: str
    raw: Any = None
    is_error: bool = False


@dataclass
class PermissionRequest:
    action: str
    tool_name: str
    path: Optional[Path] = None
    description: str = ""


PermissionCallback = Callable[[PermissionRequest], bool]
PermissionOverride = Callable[[PermissionRequest], Optional[bool]]
WriteContentFilter = Callable[[Path, str], str]


@dataclass
class PermissionPolicy:
    read: str = "allow"
    write: str = "deny"
    shell: str = "deny"
    callback: Optional[PermissionCallback] = None

    def check(self, request: PermissionRequest) -> None:
        mode = self.mode_for(request.action)
        if mode == "allow":
            return
        if mode == "deny":
            raise PermissionError(self._deny_message(request, "denied by policy"))
        if mode == "ask":
            if self.callback is None:
                raise PermissionError(
                    self._deny_message(request, "ask mode has no callback")
                )
            if not self.callback(request):
                raise PermissionError(self._deny_message(request, "denied by user"))
            return
        raise PermissionError(
            "Unknown permission mode for %s: %s" % (request.action, mode)
        )

    def mode_for(self, action: str) -> str:
        if action == "read":
            return self.read
        if action == "write":
            return self.write
        if action == "shell":
            return self.shell
        return "deny"

    def _deny_message(self, request: PermissionRequest, reason: str) -> str:
        path_text = " path=%s" % request.path if request.path is not None else ""
        detail = " %s" % request.description if request.description else ""
        return (
            "Permission %s for %s action=%s%s%s"
            % (reason, request.tool_name, request.action, path_text, detail)
        )


class Tool:
    name = ""
    description = ""
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}
    argument_aliases: Dict[str, str] = {}
    permission_action: Optional[str] = None

    def is_enabled(self) -> bool:
        return True

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
        normalized = self.prepare_input(input_data)
        return await self.call(normalized, context)

    def prepare_input(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_input(
            self.input_schema,
            input_data,
            self.argument_aliases,
        )
        validate_input(self.input_schema, normalized)
        return normalized

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
    permission_policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    shell_timeout_seconds: int = 30
    shell_max_output_chars: int = 30000
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    permission_override: Optional[PermissionOverride] = None
    write_content_filter: Optional[WriteContentFilter] = None

    def resolve_path(self, user_path: str) -> Path:
        return resolve_workspace_path(self.workspace_root, user_path)

    def check_permission(
        self,
        action: str,
        tool_name: str,
        path: Optional[Path] = None,
        description: str = "",
    ) -> None:
        request = PermissionRequest(
            action=action,
            tool_name=tool_name,
            path=path,
            description=description,
        )
        if self.permission_override is not None:
            override = self.permission_override(request)
            if override is True:
                return
            if override is False:
                raise PermissionError(
                    "Permission denied by active agent mode for %s action=%s path=%s"
                    % (tool_name, action, path or "")
                )
        if action == "write" and self.allow_writes:
            return
        self.permission_policy.check(request)

    def filter_write_content(self, path: Path, content: str) -> str:
        if self.write_content_filter is None:
            return content
        return self.write_content_filter(path.resolve(), content)


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace"
    permission_action = "read"
    argument_aliases = {"path": "file_path", "file": "file_path"}
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
        context.check_permission("read", self.name, path, "read text file")
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

        text = await _run_sync(path.read_text, encoding="utf-8", errors="replace")
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
    permission_action = "read"
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
        context.check_permission("read", self.name, path, "list directory")
        if not path.exists():
            raise ToolError("Path does not exist: %s" % path)
        if not path.is_dir():
            raise ToolError("Path is not a directory: %s" % path)
        entries = await _run_sync(_list_dir_entries, path, limit)
        return ToolResult(
            content="Directory: %s\n%s" % (path, "\n".join(entries)),
            raw={"path": str(path), "entries": entries},
        )


class GlobTool(Tool):
    name = "glob"
    description = "Find workspace files matching a glob pattern"
    permission_action = "read"
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
        context.check_permission("read", self.name, base, "glob files")
        if not base.exists() or not base.is_dir():
            raise ToolError("Search path is not a directory: %s" % base)
        matches = await _run_sync(
            _glob_files,
            base,
            pattern,
            context.workspace_root.resolve(),
            limit,
        )
        return ToolResult(
            content="Glob pattern: %s\n%s" % (pattern, "\n".join(matches)),
            raw={"pattern": pattern, "matches": matches},
        )


class GrepTool(Tool):
    name = "grep"
    description = "Search text files in the workspace"
    permission_action = "read"
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
        context.check_permission("read", self.name, base, "grep files")
        if not base.exists() or not base.is_dir():
            raise ToolError("Search path is not a directory: %s" % base)

        matches = await _run_sync(
            _grep_files,
            base,
            context.workspace_root.resolve(),
            pattern,
            file_glob,
            limit,
        )
        return ToolResult(
            content="Grep pattern: %s\n%s" % (pattern, "\n".join(matches)),
            raw={"pattern": pattern, "matches": matches},
        )


def make_unified_diff(old_text: str, new_text: str, path: Path) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=str(path) + " (before)",
            tofile=str(path) + " (after)",
            lineterm="",
        )
    )
    return "\n".join(diff_lines) if diff_lines else "(no changes)"


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write a UTF-8 text file inside the workspace and return a diff"
    permission_action = "write"
    argument_aliases = {"path": "file_path", "file": "file_path", "text": "content"}
    input_schema = {
        "type": "object",
        "required": ["file_path", "content"],
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
            "create_dirs": {"type": "boolean"},
            "overwrite": {"type": "boolean"},
        },
    }

    def is_read_only(self, input_data: Dict[str, Any]) -> bool:
        return False

    async def call(self, input_data: Dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(str(input_data["file_path"]))
        context.check_permission("write", self.name, path, "write text file")

        create_dirs = bool(input_data.get("create_dirs", False))
        overwrite = bool(input_data.get("overwrite", False))
        if path.exists() and not path.is_file():
            raise ToolError("Path is not a file: %s" % path)
        if path.exists() and not overwrite:
            raise ToolError("File already exists and overwrite=false: %s" % path)
        if not path.parent.exists():
            if create_dirs:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                raise ToolError("Parent directory does not exist: %s" % path.parent)

        old_text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        new_text = context.filter_write_content(path, str(input_data["content"]))
        path.write_text(new_text, encoding="utf-8")
        diff = make_unified_diff(old_text, new_text, path)
        return ToolResult(
            content="Wrote file: %s\nDiff:\n%s" % (path, diff),
            raw={
                "file_path": str(path),
                "old_chars": len(old_text),
                "new_chars": len(new_text),
                "diff": diff,
            },
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = "Replace text in a workspace file and return a diff"
    permission_action = "write"
    argument_aliases = {"path": "file_path", "file": "file_path"}
    input_schema = {
        "type": "object",
        "required": ["file_path", "old_text", "new_text"],
        "properties": {
            "file_path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
    }

    def is_read_only(self, input_data: Dict[str, Any]) -> bool:
        return False

    async def call(self, input_data: Dict[str, Any], context: ToolContext) -> ToolResult:
        path = context.resolve_path(str(input_data["file_path"]))
        context.check_permission("write", self.name, path, "edit text file")
        if not path.exists():
            raise ToolError("File does not exist: %s" % path)
        if not path.is_file():
            raise ToolError("Path is not a file: %s" % path)

        old_text = str(input_data["old_text"])
        new_text = str(input_data["new_text"])
        replace_all = bool(input_data.get("replace_all", False))
        if old_text == "":
            raise InputValidationError("old_text must not be empty")

        original = path.read_text(encoding="utf-8", errors="replace")
        count = original.count(old_text)
        if count == 0:
            raise ToolError("old_text was not found in file: %s" % path)
        replacements = count if replace_all else 1
        updated = original.replace(old_text, new_text, replacements)
        updated = context.filter_write_content(path, updated)
        path.write_text(updated, encoding="utf-8")
        diff = make_unified_diff(original, updated, path)
        return ToolResult(
            content=(
                "Edited file: %s\n"
                "Replacements: %d\n"
                "Diff:\n%s"
                % (path, replacements, diff)
            ),
            raw={
                "file_path": str(path),
                "replacements": replacements,
                "diff": diff,
            },
        )


def default_tools() -> List[Tool]:
    tools: List[Tool] = [
        ReadFileTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
        WriteFileTool(),
        EditFileTool(),
    ]
    from .powershell_tool import PowerShellTool

    tools.append(PowerShellTool())
    return tools


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
