from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .models import (
    AgentEvent,
    Message,
    TerminalResult,
    ensure_tool_result_pairing,
    message_from_record,
)


class Transcript:
    """Append-only JSONL transcript."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append_event(self, event: AgentEvent) -> None:
        if isinstance(event, TerminalResult):
            self._append({"record_type": "terminal", "terminal": event.to_record()})
            return
        if hasattr(event, "to_record"):
            record = event.to_record()
            if record.get("type") in {"user", "assistant", "attachment", "system"}:
                self._append({"record_type": "message", "message": record})
            else:
                self._append({"record_type": "event", "event": record})

    def append_message(self, message: Message) -> None:
        self._append({"record_type": "message", "message": message.to_record()})

    def append_terminal(self, terminal: TerminalResult) -> None:
        self._append({"record_type": "terminal", "terminal": terminal.to_record()})

    def load_messages(self, strict: bool = True) -> List[Message]:
        messages: List[Message] = []
        if not self.path.exists():
            return messages
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError("Invalid JSONL at line %d: %s" % (line_no, exc))
                if entry.get("record_type") == "message":
                    messages.append(message_from_record(entry["message"]))
        if strict:
            ensure_tool_result_pairing(messages)
        return messages

    def _append(self, entry: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
