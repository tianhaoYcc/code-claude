from __future__ import annotations

import json
import os
import ssl
import ast
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .model_client import ModelClient, ModelRequestError
from .models import (
    AssistantMessage,
    AttachmentMessage,
    Message,
    SystemMessage,
    TokenUsage,
    ToolUseBlock,
    UserMessage,
    new_uuid,
    text_block,
)
from .tools import Tool


class OpenAICompatibleModelClient(ModelClient):
    """OpenAI-compatible chat completions adapter.

    Environment variables:
    - LLM_API_KEY
    - LLM_MODEL_ID
    - LLM_BASE_URL

    The adapter maps this project's internal Claude-style `tool_use` blocks to
    OpenAI-style `tool_calls`, then maps model `tool_calls` back to internal
    `ToolUseBlock` objects so QueryLoop can stay provider-agnostic.
    """

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str,
        timeout_seconds: float = 60.0,
    ):
        self.api_key = api_key
        self.model_id = model_id
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.chat_completions_url = normalize_chat_completions_url(base_url)
        self.ssl_context = create_ssl_context()

    @classmethod
    def from_env(
        cls,
        env_file: Optional[Path] = None,
        timeout_seconds: Optional[float] = None,
    ) -> "OpenAICompatibleModelClient":
        load_env_file(env_file)
        api_key = os.environ.get("LLM_API_KEY")
        model_id = os.environ.get("LLM_MODEL_ID")
        base_url = os.environ.get("LLM_BASE_URL")
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", api_key),
                ("LLM_MODEL_ID", model_id),
                ("LLM_BASE_URL", base_url),
            )
            if not value
        ]
        if missing:
            raise ValueError("Missing LLM environment variable(s): %s" % ", ".join(missing))

        if timeout_seconds is None:
            timeout_raw = os.environ.get("LLM_TIMEOUT_SECONDS")
            timeout_seconds = float(timeout_raw) if timeout_raw else 60.0

        return cls(
            api_key=str(api_key),
            model_id=str(model_id),
            base_url=str(base_url),
            timeout_seconds=timeout_seconds,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool],
        system_prompt: str,
    ):
        payload: Dict[str, Any] = {
            "model": self.model_id,
            "messages": to_openai_messages(messages, system_prompt),
        }
        if tools:
            payload["tools"] = [tool_to_openai_schema(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        data = self._post_chat_completion(payload)
        yield assistant_from_openai_response(data)

    def _post_chat_completion(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.chat_completions_url,
            data=body,
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ModelRequestError(
                "LLM request failed with HTTP %s: %s" % (exc.code, error_body),
                status_code=int(exc.code),
                response_body=error_body,
                prompt_too_long=is_prompt_too_long_error(int(exc.code), error_body),
            )
        except urllib.error.URLError as exc:
            raise ModelRequestError("LLM request failed: %s" % exc)

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ModelRequestError("LLM response was not valid JSON: %s" % exc)


def is_prompt_too_long_error(status_code: int, response_body: str) -> bool:
    if status_code not in (400, 413, 422):
        return False
    lowered = response_body.lower()
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "context window",
        "prompt is too long",
        "prompt too long",
        "input is too long",
        "input too long",
        "too many tokens",
    )
    return any(marker in lowered for marker in markers)


def create_ssl_context() -> ssl.SSLContext:
    if os.environ.get("LLM_SKIP_SSL_VERIFY") == "1":
        return ssl._create_unverified_context()

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def normalize_chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def load_env_file(env_file: Optional[Path] = None) -> Optional[Path]:
    path = find_env_file(env_file)
    if path is None:
        return None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return path


def find_env_file(env_file: Optional[Path] = None) -> Optional[Path]:
    if env_file is not None:
        path = Path(env_file)
        return path if path.exists() else None

    module_dir = Path(__file__).resolve().parent
    candidates: List[Path] = [
        Path.cwd() / ".env",
        module_dir / ".env",
        module_dir.parent / ".env",
    ]
    candidates.extend(parent / ".env" for parent in Path.cwd().resolve().parents)

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def tool_to_openai_schema(tool: Tool) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def to_openai_messages(messages: Sequence[Message], system_prompt: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for message in messages:
        if isinstance(message, AssistantMessage):
            result.append(assistant_to_openai_message(message))
        elif isinstance(message, UserMessage):
            result.extend(user_to_openai_messages(message))
        elif isinstance(message, AttachmentMessage):
            result.append({"role": "user", "content": repr(message.attachment)})
        elif isinstance(message, SystemMessage):
            continue
    return result


def assistant_to_openai_message(message: AssistantMessage) -> Dict[str, Any]:
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in message.content:
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_use = ToolUseBlock.from_block(block)
            tool_calls.append(
                {
                    "id": tool_use.id,
                    "type": "function",
                    "function": {
                        "name": tool_use.name,
                        "arguments": json.dumps(tool_use.input, ensure_ascii=False),
                    },
                }
            )

    openai_message: Dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(part for part in text_parts if part) or None,
    }
    if tool_calls:
        openai_message["tool_calls"] = tool_calls
    return openai_message


def user_to_openai_messages(message: UserMessage) -> List[Dict[str, Any]]:
    if isinstance(message.content, str):
        return [{"role": "user", "content": message.content}]

    result: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for block in message.content:
        if block.get("type") == "tool_result":
            if text_parts:
                result.append({"role": "user", "content": "\n".join(text_parts)})
                text_parts = []
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id")),
                    "content": str(block.get("content", "")),
                }
            )
        elif block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        else:
            text_parts.append(json.dumps(block, ensure_ascii=False))

    if text_parts:
        result.append({"role": "user", "content": "\n".join(text_parts)})
    return result


def assistant_from_openai_response(data: Dict[str, Any]) -> AssistantMessage:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response did not include choices")

    choice = choices[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    content_blocks = []

    content = message.get("content")
    if content:
        content_blocks.append(text_block(str(content)))

    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        content_blocks.append(
            ToolUseBlock(
                id=str(tool_call.get("id") or "toolu_" + new_uuid().replace("-", "")[:12]),
                name=str(function.get("name") or ""),
                input=parse_tool_arguments(function.get("arguments")),
            ).to_block()
        )

    if not content_blocks:
        content_blocks.append(text_block(""))

    usage_data = data.get("usage") or {}
    input_tokens = int(
        usage_data.get("prompt_tokens")
        or usage_data.get("input_tokens")
        or 0
    )
    output_tokens = int(
        usage_data.get("completion_tokens")
        or usage_data.get("output_tokens")
        or 0
    )
    total_tokens = int(
        usage_data.get("total_tokens")
        or input_tokens + output_tokens
    )

    return AssistantMessage(
        content=content_blocks,
        model=str(data.get("model") or "openai-compatible"),
        stop_reason="tool_use" if message.get("tool_calls") else finish_reason,
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        ) if usage_data else None,
    )


def parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if not isinstance(arguments, str):
        return {"_raw_arguments": arguments}

    repaired = parse_jsonish_object(arguments)
    if repaired is not None:
        return repaired
    return {"_raw_arguments": arguments}


def parse_jsonish_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = strip_json_fence(text.strip())
    if not cleaned:
        return {}

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        return None

    try:
        literal = ast.literal_eval(cleaned)
    except (ValueError, SyntaxError):
        literal = None
    if isinstance(literal, dict):
        return literal

    repaired_objects = decode_json_objects(cleaned)
    if not repaired_objects:
        return None

    merged: Dict[str, Any] = {}
    for item in repaired_objects:
        merged.update(item)
    return merged


def strip_json_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if not lines:
        return text
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def decode_json_objects(text: str) -> List[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] not in "{[":
            index += 1
        if index >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        index = max(end, index + 1)
    return objects
