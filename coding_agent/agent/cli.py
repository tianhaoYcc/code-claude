from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from .context_manager import ContextConfig
from .mock_model import HeuristicMockModelClient
from .models import (
    AssistantMessage,
    CompactionEvent,
    RequestStartEvent,
    TerminalResult,
    ToolEvent,
    UserMessage,
)
from .openai_model import OpenAICompatibleModelClient
from .query_loop import QueryLoop, QueryLoopConfig
from .tools import PermissionRequest
from .transcript import Transcript


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the coding agent.")
    parser.add_argument("prompt", nargs="?", default="", help="Prompt to send")
    parser.add_argument(
        "--model-client",
        choices=["auto", "openai", "mock"],
        default="auto",
        help="Model backend. auto uses .env when available, otherwise mock.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional .env path for LLM_API_KEY, LLM_MODEL_ID, LLM_BASE_URL.",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root. File tools cannot access paths outside it.",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Transcript JSONL path. Defaults to .agent_sessions/<uuid>.jsonl.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Load existing transcript messages before appending the new prompt.",
    )
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument(
        "--read-permission",
        choices=["allow", "deny", "ask"],
        default="allow",
        help="Permission mode for read tools.",
    )
    parser.add_argument(
        "--write-permission",
        choices=["allow", "deny", "ask"],
        default="deny",
        help="Permission mode for write/edit tools.",
    )
    parser.add_argument(
        "--shell-permission",
        choices=["allow", "deny", "ask"],
        default="deny",
        help="Permission mode for the PowerShell tool.",
    )
    parser.add_argument(
        "--max-tool-concurrency",
        type=int,
        default=10,
        help="Maximum concurrent read-only tool calls.",
    )
    parser.add_argument(
        "--shell-timeout-seconds",
        type=int,
        default=30,
        help="Default timeout for a PowerShell command.",
    )
    parser.add_argument(
        "--shell-max-output-chars",
        type=int,
        default=30000,
        help="PowerShell output budget before the full result is written to disk.",
    )
    parser.add_argument(
        "--disable-tool",
        action="append",
        default=[],
        metavar="NAME",
        help="Disable a registered tool before exposing tools to the model. Repeatable.",
    )
    parser.add_argument(
        "--max-bad-tool-input-attempts",
        type=int,
        default=3,
        help="Stop after this many malformed or schema-invalid tool inputs.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Run a manual full compaction before sending the prompt.",
    )
    parser.add_argument(
        "--no-auto-compact",
        action="store_false",
        dest="auto_compact",
        help="Disable threshold-based automatic context compaction.",
    )
    parser.set_defaults(auto_compact=True)
    parser.add_argument(
        "--context-window-tokens",
        type=int,
        default=128000,
        help="Model context window used by the local compaction policy.",
    )
    parser.add_argument(
        "--reserved-output-tokens",
        type=int,
        default=8192,
        help="Tokens reserved for the next model response.",
    )
    parser.add_argument(
        "--auto-compact-ratio",
        type=float,
        default=0.8,
        help="Compact when estimated usage reaches this effective-context ratio.",
    )
    parser.add_argument(
        "--preserve-recent-groups",
        type=int,
        default=4,
        help="Atomic recent message groups preserved verbatim after full compaction.",
    )
    parser.add_argument(
        "--microcompact-keep-tool-results",
        type=int,
        default=4,
        help="Number of recent tool results excluded from microcompact.",
    )
    return parser


def build_model_client(args: argparse.Namespace):
    if args.model_client == "mock":
        return HeuristicMockModelClient()

    env_file = Path(args.env_file) if args.env_file else None
    try:
        return OpenAICompatibleModelClient.from_env(env_file=env_file)
    except ValueError:
        if args.model_client == "auto":
            print("[model] 未找到完整 LLM .env 配置，回退到 mock 模型。")
            return HeuristicMockModelClient()
        raise


def ask_permission(request: PermissionRequest) -> bool:
    print(
        "[permission] tool=%s action=%s path=%s %s"
        % (
            request.tool_name,
            request.action,
            request.path or "",
            request.description,
        )
    )
    answer = input("Allow this tool call? [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def run_cli(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    session_path = (
        Path(args.session)
        if args.session
        else workspace / ".agent_sessions" / ("%s.jsonl" % uuid4())
    )
    transcript = Transcript(session_path)
    initial_messages = transcript.load_messages(strict=True) if args.resume else []
    loop = QueryLoop(
        model_client=build_model_client(args),
        workspace_root=workspace,
        transcript=transcript,
        config=QueryLoopConfig(
            max_turns=args.max_turns,
            max_bad_tool_input_attempts=args.max_bad_tool_input_attempts,
            max_tool_concurrency=args.max_tool_concurrency,
            shell_timeout_seconds=args.shell_timeout_seconds,
            shell_max_output_chars=args.shell_max_output_chars,
            read_permission=args.read_permission,
            write_permission=args.write_permission,
            shell_permission=args.shell_permission,
            permission_callback=ask_permission,
            disabled_tools=tuple(args.disable_tool),
            context=ContextConfig(
                auto_compact_enabled=args.auto_compact,
                context_window_tokens=args.context_window_tokens,
                reserved_output_tokens=args.reserved_output_tokens,
                auto_compact_ratio=args.auto_compact_ratio,
                preserve_recent_groups=args.preserve_recent_groups,
                microcompact_keep_recent_tool_results=(
                    args.microcompact_keep_tool_results
                ),
            ),
        ),
        initial_messages=initial_messages,
    )

    if args.compact:
        async for event in loop.compact(trigger="manual"):
            print_event(event)
        if not args.prompt:
            print("transcript:", session_path)
            return 0

    async for event in loop.run(args.prompt):
        print_event(event)

    print("transcript:", session_path)
    return 0


def print_event(event) -> None:
    if isinstance(event, UserMessage):
        if not event.is_meta:
            print("user:", event.content)
    elif isinstance(event, RequestStartEvent):
        print("[request_start] turn=%d" % event.turn_count)
    elif isinstance(event, AssistantMessage):
        text = event.text_content()
        if text:
            print("assistant:", text)
        for tool_use in event.tool_uses():
            print("assistant tool_use: %s %s" % (tool_use.name, tool_use.input))
    elif isinstance(event, ToolEvent):
        print(
            "[tool] %s %s %s"
            % (event.status, event.tool_name, event.message or "")
        )
    elif isinstance(event, CompactionEvent):
        print(
            "[compact] %s trigger=%s tokens=%d->%d %s"
            % (
                event.status,
                event.trigger,
                event.before_tokens,
                event.after_tokens,
                event.message or "",
            )
        )
    elif isinstance(event, TerminalResult):
        print("[terminal] reason=%s turns=%d" % (event.reason, event.turn_count))


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run_cli(args)))


if __name__ == "__main__":
    main()
