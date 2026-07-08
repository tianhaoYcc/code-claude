from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union
from uuid import uuid4


ContentBlock = Dict[str, Any]
MessageContent = Union[str, List[ContentBlock]]


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def new_uuid() -> str:
    return str(uuid4())


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: Dict[str, Any]
    type: str = field(init=False, default="tool_use")

    def to_block(self) -> ContentBlock:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }

    @classmethod
    def from_block(cls, block: ContentBlock) -> "ToolUseBlock":
        return cls(
            id=str(block["id"]),
            name=str(block["name"]),
            input=dict(block.get("input") or {}),
        )


def text_block(text: str) -> ContentBlock:
    return {"type": "text", "text": text}


def tool_result_block(
    tool_use_id: str,
    content: str,
    is_error: bool = False,
) -> ContentBlock:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


@dataclass
class UserMessage:
    content: MessageContent
    is_meta: bool = False
    tool_use_result: Any = None
    uuid: str = field(default_factory=new_uuid)
    timestamp: str = field(default_factory=utc_now_iso)
    source_tool_assistant_uuid: Optional[str] = None
    type: str = field(init=False, default="user")

    def to_api_message(self) -> Dict[str, Any]:
        return {"role": "user", "content": self.content}

    def tool_result_ids(self) -> List[str]:
        if not isinstance(self.content, list):
            return []
        ids: List[str] = []
        for block in self.content:
            if block.get("type") == "tool_result":
                ids.append(str(block.get("tool_use_id")))
        return ids

    def to_record(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "uuid": self.uuid,
            "timestamp": self.timestamp,
            "is_meta": self.is_meta,
            "content": self.content,
            "tool_use_result": self.tool_use_result,
            "source_tool_assistant_uuid": self.source_tool_assistant_uuid,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "UserMessage":
        return cls(
            content=record.get("content", ""),
            is_meta=bool(record.get("is_meta", False)),
            tool_use_result=record.get("tool_use_result"),
            uuid=str(record.get("uuid") or new_uuid()),
            timestamp=str(record.get("timestamp") or utc_now_iso()),
            source_tool_assistant_uuid=record.get("source_tool_assistant_uuid"),
        )


@dataclass
class AssistantMessage:
    content: List[ContentBlock]
    uuid: str = field(default_factory=new_uuid)
    timestamp: str = field(default_factory=utc_now_iso)
    model: str = "mock"
    stop_reason: Optional[str] = None
    type: str = field(init=False, default="assistant")

    @classmethod
    def text(cls, text: str, **kwargs: Any) -> "AssistantMessage":
        return cls(content=[text_block(text)], **kwargs)

    @classmethod
    def tool_use(
        cls,
        name: str,
        input: Dict[str, Any],
        tool_use_id: Optional[str] = None,
        text: Optional[str] = None,
    ) -> "AssistantMessage":
        content: List[ContentBlock] = []
        if text:
            content.append(text_block(text))
        content.append(
            ToolUseBlock(
                id=tool_use_id or "toolu_" + new_uuid().replace("-", "")[:12],
                name=name,
                input=input,
            ).to_block()
        )
        return cls(content=content, stop_reason="tool_use")

    @classmethod
    def from_tool_uses(
        cls,
        tool_uses: Sequence[ToolUseBlock],
        text: Optional[str] = None,
    ) -> "AssistantMessage":
        content: List[ContentBlock] = []
        if text:
            content.append(text_block(text))
        content.extend(tool_use.to_block() for tool_use in tool_uses)
        return cls(content=content, stop_reason="tool_use")

    def text_content(self) -> str:
        parts = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(part for part in parts if part)

    def tool_uses(self) -> List[ToolUseBlock]:
        uses: List[ToolUseBlock] = []
        for block in self.content:
            if block.get("type") == "tool_use":
                uses.append(ToolUseBlock.from_block(block))
        return uses

    def to_api_message(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}

    def to_record(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "uuid": self.uuid,
            "timestamp": self.timestamp,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "content": self.content,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "AssistantMessage":
        return cls(
            content=list(record.get("content") or []),
            uuid=str(record.get("uuid") or new_uuid()),
            timestamp=str(record.get("timestamp") or utc_now_iso()),
            model=str(record.get("model") or "mock"),
            stop_reason=record.get("stop_reason"),
        )


@dataclass
class AttachmentMessage:
    attachment: Dict[str, Any]
    uuid: str = field(default_factory=new_uuid)
    timestamp: str = field(default_factory=utc_now_iso)
    type: str = field(init=False, default="attachment")

    def to_record(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "uuid": self.uuid,
            "timestamp": self.timestamp,
            "attachment": self.attachment,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "AttachmentMessage":
        return cls(
            attachment=dict(record.get("attachment") or {}),
            uuid=str(record.get("uuid") or new_uuid()),
            timestamp=str(record.get("timestamp") or utc_now_iso()),
        )


Message = Union[UserMessage, AssistantMessage, AttachmentMessage]


@dataclass
class RequestStartEvent:
    turn_count: int
    type: str = field(init=False, default="request_start")

    def to_record(self) -> Dict[str, Any]:
        return {"type": self.type, "turn_count": self.turn_count}


@dataclass
class ToolEvent:
    tool_use_id: str
    tool_name: str
    status: str
    message: str = ""
    type: str = field(init=False, default="tool_event")

    def to_record(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "message": self.message,
        }


@dataclass
class TerminalResult:
    reason: str
    turn_count: int
    is_error: bool = False
    message: str = ""
    type: str = field(init=False, default="terminal")

    def to_record(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "reason": self.reason,
            "turn_count": self.turn_count,
            "is_error": self.is_error,
            "message": self.message,
        }


AgentEvent = Union[RequestStartEvent, ToolEvent, TerminalResult, Message]


def message_from_record(record: Dict[str, Any]) -> Message:
    record_type = record.get("type")
    if record_type == "user":
        return UserMessage.from_record(record)
    if record_type == "assistant":
        return AssistantMessage.from_record(record)
    if record_type == "attachment":
        return AttachmentMessage.from_record(record)
    raise ValueError("Unsupported message record type: %r" % (record_type,))


def messages_to_api(messages: Iterable[Message]) -> List[Dict[str, Any]]:
    api_messages: List[Dict[str, Any]] = []
    for message in messages:
        if isinstance(message, AttachmentMessage):
            api_messages.append(
                {
                    "role": "user",
                    "content": "[attachment] " + repr(message.attachment),
                }
            )
        else:
            api_messages.append(message.to_api_message())
    return api_messages


def tool_result_ids_from_message(message: Message) -> List[str]:
    if isinstance(message, UserMessage):
        return message.tool_result_ids()
    return []


def ensure_tool_result_pairing(messages: Sequence[Message]) -> None:
    """Strictly validate assistant tool_use -> following user tool_result pairs.

    This intentionally does not repair invalid history. A resume loader should
    fail fast when the transcript violates the tool protocol.
    """
    for index, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        tool_use_ids = [tool_use.id for tool_use in message.tool_uses()]
        if not tool_use_ids:
            continue
        if index + 1 >= len(messages):
            raise ValueError(
                "Assistant message %s has tool_use blocks without a following "
                "user tool_result message" % message.uuid
            )
        next_message = messages[index + 1]
        if not isinstance(next_message, UserMessage):
            raise ValueError(
                "Assistant message %s with tool_use blocks must be followed by "
                "a user message" % message.uuid
            )
        result_ids = set(next_message.tool_result_ids())
        missing = [tool_use_id for tool_use_id in tool_use_ids if tool_use_id not in result_ids]
        if missing:
            raise ValueError(
                "Missing tool_result for tool_use id(s): %s" % ", ".join(missing)
            )
