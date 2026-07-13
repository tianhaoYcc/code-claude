from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.mock_model import ScriptedModelClient
from agent.models import AssistantMessage, UserMessage
from agent.powershell_tool import (
    PowerShellTool,
    ShellProcessResult,
    analyze_powershell_command,
)
from agent.query_loop import QueryLoop, QueryLoopConfig
from agent.tools import (
    InputValidationError,
    PermissionError as ToolPermissionError,
    PermissionPolicy,
    ToolContext,
)


def make_context(workspace: Path, shell="allow", max_output=30000):
    return ToolContext(
        workspace_root=workspace,
        output_dir=workspace / ".agent_outputs",
        permission_policy=PermissionPolicy(shell=shell),
        shell_max_output_chars=max_output,
        cancel_event=asyncio.Event(),
    )


class PowerShellToolTests(unittest.IsolatedAsyncioTestCase):
    def test_command_analysis_distinguishes_read_mutating_network_and_dangerous(self):
        self.assertEqual(
            analyze_powershell_command(
                "Get-Content README.md | Select-String QueryLoop"
            ).category,
            "read_only",
        )
        self.assertEqual(
            analyze_powershell_command("Set-Content demo.txt hello").category,
            "mutating",
        )
        self.assertEqual(
            analyze_powershell_command("Invoke-WebRequest https://example.com").category,
            "network",
        )
        self.assertEqual(
            analyze_powershell_command("Remove-Item demo.txt").category,
            "dangerous",
        )
        self.assertEqual(
            analyze_powershell_command("python -m unittest").category,
            "unknown",
        )

    async def test_read_only_command_runs_with_workspace_cwd(self):
        calls = []

        async def runner(command, cwd, timeout, cancel_event):
            calls.append((command, cwd, timeout, cancel_event.is_set()))
            return ShellProcessResult(0, stdout="README contents\n")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tool = PowerShellTool(runner=runner)
            result = await tool.execute(
                {"command": "Get-Content README.md", "timeout_seconds": 12},
                make_context(workspace),
            )

        self.assertEqual(calls[0][0], "Get-Content README.md")
        self.assertEqual(calls[0][1], workspace)
        self.assertEqual(calls[0][2], 12)
        self.assertFalse(calls[0][3])
        self.assertFalse(result.is_error)
        self.assertIn("README contents", result.content)

    async def test_shell_permission_deny_prevents_runner(self):
        calls = []

        async def runner(command, cwd, timeout, cancel_event):
            calls.append(command)
            return ShellProcessResult(0)

        with tempfile.TemporaryDirectory() as tmp:
            tool = PowerShellTool(runner=runner)
            with self.assertRaises(ToolPermissionError):
                await tool.execute(
                    {"command": "Get-Content README.md"},
                    make_context(Path(tmp), shell="deny"),
                )

        self.assertEqual(calls, [])

    async def test_dangerous_command_is_blocked_before_runner_even_when_allowed(self):
        calls = []

        async def runner(command, cwd, timeout, cancel_event):
            calls.append(command)
            return ShellProcessResult(0)

        with tempfile.TemporaryDirectory() as tmp:
            tool = PowerShellTool(runner=runner)
            with self.assertRaises(ToolPermissionError) as captured:
                await tool.execute(
                    {"command": "Remove-Item demo.txt"},
                    make_context(Path(tmp), shell="allow"),
                )

        self.assertIn("Blocked dangerous", str(captured.exception))
        self.assertEqual(calls, [])

    async def test_explicit_path_outside_workspace_is_blocked(self):
        calls = []

        async def runner(command, cwd, timeout, cancel_event):
            calls.append(command)
            return ShellProcessResult(0)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            tool = PowerShellTool(runner=runner)

            with self.assertRaises(ToolPermissionError):
                await tool.execute(
                    {"command": 'Get-Content "%s"' % outside},
                    make_context(workspace),
                )

        self.assertEqual(calls, [])

    async def test_large_output_is_written_to_agent_outputs(self):
        async def runner(command, cwd, timeout, cancel_event):
            return ShellProcessResult(0, stdout="x" * 1000)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tool = PowerShellTool(runner=runner)
            result = await tool.execute(
                {"command": "Get-Content big.txt"},
                make_context(workspace, max_output=120),
            )

            output_files = list((workspace / ".agent_outputs").glob("shell-*.txt"))
            self.assertEqual(len(output_files), 1)
            self.assertIn("x" * 500, output_files[0].read_text(encoding="utf-8"))

        self.assertIn("Full output written to:", result.content)
        self.assertIsNotNone(result.raw["output_file"])

    async def test_nonzero_exit_is_an_error_tool_result(self):
        async def runner(command, cwd, timeout, cancel_event):
            return ShellProcessResult(7, stderr="failed")

        with tempfile.TemporaryDirectory() as tmp:
            tool = PowerShellTool(runner=runner)
            result = await tool.execute(
                {"command": "python -m unittest"},
                make_context(Path(tmp)),
            )

        self.assertTrue(result.is_error)
        self.assertIn("exit_code: 7", result.content)
        self.assertIn("failed", result.content)

    async def test_nonzero_exit_stays_error_when_injected_into_query_loop(self):
        async def runner(command, cwd, timeout, cancel_event):
            return ShellProcessResult(3, stderr="command failed")

        tool = PowerShellTool(runner=runner)
        model = ScriptedModelClient(
            [
                AssistantMessage.tool_use(tool.name, {"command": "python bad.py"}),
                AssistantMessage.text("done"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            loop = QueryLoop(
                model,
                Path(tmp),
                tools=[tool],
                config=QueryLoopConfig(shell_permission="allow"),
            )
            events = []
            async for event in loop.run("run command"):
                events.append(event)

        result_message = next(
            event
            for event in events
            if isinstance(event, UserMessage) and event.is_meta
        )
        self.assertTrue(result_message.content[0]["is_error"])
        self.assertIn("command failed", result_message.content[0]["content"])

    async def test_timeout_bounds_are_validated(self):
        async def runner(command, cwd, timeout, cancel_event):
            return ShellProcessResult(0)

        with tempfile.TemporaryDirectory() as tmp:
            tool = PowerShellTool(runner=runner)
            with self.assertRaises(InputValidationError):
                await tool.execute(
                    {"command": "Get-Location", "timeout_seconds": 0},
                    make_context(Path(tmp)),
                )


if __name__ == "__main__":
    unittest.main()
