from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.models import AssistantMessage, UserMessage, tool_result_block
from agent.openai_model import (
    OpenAICompatibleModelClient,
    assistant_from_openai_response,
    normalize_chat_completions_url,
    parse_tool_arguments,
    to_openai_messages,
    tool_to_openai_schema,
)
from agent.tools import ReadFileTool


class FakeOpenAIClient(OpenAICompatibleModelClient):
    def __init__(self, response):
        super().__init__("key", "model", "https://example.com/v1")
        self.response = response
        self.payload = None

    def _post_chat_completion(self, payload):
        self.payload = payload
        return self.response


async def collect_one(client, messages, tools):
    result = []
    async for item in client.stream(messages, tools, "system prompt"):
        result.append(item)
    return result[0]


class OpenAIModelTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_chat_completions_url(self):
        self.assertEqual(
            normalize_chat_completions_url("https://example.com"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            normalize_chat_completions_url("https://example.com/v1"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            normalize_chat_completions_url("https://example.com/v1/chat/completions"),
            "https://example.com/v1/chat/completions",
        )

    def test_tool_schema_maps_to_openai_function(self):
        schema = tool_to_openai_schema(ReadFileTool())

        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "read_file")
        self.assertIn("file_path", schema["function"]["parameters"]["required"])

    def test_internal_tool_use_history_maps_to_openai_messages(self):
        assistant = AssistantMessage.tool_use(
            "read_file",
            {"file_path": "README.md"},
            tool_use_id="call_1",
            text="Reading.",
        )
        result = UserMessage(
            content=[tool_result_block("call_1", "file content")],
            is_meta=True,
        )

        messages = to_openai_messages([UserMessage("hi"), assistant, result], "sys")

        self.assertEqual(messages[0], {"role": "system", "content": "sys"})
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(messages[3]["role"], "tool")
        self.assertEqual(messages[3]["tool_call_id"], "call_1")

    def test_openai_tool_calls_map_to_internal_tool_use(self):
        data = {
            "model": "fake-model",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Reading.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"file_path":"README.md"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }

        assistant = assistant_from_openai_response(data)
        tool_use = assistant.tool_uses()[0]

        self.assertEqual(assistant.model, "fake-model")
        self.assertEqual(tool_use.id, "call_1")
        self.assertEqual(tool_use.name, "read_file")
        self.assertEqual(tool_use.input, {"file_path": "README.md"})

    def test_malformed_tool_arguments_are_repaired_when_possible(self):
        self.assertEqual(parse_tool_arguments('{}{"path": "."}'), {"path": "."})
        self.assertEqual(
            parse_tool_arguments('```json\n{"file_path": "README.md"}\n```'),
            {"file_path": "README.md"},
        )
        self.assertEqual(
            parse_tool_arguments("not json"),
            {"_raw_arguments": "not json"},
        )

    async def test_client_sends_tools_and_returns_internal_assistant(self):
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"file_path":"README.md"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        client = FakeOpenAIClient(response)

        assistant = await collect_one(client, [UserMessage("read file")], [ReadFileTool()])

        self.assertEqual(client.payload["model"], "model")
        self.assertEqual(client.payload["tools"][0]["function"]["name"], "read_file")
        self.assertEqual(assistant.tool_uses()[0].name, "read_file")


if __name__ == "__main__":
    unittest.main()
