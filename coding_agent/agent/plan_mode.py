from __future__ import annotations

import inspect
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Union
from uuid import uuid4

from .context_manager import estimate_text_tokens
from .memory import sanitize_memory_text
from .models import PlanEvent, utc_now_iso
from .tools import (
    PermissionRequest,
    Tool,
    ToolError,
    ToolResult,
    resolve_workspace_path,
)


ENTER_PLAN_MODE_TOOL_NAME = "enter_plan_mode"
EXIT_PLAN_MODE_TOOL_NAME = "exit_plan_mode"
AGENT_TOOL_NAME = "agent"
PLAN_MODES = {"execute", "plan"}


class PlanError(RuntimeError):
    pass


class PlanStateError(PlanError):
    pass


@dataclass(frozen=True)
class PlanApprovalRequest:
    kind: str
    session_id: str
    plan_path: Path
    plan_content: str = ""


@dataclass(frozen=True)
class PlanApprovalDecision:
    approved: bool
    feedback: str = ""


PlanApprovalValue = Union[
    PlanApprovalDecision,
    bool,
    Awaitable[Union[PlanApprovalDecision, bool]],
]
PlanApprovalCallback = Callable[[PlanApprovalRequest], PlanApprovalValue]


@dataclass
class PlanConfig:
    enabled: bool = False
    root: Optional[Path] = None
    initial_mode: str = "execute"
    max_plan_tokens: int = 12000
    approval_callback: Optional[PlanApprovalCallback] = None

    def __post_init__(self) -> None:
        if self.root is not None:
            self.root = Path(self.root)
        if self.initial_mode not in PLAN_MODES:
            raise ValueError("initial_mode must be execute or plan")
        if self.max_plan_tokens <= 0:
            raise ValueError("max_plan_tokens must be greater than zero")


@dataclass
class PlanState:
    session_id: str
    schema_version: int = 1
    mode: str = "execute"
    plan_version: int = 0
    approved_version: int = 0
    active_plan: bool = False
    source_message_uuid: Optional[str] = None
    updated_at: str = field(default_factory=utc_now_iso)

    def to_record(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "mode": self.mode,
            "plan_version": self.plan_version,
            "approved_version": self.approved_version,
            "active_plan": self.active_plan,
            "source_message_uuid": self.source_message_uuid,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, record: Dict[str, object], session_id: str) -> "PlanState":
        if not isinstance(record, dict):
            raise PlanStateError("Plan state must be a JSON object")
        try:
            version = int(record.get("schema_version") or 0)
            plan_version = max(0, int(record.get("plan_version") or 0))
            approved_version = max(0, int(record.get("approved_version") or 0))
        except (TypeError, ValueError) as exc:
            raise PlanStateError("Plan state contains invalid numbers: %s" % exc)
        if version != 1:
            raise PlanStateError("Unsupported plan state version: %s" % version)
        stored_session = str(record.get("session_id") or "")
        if stored_session and stored_session != session_id:
            raise PlanStateError(
                "Plan state belongs to session %s, expected %s"
                % (stored_session, session_id)
            )
        mode = str(record.get("mode") or "")
        if mode not in PLAN_MODES:
            raise PlanStateError("Invalid plan mode: %s" % mode)
        active_plan = bool(record.get("active_plan", False))
        if approved_version > plan_version:
            raise PlanStateError("approved_version cannot exceed plan_version")
        if active_plan and (
            mode != "execute"
            or approved_version == 0
            or approved_version != plan_version
        ):
            raise PlanStateError("Active plan state is internally inconsistent")
        source = record.get("source_message_uuid")
        return cls(
            session_id=session_id,
            schema_version=version,
            mode=mode,
            plan_version=plan_version,
            approved_version=approved_version,
            active_plan=active_plan,
            source_message_uuid=str(source) if source else None,
            updated_at=str(record.get("updated_at") or utc_now_iso()),
        )


class PlanStore:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        config: PlanConfig,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        configured_root = config.root or Path(".agent_plans")
        self.root = resolve_workspace_path(self.workspace_root, str(configured_root))
        self.session_id = _safe_session_id(session_id)
        self.session_dir = self.root / self.session_id
        self.plan_path = self.session_dir / "plan.md"
        self.state_path = self.session_dir / "state.json"
        self.config = config

    def ensure_layout(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> PlanState:
        if not self.state_path.exists():
            return PlanState(session_id=self.session_id)
        try:
            record = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanStateError("Invalid plan state: %s" % exc)
        return PlanState.from_record(record, self.session_id)

    def save_state(self, state: PlanState) -> None:
        state.updated_at = utc_now_iso()
        payload = json.dumps(
            state.to_record(), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        self.write_text_atomic(self.state_path, payload)

    def read_plan(self) -> str:
        if not self.plan_path.exists():
            return ""
        return self.plan_path.read_text(encoding="utf-8", errors="replace")

    def clear_plan(self) -> None:
        if self.plan_path.exists():
            self.write_text_atomic(self.plan_path, "")

    def validated_plan(self) -> str:
        raw = self.read_plan()
        sanitized = sanitize_memory_text(raw).strip()
        if not sanitized:
            raise PlanError("Plan file is empty: %s" % self.plan_path)
        if estimate_text_tokens(sanitized) > self.config.max_plan_tokens:
            raise PlanError(
                "Plan exceeds max_plan_tokens=%d" % self.config.max_plan_tokens
            )
        normalized = sanitized + "\n"
        if normalized != raw:
            self.write_text_atomic(self.plan_path, normalized)
        return sanitized

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


class PlanManager:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        config: PlanConfig,
    ):
        self.config = config
        self.store = PlanStore(workspace_root, session_id, config)
        self.store.ensure_layout()
        self.events: List[PlanEvent] = []
        self._last_user_message_uuid: Optional[str] = None
        state_existed = self.store.state_path.exists()
        try:
            self.state = self.store.load_state()
        except PlanStateError as exc:
            self.state = PlanState(
                session_id=self.store.session_id,
                mode="plan",
                plan_version=1,
            )
            self._emit("failed", "Plan state was reset in fail-closed mode: %s" % exc)
            self.store.save_state(self.state)

        if self.state.active_plan:
            try:
                self.store.validated_plan()
            except PlanError as exc:
                self.state.mode = "plan"
                self.state.active_plan = False
                self.state.approved_version = 0
                self._emit("failed", "Approved plan was invalidated: %s" % exc)
                self.store.save_state(self.state)

        if config.initial_mode == "plan" and (
            not state_existed or self.state.mode != "plan"
        ):
            self.force_enter(source_message_uuid=None)

    @property
    def mode(self) -> str:
        return self.state.mode

    @property
    def is_planning(self) -> bool:
        return self.state.mode == "plan"

    def note_user_message(self, message_uuid: str) -> None:
        self._last_user_message_uuid = message_uuid
        if self.is_planning and self.state.source_message_uuid is None:
            self.state.source_message_uuid = message_uuid
            self.store.save_state(self.state)

    def force_enter(self, source_message_uuid: Optional[str]) -> None:
        if self.is_planning:
            if source_message_uuid and not self.state.source_message_uuid:
                self.state.source_message_uuid = source_message_uuid
                self.store.save_state(self.state)
            return
        self.store.clear_plan()
        self.state.mode = "plan"
        self.state.plan_version += 1
        self.state.approved_version = 0
        self.state.active_plan = False
        self.state.source_message_uuid = source_message_uuid
        self.store.save_state(self.state)
        self._emit("entered", "Entered plan mode")

    async def request_enter(self) -> PlanApprovalDecision:
        if self.is_planning:
            return PlanApprovalDecision(False, "Agent is already in plan mode")
        self._emit("approval_requested", "Approval requested to enter plan mode")
        decision = await self._request_approval(
            PlanApprovalRequest(
                kind="enter",
                session_id=self.store.session_id,
                plan_path=self.store.plan_path,
            )
        )
        if decision.approved:
            self.force_enter(self._last_user_message_uuid)
        else:
            self._emit("rejected", decision.feedback or "Plan mode entry rejected")
        return decision

    async def request_exit(self) -> PlanApprovalDecision:
        if not self.is_planning:
            return PlanApprovalDecision(False, "Agent is not in plan mode")
        try:
            plan = self.store.validated_plan()
        except PlanError as exc:
            self._emit("failed", str(exc))
            return PlanApprovalDecision(False, str(exc))
        self._emit("approval_requested", "Plan approval requested")
        decision = await self._request_approval(
            PlanApprovalRequest(
                kind="exit",
                session_id=self.store.session_id,
                plan_path=self.store.plan_path,
                plan_content=plan,
            )
        )
        if decision.approved:
            self.state.mode = "execute"
            self.state.approved_version = self.state.plan_version
            self.state.active_plan = True
            self.store.save_state(self.state)
            self._emit("approved", "Plan approved; execution mode restored")
        else:
            self._emit("rejected", decision.feedback or "Plan rejected")
        return decision

    def mark_completed(self) -> None:
        if self.is_planning or not self.state.active_plan:
            return
        self.state.active_plan = False
        self.store.save_state(self.state)
        self._emit("completed", "Approved plan execution completed")

    def effective_prompt(self) -> str:
        if self.is_planning:
            return (
                "<plan_mode>\n"
                "You are in plan mode. Explore the codebase and create a concrete "
                "implementation plan. Do not modify project files or run shell "
                "commands. The only writable file is %s. You may call explore or "
                "plan subagents. When the plan is complete, call %s for user "
                "approval; do not finish with a normal answer.\n"
                "</plan_mode>"
                % (self.store.plan_path, EXIT_PLAN_MODE_TOOL_NAME)
            )
        if self.state.active_plan:
            try:
                plan = self.store.validated_plan()
            except PlanError as exc:
                self.state.mode = "plan"
                self.state.active_plan = False
                self.state.approved_version = 0
                self.store.save_state(self.state)
                self._emit("failed", "Approved plan became unavailable: %s" % exc)
                return self.effective_prompt()
            return (
                "<approved_plan version=\"%d\" path=\"%s\">\n%s\n"
                "</approved_plan>\nImplement this approved plan."
                % (self.state.approved_version, self.store.plan_path, plan)
            )
        return ""

    def subagent_context(self) -> str:
        plan = self.store.read_plan().strip()
        if not plan:
            return ""
        return (
            "<parent_plan mode=\"%s\" version=\"%d\">\n%s\n</parent_plan>"
            % (self.mode, self.state.plan_version, sanitize_memory_text(plan))
        )

    def boundary_metadata(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "plan_path": str(self.store.plan_path),
            "plan_version": self.state.plan_version,
            "approved_version": self.state.approved_version,
            "active_plan": self.state.active_plan,
        }

    def filter_tools(
        self,
        enabled_tools: Sequence[Tool],
        normally_available: Sequence[Tool],
    ) -> List[Tool]:
        if not self.is_planning:
            return [
                tool
                for tool in normally_available
                if tool.name != EXIT_PLAN_MODE_TOOL_NAME
            ]
        normal_names = {tool.name for tool in normally_available}
        selected: List[Tool] = []
        for tool in enabled_tools:
            if tool.permission_action == "read" and tool.name in normal_names:
                selected.append(tool)
            elif tool.name in {
                "write_file",
                "edit_file",
                AGENT_TOOL_NAME,
                EXIT_PLAN_MODE_TOOL_NAME,
            }:
                selected.append(tool)
        return selected

    def permission_override(self, request: PermissionRequest) -> Optional[bool]:
        if request.action == "write" and request.path is not None:
            resolved = request.path.resolve()
            try:
                resolved.relative_to(self.store.root.resolve())
                is_plan_storage = True
            except ValueError:
                is_plan_storage = False
            if is_plan_storage:
                return bool(
                    self.is_planning
                    and request.tool_name in {"write_file", "edit_file"}
                    and resolved == self.store.plan_path.resolve()
                )
        if not self.is_planning:
            return None
        if request.action == "shell":
            return False
        if request.action != "write":
            return None
        if request.path is None or request.tool_name not in {"write_file", "edit_file"}:
            return False
        return request.path.resolve() == self.store.plan_path.resolve()

    def filter_write_content(self, path: Path, content: str) -> str:
        if not self.is_planning or path.resolve() != self.store.plan_path.resolve():
            return content
        sanitized = sanitize_memory_text(content)
        if not sanitized.strip():
            raise ToolError("Plan file must not be empty")
        token_count = estimate_text_tokens(sanitized)
        if token_count > self.config.max_plan_tokens:
            raise ToolError(
                "Plan exceeds max_plan_tokens=%d (estimated=%d)"
                % (self.config.max_plan_tokens, token_count)
            )
        return sanitized

    def drain_events(self) -> List[PlanEvent]:
        events = list(self.events)
        self.events.clear()
        return events

    def close(self) -> None:
        self.store.save_state(self.state)

    async def _request_approval(
        self,
        request: PlanApprovalRequest,
    ) -> PlanApprovalDecision:
        callback = self.config.approval_callback
        if callback is None:
            return PlanApprovalDecision(False, "Plan approval callback is not configured")
        try:
            value = callback(request)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            self._emit("failed", "Plan approval callback failed: %s" % exc)
            return PlanApprovalDecision(False, "Plan approval callback failed: %s" % exc)
        if isinstance(value, PlanApprovalDecision):
            return PlanApprovalDecision(
                value.approved,
                sanitize_memory_text(value.feedback).strip(),
            )
        return PlanApprovalDecision(bool(value), "")

    def _emit(self, status: str, message: str) -> None:
        self.events.append(
            PlanEvent(
                status=status,
                session_id=self.store.session_id,
                mode=self.state.mode,
                plan_version=self.state.plan_version,
                approved_version=self.state.approved_version,
                message=message,
            )
        )


class EnterPlanModeTool(Tool):
    name = ENTER_PLAN_MODE_TOOL_NAME
    description = "Request approval to enter read-only plan mode"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, manager: PlanManager):
        self.manager = manager

    def is_read_only(self, input_data: Dict[str, object]) -> bool:
        return False

    async def call(self, input_data, context) -> ToolResult:
        decision = await self.manager.request_enter()
        if not decision.approved:
            return ToolResult(
                content=decision.feedback or "Plan mode entry rejected",
                raw={"approved": False, "feedback": decision.feedback},
                is_error=True,
            )
        return ToolResult(
            content=(
                "Entered plan mode. Explore first, write the plan to %s, then "
                "call %s."
                % (self.manager.store.plan_path, EXIT_PLAN_MODE_TOOL_NAME)
            ),
            raw={"approved": True, "plan_path": str(self.manager.store.plan_path)},
        )


class ExitPlanModeTool(Tool):
    name = EXIT_PLAN_MODE_TOOL_NAME
    description = "Present the plan file for approval and return to execution mode"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, manager: PlanManager):
        self.manager = manager

    def is_read_only(self, input_data: Dict[str, object]) -> bool:
        return False

    async def call(self, input_data, context) -> ToolResult:
        decision = await self.manager.request_exit()
        if not decision.approved:
            return ToolResult(
                content=(
                    "Plan was not approved. Stay in plan mode and revise it.\n"
                    "Feedback: %s" % (decision.feedback or "No feedback provided")
                ),
                raw={"approved": False, "feedback": decision.feedback},
                is_error=True,
            )
        plan = self.manager.store.validated_plan()
        return ToolResult(
            content=(
                "Plan approved. Continue directly with implementation.\n\n%s"
                % plan
            ),
            raw={
                "approved": True,
                "plan_path": str(self.manager.store.plan_path),
                "plan_version": self.manager.state.approved_version,
            },
        )


def _safe_session_id(session_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id).strip())
    if not normalized or normalized in {".", ".."}:
        raise ValueError("session_id must contain a safe path component")
    return normalized
