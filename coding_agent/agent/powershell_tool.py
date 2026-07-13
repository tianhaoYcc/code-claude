from __future__ import annotations

import asyncio
import locale
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Sequence
from uuid import uuid4

from .tools import (
    InputValidationError,
    PermissionError,
    Tool,
    ToolCancelledError,
    ToolContext,
    ToolResult,
    resolve_workspace_path,
)


@dataclass(frozen=True)
class PowerShellAnalysis:
    category: str
    reason: str

    @property
    def is_read_only(self) -> bool:
        return self.category == "read_only"


@dataclass
class ShellProcessResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False


PowerShellRunner = Callable[
    [str, Path, int, asyncio.Event],
    Awaitable[ShellProcessResult],
]


_DANGEROUS_PATTERNS = (
    (r"(?:^|[\s;|])(?:remove-item|rm|del|erase|rd|rmdir)(?:\s|$)", "file deletion"),
    (r"\b(?:clear-content|format-volume|clear-disk|initialize-disk|remove-partition)\b", "destructive storage command"),
    (r"\b(?:stop-computer|restart-computer|shutdown|diskpart|bcdedit)\b", "system-level command"),
    (r"\b(?:set-executionpolicy|invoke-expression|iex)\b", "dynamic or policy-changing execution"),
    (r"(?:^|\s)-(?:encodedcommand|enc)(?:\s|$)", "encoded command"),
    (r"(?:^|[\s;|])(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)(?:\s|$)", "nested PowerShell process"),
    (r"\bstart-process\b", "nested process launch"),
    (r"\b(?:set-location|push-location|pop-location|new-psdrive|remove-psdrive)\b", "working-directory or drive mutation"),
    (r"(?:^|[\s;|])(?:cd|chdir|sl)(?:\s|$)", "working-directory mutation"),
    (r"\bgit\s+reset\s+--hard\b", "destructive git reset"),
    (r"\bgit\s+clean\s+-[^\s]*f", "destructive git clean"),
    (r"\bgit\s+(?:checkout\s+--|restore\b)", "destructive git file restore"),
    (r"\bgit\s+push\b[^\r\n;|]*(?:--force|-f(?:\s|$))", "force git push"),
)

_MUTATING_PATTERNS = (
    r"\b(?:set-content|add-content|out-file|tee-object)\b",
    r"\b(?:new-item|copy-item|move-item|rename-item)\b",
    r"(?:^|[\s;|])(?:mkdir|md|ni|cp|mv|ren)(?:\s|$)",
    r"\b(?:set-item|set-itemproperty|new-itemproperty|remove-itemproperty)\b",
    r"\bgit\s+(?:add|commit|checkout|switch|merge|rebase|cherry-pick|tag|push|pull|fetch)\b",
    r"\b(?:pip|conda|npm|pnpm|yarn|cargo)\s+(?:install|uninstall|remove|update|add)\b",
    r"(?<![<>=])>{1,2}(?![=>])",
)

_NETWORK_PATTERNS = (
    r"\b(?:invoke-webrequest|invoke-restmethod|iwr|irm|wget|curl)\b",
)

_DYNAMIC_PATTERNS = (
    r"\$\(",
    r"`",
    r"(?:^|[\s;|])&\s*[^&]",
    r"\b(?:env|registry|cert|wsman|variable|function):",
    r"\$env:",
)

_READ_ONLY_COMMANDS = {
    "get-childitem",
    "gci",
    "dir",
    "ls",
    "get-content",
    "gc",
    "type",
    "cat",
    "select-string",
    "test-path",
    "get-item",
    "get-location",
    "pwd",
    "resolve-path",
    "measure-object",
    "sort-object",
    "format-list",
    "format-table",
    "out-string",
    "write-output",
    "echo",
    "get-command",
    "get-process",
    "rg",
    "rg.exe",
    "findstr",
    "findstr.exe",
    "where",
    "where.exe",
}

_READ_ONLY_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "grep",
    "blame",
    "describe",
}


def analyze_powershell_command(command: str) -> PowerShellAnalysis:
    text = command.strip()
    if not text:
        return PowerShellAnalysis("invalid", "command is empty")
    if "\x00" in text:
        return PowerShellAnalysis("invalid", "command contains a NUL byte")

    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return PowerShellAnalysis("dangerous", reason)
    for pattern in _NETWORK_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return PowerShellAnalysis("network", "network access")
    for pattern in _MUTATING_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return PowerShellAnalysis("mutating", "command may change external state")
    for pattern in _DYNAMIC_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return PowerShellAnalysis("unknown", "dynamic syntax cannot be validated statically")

    segments = [part.strip() for part in re.split(r"(?:\|\||&&|[;|])", text)]
    if segments and all(_segment_is_read_only(segment) for segment in segments):
        return PowerShellAnalysis("read_only", "all command segments are allowlisted reads")
    return PowerShellAnalysis("unknown", "command is not in the read-only allowlist")


def _segment_is_read_only(segment: str) -> bool:
    if not segment:
        return True
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return False
    if not tokens:
        return True
    command_name = tokens[0].strip("\"'").lower()
    if command_name in _READ_ONLY_COMMANDS:
        return True
    if command_name in ("git", "git.exe"):
        if len(tokens) < 2:
            return False
        return tokens[1].strip("\"'").lower() in _READ_ONLY_GIT_SUBCOMMANDS
    return False


def validate_explicit_workspace_paths(command: str, workspace_root: Path) -> None:
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError as exc:
        raise InputValidationError("PowerShell command could not be tokenized: %s" % exc)

    for token in tokens:
        candidate = _path_candidate_from_token(token)
        if candidate is None:
            continue
        resolve_workspace_path(workspace_root, candidate)


def _path_candidate_from_token(token: str) -> Optional[str]:
    cleaned = token.strip("\"'`,;()[]{}")
    if not cleaned:
        return None
    if "=" in cleaned:
        cleaned = cleaned.split("=", 1)[1].strip("\"'")
    if re.match(r"^-[A-Za-z]+:[A-Za-z]:[\\/]", cleaned):
        cleaned = cleaned.split(":", 1)[1]

    if re.match(r"^[A-Za-z]:[\\/]", cleaned) or cleaned.startswith("\\\\"):
        return cleaned
    if cleaned == ".." or cleaned.startswith("..\\") or cleaned.startswith("../"):
        return cleaned
    return None


class PowerShellTool(Tool):
    name = "powershell"
    description = (
        "Run a PowerShell command from the workspace with permission checks, "
        "dangerous-command blocking, timeout, and output budgeting"
    )
    permission_action = "shell"
    argument_aliases = {"cmd": "command"}
    input_schema = {
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string"},
            "description": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
        },
    }

    def __init__(
        self,
        runner: Optional[PowerShellRunner] = None,
        executable: Optional[str] = None,
    ):
        self._runner = runner
        self._executable = executable

    def is_enabled(self) -> bool:
        if self._runner is not None:
            return True
        return os.name == "nt" and self._find_executable() is not None

    def is_read_only(self, input_data):
        command = str(input_data.get("command") or "")
        return analyze_powershell_command(command).is_read_only

    def is_concurrency_safe(self, input_data):
        return self.is_read_only(input_data)

    async def call(self, input_data, context: ToolContext) -> ToolResult:
        command = str(input_data["command"]).strip()
        analysis = analyze_powershell_command(command)
        if analysis.category == "invalid":
            raise InputValidationError(analysis.reason)
        if analysis.category == "dangerous":
            raise PermissionError(
                "Blocked dangerous PowerShell command: %s" % analysis.reason
            )

        validate_explicit_workspace_paths(command, context.workspace_root)
        timeout_value = input_data.get("timeout_seconds")
        timeout_seconds = int(
            context.shell_timeout_seconds
            if timeout_value is None
            else timeout_value
        )
        if timeout_seconds < 1 or timeout_seconds > 600:
            raise InputValidationError("timeout_seconds must be between 1 and 600")

        description = str(input_data.get("description") or analysis.reason)
        context.check_permission(
            "shell",
            self.name,
            description=(
                "classification=%s; %s; command=%s"
                % (analysis.category, description, command)
            ),
        )
        if context.cancel_event.is_set():
            raise ToolCancelledError("PowerShell command was cancelled before start")

        result = await self._run(command, context, timeout_seconds)
        content, output_file = _format_and_budget_result(
            result,
            analysis,
            context.output_dir,
            context.shell_max_output_chars,
        )
        return ToolResult(
            content=content,
            is_error=(
                result.exit_code != 0 or result.timed_out or result.cancelled
            ),
            raw={
                "command": command,
                "classification": analysis.category,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "stdout_chars": len(result.stdout),
                "stderr_chars": len(result.stderr),
                "output_file": str(output_file) if output_file else None,
            },
        )

    async def _run(
        self,
        command: str,
        context: ToolContext,
        timeout_seconds: int,
    ) -> ShellProcessResult:
        if self._runner is not None:
            return await self._runner(
                command,
                context.workspace_root,
                timeout_seconds,
                context.cancel_event,
            )
        executable = self._find_executable()
        if executable is None:
            raise PermissionError("PowerShell executable was not found")
        return await run_powershell_process(
            executable,
            command,
            context.workspace_root,
            timeout_seconds,
            context.cancel_event,
        )

    def _find_executable(self) -> Optional[str]:
        if self._executable:
            return self._executable
        return shutil.which("pwsh") or shutil.which("powershell")


async def run_powershell_process(
    executable: str,
    command: str,
    cwd: Path,
    timeout_seconds: int,
    cancel_event: asyncio.Event,
) -> ShellProcessResult:
    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

    process = await asyncio.create_subprocess_exec(
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-Command",
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    communicate_task = asyncio.create_task(process.communicate())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            (communicate_task, cancel_task),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate_task in done:
            stdout_data, stderr_data = communicate_task.result()
            return ShellProcessResult(
                exit_code=int(process.returncode or 0),
                stdout=_decode_output(stdout_data),
                stderr=_decode_output(stderr_data),
            )

        cancelled = cancel_task in done and cancel_event.is_set()
        await _terminate_process_tree(process)
        stdout_data, stderr_data = await _collect_after_termination(communicate_task)
        reason = "Command cancelled" if cancelled else "Command timed out"
        stderr = _decode_output(stderr_data)
        stderr = (stderr + "\n" + reason).strip()
        return ShellProcessResult(
            exit_code=-1,
            stdout=_decode_output(stdout_data),
            stderr=stderr,
            timed_out=not cancelled,
            cancelled=cancelled,
        )
    except asyncio.CancelledError:
        await _terminate_process_tree(process)
        communicate_task.cancel()
        raise
    finally:
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            process.kill()
    else:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()


async def _collect_after_termination(
    communicate_task: "asyncio.Task[Sequence[bytes]]",
):
    try:
        return await asyncio.wait_for(communicate_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
        return b"", b""


def _decode_output(data: bytes) -> str:
    if not data:
        return ""
    encodings: List[str] = []
    if b"\x00" in data:
        encodings.append("utf-16-le")
    encodings.extend(["utf-8-sig", locale.getpreferredencoding(False)])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _format_and_budget_result(
    result: ShellProcessResult,
    analysis: PowerShellAnalysis,
    output_dir: Path,
    max_output_chars: int,
):
    parts = [
        "PowerShell exit_code: %d" % result.exit_code,
        "Classification: %s" % analysis.category,
    ]
    if result.stdout:
        parts.append("STDOUT:\n%s" % result.stdout.rstrip())
    if result.stderr:
        parts.append("STDERR:\n%s" % result.stderr.rstrip())
    if not result.stdout and not result.stderr:
        parts.append("(no output)")
    full_content = "\n".join(parts)
    if max_output_chars <= 0 or len(full_content) <= max_output_chars:
        return full_content, None

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / ("shell-%s.txt" % uuid4().hex)
    output_path.write_text(full_content, encoding="utf-8")
    content = (
        "PowerShell output exceeded shell budget.\n"
        "Full output written to: %s\n"
        "Preview (%d chars):\n%s"
        % (output_path, max_output_chars, full_content[:max_output_chars])
    )
    return content, output_path
