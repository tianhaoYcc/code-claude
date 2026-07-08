from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from .mock_model import HeuristicMockModelClient
from .models import AssistantMessage, RequestStartEvent, TerminalResult, ToolEvent, UserMessage
from .openai_model import OpenAICompatibleModelClient
from .query_loop import QueryLoop, QueryLoopConfig
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
        config=QueryLoopConfig(max_turns=args.max_turns),
        initial_messages=initial_messages,
    )

    async for event in loop.run(args.prompt):
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
        elif isinstance(event, TerminalResult):
            print("[terminal] reason=%s turns=%d" % (event.reason, event.turn_count))

    print("transcript:", session_path)
    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run_cli(args)))


if __name__ == "__main__":
    main()
