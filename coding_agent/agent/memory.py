from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from .context_manager import estimate_text_tokens
from .models import (
    AssistantMessage,
    Message,
    SystemMessage,
    UserMessage,
    utc_now_iso,
)
from .tools import resolve_workspace_path


SESSION_MEMORY_HEADINGS = (
    "Current State",
    "Task Specification",
    "Files and Functions",
    "Workflow",
    "Errors and Corrections",
    "Key Discoveries",
    "Key Results",
    "Next Steps",
    "Worklog",
)

SESSION_MEMORY_TEMPLATE = "# Session Memory\n\n" + "\n\n".join(
    "## %s\n" % heading for heading in SESSION_MEMORY_HEADINGS
) + "\n"

MEMORY_INDEX_HEADER = "# Memory Index\n"
MEMORY_TYPES = {"user", "feedback", "project", "reference"}
INDEX_ENTRY_RE = re.compile(
    r"^- \[([^\]]+)\]\((topics/[A-Za-z0-9._-]+\.md)\):\s*(.+)$"
)
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|password|client[_-]?secret))\b"
    r"(\s*[:=]\s*)([\"']?)([^\s\"']+)([\"']?)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL,
)


class MemoryError(RuntimeError):
    pass


class MemoryStateError(MemoryError):
    pass


@dataclass
class MemoryConfig:
    enabled: bool = False
    root: Optional[Path] = None
    session_start_tokens: int = 10000
    session_update_tokens: int = 5000
    session_min_tool_calls: int = 3
    worker_max_turns: int = 5
    flush_timeout_seconds: float = 15.0
    summary_max_tokens: int = 12000
    summary_section_max_tokens: int = 2000
    index_max_lines: int = 200
    index_entry_max_chars: int = 180
    topic_max_files: int = 200
    recall_max_topics: int = 3
    recall_max_tokens: int = 8000
    session_compact_min_tokens: int = 10000
    session_compact_min_text_groups: int = 5
    session_compact_max_tokens: int = 40000

    def __post_init__(self) -> None:
        if self.root is not None:
            self.root = Path(self.root)
        integer_fields = (
            "session_start_tokens",
            "session_update_tokens",
            "session_min_tool_calls",
            "worker_max_turns",
            "summary_max_tokens",
            "summary_section_max_tokens",
            "index_max_lines",
            "index_entry_max_chars",
            "topic_max_files",
            "recall_max_topics",
            "recall_max_tokens",
            "session_compact_min_tokens",
            "session_compact_min_text_groups",
            "session_compact_max_tokens",
        )
        for name in integer_fields:
            if int(getattr(self, name)) < 0:
                raise ValueError("%s cannot be negative" % name)
        if self.worker_max_turns == 0:
            raise ValueError("worker_max_turns must be greater than zero")
        if self.flush_timeout_seconds <= 0:
            raise ValueError("flush_timeout_seconds must be greater than zero")
        if self.session_compact_max_tokens < self.session_compact_min_tokens:
            raise ValueError(
                "session_compact_max_tokens cannot be smaller than "
                "session_compact_min_tokens"
            )


@dataclass
class MemoryState:
    session_id: str
    schema_version: int = 1
    last_session_summary_message_uuid: Optional[str] = None
    last_durable_memory_message_uuid: Optional[str] = None
    last_summary_token_count: int = 0
    tool_calls_since_summary: int = 0
    updated_at: str = field(default_factory=utc_now_iso)

    def to_record(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "last_session_summary_message_uuid": (
                self.last_session_summary_message_uuid
            ),
            "last_durable_memory_message_uuid": (
                self.last_durable_memory_message_uuid
            ),
            "last_summary_token_count": self.last_summary_token_count,
            "tool_calls_since_summary": self.tool_calls_since_summary,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any], session_id: str) -> "MemoryState":
        if not isinstance(record, dict):
            raise MemoryStateError("Memory state must be a JSON object")
        try:
            version = int(record.get("schema_version") or 0)
            last_summary_token_count = max(
                0, int(record.get("last_summary_token_count") or 0)
            )
            tool_calls_since_summary = max(
                0, int(record.get("tool_calls_since_summary") or 0)
            )
        except (TypeError, ValueError) as exc:
            raise MemoryStateError("Memory state contains invalid numbers: %s" % exc)
        if version != 1:
            raise MemoryStateError("Unsupported memory state version: %s" % version)
        stored_session = str(record.get("session_id") or "")
        if stored_session and stored_session != session_id:
            raise MemoryStateError(
                "Memory state belongs to session %s, expected %s"
                % (stored_session, session_id)
            )
        return cls(
            session_id=session_id,
            schema_version=version,
            last_session_summary_message_uuid=_optional_string(
                record.get("last_session_summary_message_uuid")
            ),
            last_durable_memory_message_uuid=_optional_string(
                record.get("last_durable_memory_message_uuid")
            ),
            last_summary_token_count=last_summary_token_count,
            tool_calls_since_summary=tool_calls_since_summary,
            updated_at=str(record.get("updated_at") or utc_now_iso()),
        )


@dataclass(frozen=True)
class MemoryCheckpoint:
    summary: str
    message_uuid: str
    transcript_path: Optional[Path]


@dataclass(frozen=True)
class MemoryIndexEntry:
    title: str
    relative_path: str
    description: str


@dataclass(frozen=True)
class MemoryTopic:
    entry: MemoryIndexEntry
    path: Path
    memory_type: str
    keywords: Tuple[str, ...]
    updated_at: str
    source_session: str
    content: str


class MemoryStore:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        config: MemoryConfig,
        transcript_path: Optional[Path] = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        configured_root = config.root or Path(".agent_memory")
        self.root = resolve_workspace_path(self.workspace_root, str(configured_root))
        self.session_id = _safe_session_id(session_id)
        self.config = config
        self.transcript_path = (
            Path(transcript_path).resolve() if transcript_path is not None else None
        )
        self.index_path = self.root / "MEMORY.md"
        self.topics_dir = self.root / "topics"
        self.session_dir = self.root / "sessions" / self.session_id
        self.summary_path = self.session_dir / "summary.md"
        self.state_path = self.session_dir / "state.json"

    def ensure_layout(self) -> None:
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.write_text_atomic(self.index_path, MEMORY_INDEX_HEADER)

    def ensure_summary_template(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if not self.summary_path.exists():
            self.write_text_atomic(self.summary_path, SESSION_MEMORY_TEMPLATE)

    def load_state(self) -> MemoryState:
        if not self.state_path.exists():
            return MemoryState(session_id=self.session_id)
        try:
            record = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryStateError("Invalid memory state: %s" % exc)
        return MemoryState.from_record(record, self.session_id)

    def save_state(self, state: MemoryState) -> None:
        state.updated_at = utc_now_iso()
        payload = json.dumps(
            state.to_record(), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        self.write_text_atomic(self.state_path, payload)

    def read_summary(self) -> Optional[str]:
        if not self.summary_path.exists():
            return None
        return self.summary_path.read_text(encoding="utf-8", errors="replace")

    def write_summary(self, text: str) -> None:
        sanitized = sanitize_memory_text(text)
        self.validate_summary(sanitized)
        self.write_text_atomic(self.summary_path, sanitized.rstrip() + "\n")

    def validate_summary(self, text: str) -> None:
        if not text or text.strip() == SESSION_MEMORY_TEMPLATE.strip():
            raise MemoryError("Session memory is empty")
        missing = [
            heading
            for heading in SESSION_MEMORY_HEADINGS
            if "## %s" % heading not in text
        ]
        if missing:
            raise MemoryError(
                "Session memory is missing section(s): %s" % ", ".join(missing)
            )
        if estimate_text_tokens(text) > self.config.summary_max_tokens:
            raise MemoryError(
                "Session memory exceeds %d tokens" % self.config.summary_max_tokens
            )
        if sanitize_memory_text(text) != text:
            raise MemoryError("Session memory contains sensitive data")

    def checkpoint(self) -> Optional[MemoryCheckpoint]:
        state = self.load_state()
        summary = self.read_summary()
        if not summary or not state.last_session_summary_message_uuid:
            return None
        self.validate_summary(summary)
        return MemoryCheckpoint(
            summary=self.summary_for_compaction(summary),
            message_uuid=state.last_session_summary_message_uuid,
            transcript_path=self.transcript_path,
        )

    def summary_for_compaction(self, text: str) -> str:
        sections = _split_markdown_sections(text)
        compacted: List[str] = []
        for heading, body in sections:
            if heading is None:
                compacted.append(body.rstrip())
                continue
            limited = truncate_to_token_budget(
                body.strip(),
                self.config.summary_section_max_tokens,
                "\n[Section truncated; read the full session memory for details.]",
            )
            compacted.append("## %s\n%s" % (heading, limited))
        result = "\n\n".join(part for part in compacted if part.strip()).strip()
        return truncate_to_token_budget(
            result,
            self.config.summary_max_tokens,
            "\n[Session memory truncated; read the full file for details.]",
        )

    def read_index_lines(self) -> List[str]:
        if not self.index_path.exists():
            return []
        return self.index_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[: self.config.index_max_lines]

    def parse_index(
        self, lines: Optional[Sequence[str]] = None
    ) -> List[MemoryIndexEntry]:
        entries: List[MemoryIndexEntry] = []
        for line in lines if lines is not None else self.read_index_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = INDEX_ENTRY_RE.match(stripped)
            if not match:
                raise MemoryError("Invalid MEMORY.md index entry: %s" % stripped)
            entries.append(
                MemoryIndexEntry(
                    title=match.group(1).strip(),
                    relative_path=match.group(2),
                    description=match.group(3).strip(),
                )
            )
        return entries

    def read_topic(self, entry: MemoryIndexEntry) -> MemoryTopic:
        path = resolve_workspace_path(self.root, entry.relative_path)
        if path.parent != self.topics_dir.resolve():
            raise MemoryError("Memory topic must be directly under topics/: %s" % path)
        if not path.exists() or not path.is_file():
            raise MemoryError("Memory topic does not exist: %s" % path)
        content = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = parse_frontmatter(content)
        memory_type = metadata.get("type", "")
        if memory_type not in MEMORY_TYPES:
            raise MemoryError("Invalid memory type in %s: %s" % (path, memory_type))
        keywords = tuple(
            item.strip() for item in metadata.get("keywords", "").split(",")
            if item.strip()
        )
        if not body.strip():
            raise MemoryError("Memory topic is empty: %s" % path)
        return MemoryTopic(
            entry=entry,
            path=path,
            memory_type=memory_type,
            keywords=keywords,
            updated_at=metadata.get("updated_at", ""),
            source_session=metadata.get("source_session", ""),
            content=content,
        )

    def validate_persistent_memory(self) -> None:
        if not self.index_path.exists():
            raise MemoryError("MEMORY.md does not exist")
        index_text = self.index_path.read_text(encoding="utf-8", errors="replace")
        lines = index_text.splitlines()
        if len(lines) > self.config.index_max_lines:
            raise MemoryError("MEMORY.md exceeds the index line limit")
        if sanitize_memory_text(index_text) != index_text:
            raise MemoryError("MEMORY.md contains sensitive data")
        entries = self.parse_index(lines)
        if len(entries) > self.config.topic_max_files:
            raise MemoryError("MEMORY.md exceeds the topic limit")
        seen: Set[str] = set()
        for entry in entries:
            if entry.relative_path in seen:
                raise MemoryError("Duplicate memory topic: %s" % entry.relative_path)
            seen.add(entry.relative_path)
            raw_line = "- [%s](%s): %s" % (
                entry.title,
                entry.relative_path,
                entry.description,
            )
            if len(raw_line) > self.config.index_entry_max_chars:
                raise MemoryError("Memory index entry is too long: %s" % raw_line)
            topic = self.read_topic(entry)
            if sanitize_memory_text(topic.content) != topic.content:
                raise MemoryError("Memory topic contains sensitive data: %s" % topic.path)
        topic_files = list(self.topics_dir.glob("*.md")) if self.topics_dir.exists() else []
        if len(topic_files) > self.config.topic_max_files:
            raise MemoryError("Too many memory topic files")
        linked = {entry.relative_path for entry in entries}
        for path in topic_files:
            rel = path.relative_to(self.root).as_posix()
            if rel not in linked:
                raise MemoryError("Unindexed memory topic: %s" % rel)

    def persistent_snapshot(self) -> Dict[str, str]:
        snapshot: Dict[str, str] = {}
        if self.index_path.exists():
            snapshot["MEMORY.md"] = self.index_path.read_text(
                encoding="utf-8", errors="replace"
            )
        if self.topics_dir.exists():
            for path in sorted(self.topics_dir.glob("*.md")):
                snapshot[path.relative_to(self.root).as_posix()] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
        return snapshot

    def restore_persistent_snapshot(self, snapshot: Dict[str, str]) -> None:
        current = self.persistent_snapshot()
        for relative_path in current:
            if relative_path not in snapshot:
                path = resolve_workspace_path(self.root, relative_path)
                path.unlink(missing_ok=True)
        for relative_path, content in snapshot.items():
            self.write_text_atomic(
                resolve_workspace_path(self.root, relative_path), content
            )

    @staticmethod
    def write_text_atomic(path: Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(".%s.%s.tmp" % (path.name, uuid4().hex))
        try:
            temp_path.write_text(text, encoding="utf-8")
            os.replace(str(temp_path), str(path))
        finally:
            if temp_path.exists():
                temp_path.unlink()


class MemoryRetriever:
    def __init__(self, store: MemoryStore, config: MemoryConfig):
        self.store = store
        self.config = config

    def recall(self, prompt: str) -> str:
        if self.config.recall_max_tokens <= 0:
            return ""
        index_lines = self.store.read_index_lines()
        if not index_lines:
            return ""
        entries = self.store.parse_index()
        scored: List[Tuple[int, str, MemoryTopic]] = []
        for entry in entries:
            topic = self.store.read_topic(entry)
            score = _topic_score(prompt, topic)
            if score > 0:
                scored.append((score, topic.updated_at, topic))
        scored.sort(key=lambda item: (item[0], item[1], item[2].path.name), reverse=True)

        parts = [
            "<project_memory>",
            "MEMORY.md is an index of durable project memory.",
            "[MEMORY.md]",
            sanitize_memory_text("\n".join(index_lines)),
        ]
        for _, _, topic in scored[: self.config.recall_max_topics]:
            candidate = parts + [
                "[topic path=%s]" % topic.entry.relative_path,
                sanitize_memory_text(topic.content),
            ]
            if estimate_text_tokens("\n\n".join(candidate) + "\n</project_memory>") > (
                self.config.recall_max_tokens
            ):
                remaining = max(
                    0,
                    self.config.recall_max_tokens
                    - estimate_text_tokens("\n\n".join(parts))
                    - 32,
                )
                if remaining:
                    parts.extend(
                        [
                            "[topic path=%s]" % topic.entry.relative_path,
                            truncate_to_token_budget(
                                sanitize_memory_text(topic.content),
                                remaining,
                                "\n[Topic truncated.]",
                            ),
                        ]
                    )
                break
            parts = candidate
        closing = "\n\n</project_memory>"
        body_budget = max(
            0,
            self.config.recall_max_tokens - estimate_text_tokens(closing),
        )
        body = truncate_to_token_budget(
            "\n\n".join(parts),
            body_budget,
            "\n[Project memory truncated.]",
        )
        return body + closing


def serialize_messages_for_memory(messages: Sequence[Message]) -> str:
    records: List[Dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, UserMessage):
            content: Any = message.content
            if isinstance(content, list):
                cleaned_blocks: List[Dict[str, Any]] = []
                for block in content:
                    cleaned = dict(block)
                    if cleaned.get("type") == "tool_result":
                        cleaned["content"] = _truncate_chars(
                            str(cleaned.get("content", "")), 6000
                        )
                    cleaned_blocks.append(cleaned)
                content = cleaned_blocks
            records.append(
                {
                    "uuid": message.uuid,
                    "role": "user",
                    "content": content,
                    "is_compact_summary": message.is_compact_summary,
                }
            )
        elif isinstance(message, AssistantMessage):
            records.append(
                {
                    "uuid": message.uuid,
                    "role": "assistant",
                    "content": message.content,
                }
            )
        else:
            records.append({"uuid": message.uuid, "type": message.type})
    raw = json.dumps(records, ensure_ascii=False, indent=2)
    return sanitize_memory_text(raw)


def messages_after_cursor(
    messages: Sequence[Message], cursor_uuid: Optional[str]
) -> List[Message]:
    if not cursor_uuid:
        return [message for message in messages if not isinstance(message, SystemMessage)]
    index = next(
        (
            idx
            for idx, message in enumerate(messages)
            if message.uuid == cursor_uuid
        ),
        -1,
    )
    if index < 0:
        raise MemoryStateError("Memory cursor is not present in the transcript")
    return [
        message
        for message in messages[index + 1 :]
        if not isinstance(message, SystemMessage)
    ]


def last_memory_message_uuid(messages: Sequence[Message]) -> Optional[str]:
    for message in reversed(messages):
        if not isinstance(message, SystemMessage):
            return message.uuid
    return None


def parse_frontmatter(content: str) -> Tuple[Dict[str, str], str]:
    match = FRONTMATTER_RE.match(content.replace("\r\n", "\n"))
    if not match:
        raise MemoryError("Memory topic is missing frontmatter")
    metadata: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise MemoryError("Invalid memory topic frontmatter: %s" % line)
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    required = {"type", "keywords", "updated_at", "source_session"}
    missing = sorted(required - set(metadata))
    if missing:
        raise MemoryError(
            "Memory topic frontmatter is missing: %s" % ", ".join(missing)
        )
    empty = sorted(key for key in required if not metadata.get(key, "").strip())
    if empty:
        raise MemoryError(
            "Memory topic frontmatter is empty: %s" % ", ".join(empty)
        )
    return metadata, match.group(2)


def sanitize_memory_text(
    text: str, secret_values: Optional[Iterable[str]] = None
) -> str:
    sanitized = str(text)
    values = list(secret_values or known_secret_values())
    for value in values:
        if len(value) >= 6:
            sanitized = sanitized.replace(value, "[REDACTED]")

    def replace_assignment(match: re.Match) -> str:
        return "%s%s[REDACTED]" % (match.group(1), match.group(2))

    sanitized = SECRET_ASSIGNMENT_RE.sub(replace_assignment, sanitized)
    sanitized = BEARER_RE.sub("Bearer [REDACTED]", sanitized)
    sanitized = PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", sanitized)
    return sanitized


def known_secret_values() -> Tuple[str, ...]:
    names = (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "API_KEY",
        "ACCESS_TOKEN",
        "AUTH_TOKEN",
        "PASSWORD",
        "CLIENT_SECRET",
    )
    return tuple(
        value for value in (os.environ.get(name) for name in names) if value
    )


def truncate_to_token_budget(text: str, budget: int, marker: str = "") -> str:
    if budget <= 0:
        return ""
    if estimate_text_tokens(text) <= budget:
        return text
    marker_tokens = estimate_text_tokens(marker)
    content_budget = max(0, budget - marker_tokens)
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= content_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + marker


def _split_markdown_sections(text: str) -> List[Tuple[Optional[str], str]]:
    sections: List[Tuple[Optional[str], str]] = []
    heading: Optional[str] = None
    lines: List[str] = []
    for line in text.replace("\r\n", "\n").splitlines():
        if line.startswith("## "):
            sections.append((heading, "\n".join(lines)))
            heading = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    sections.append((heading, "\n".join(lines)))
    return sections


def _topic_score(prompt: str, topic: MemoryTopic) -> int:
    normalized_prompt = prompt.casefold()
    score = 0
    for keyword in topic.keywords:
        if keyword.casefold() in normalized_prompt:
            score += 3
    prompt_terms = _lexical_terms(prompt)
    topic_terms = _lexical_terms(
        "%s %s %s" % (
            topic.entry.title,
            topic.entry.description,
            topic.path.stem,
        )
    )
    score += len(prompt_terms.intersection(topic_terms))
    return score


def _lexical_terms(text: str) -> Set[str]:
    terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_]+", text)
        if len(token) > 1
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _safe_session_id(session_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id).strip())
    if not normalized or normalized in {".", ".."}:
        raise ValueError("session_id must contain a safe path component")
    return normalized


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[Tool result truncated for memory extraction.]"
