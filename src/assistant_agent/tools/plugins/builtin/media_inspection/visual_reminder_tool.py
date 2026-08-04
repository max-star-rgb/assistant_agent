"""Governed management Tool for connection-scoped visual reminders."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, TextObservation
from assistant_agent.media.video.visual_reminder import VisualReminderRegistry
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.models import ToolResult


VisualReminderAction = Literal["create", "list", "cancel"]


class VisualReminderManageInput(BaseModel):
    action: VisualReminderAction
    target: str | None = Field(default=None, min_length=1, max_length=500)
    message: str | None = Field(default=None, min_length=1, max_length=500)
    reminder_id: str | None = Field(default=None, min_length=1, max_length=120)
    session_id: str = ""

    @model_validator(mode="after")
    def validate_action_fields(self) -> "VisualReminderManageInput":
        if self.action == "create":
            if self.target is None or self.message is None or self.reminder_id is not None:
                raise ValueError("create requires target and message only")
        elif self.action == "list":
            if any(value is not None for value in (self.target, self.message, self.reminder_id)):
                raise ValueError("list does not accept target, message, or reminder_id")
        elif self.reminder_id is None or self.target is not None or self.message is not None:
            raise ValueError("cancel requires reminder_id only")
        return self


class VisualReminderManageOutput(BaseModel):
    status: str
    reminder_id: str | None = None
    target: str | None = None
    message: str | None = None
    changed: bool | None = None
    reminders: list[dict] = Field(default_factory=list)
    count: int = 0


class VisualReminderManageTool(ToolBase):
    name = VISUAL_REMINDER_MANAGE_TOOL_NAME
    description = "创建、查看或取消当前视频连接中的一次性视觉提醒。"
    input_schema = VisualReminderManageInput
    output_schema = VisualReminderManageOutput
    category = "write"
    requires_media = []
    runtime_input_bindings = (
        RuntimeInputBinding(
            field="session_id",
            source="runtime_identity",
            key="session_id",
        ),
    )

    def __init__(
        self,
        *,
        coordinator_store: SessionEmbeddingCoordinatorStore,
        reminder_registry: VisualReminderRegistry,
    ) -> None:
        self.coordinator_store = coordinator_store
        self.reminder_registry = reminder_registry

    def _run(
        self,
        input: VisualReminderManageInput,
        context: ToolContext,
    ) -> ToolResult:
        user_id = context.user_id or ""
        manager = self.reminder_registry.peek(user_id, input.session_id)
        if manager is None:
            return self._result(
                {"status": "unavailable", "count": 0, "reminders": []},
                success=False,
                error="visual reminder connection is unavailable",
            )
        if input.action == "list":
            reminders = [record.model_dump(mode="json") for record in manager.list_records()]
            return self._result(
                {
                    "status": "available",
                    "reminders": reminders,
                    "count": len(reminders),
                }
            )
        if input.action == "cancel":
            operation = manager.cancel(input.reminder_id or "")
            return self._result(
                {
                    **operation.model_dump(mode="json"),
                    "count": len(manager.list_records()),
                    "reminders": [],
                }
            )
        return self._create(input, context, manager)

    def _create(self, input, context, manager) -> ToolResult:
        lease = self.coordinator_store.acquire(context.user_id or "", input.session_id)
        try:
            observation_id = (
                f"visual-reminder:{context.run_id or 'run'}:{uuid4().hex}"
            )
            outcome = lease.coordinator.embed_text(
                TextObservation(
                    session_id=input.session_id,
                    observation_id=observation_id,
                    text=input.target or "",
                    source="user_text",
                )
            )
        finally:
            lease.release()
        if not isinstance(outcome, EmbeddingEvent):
            return self._result(
                {"status": "unavailable", "count": 0, "reminders": []},
                success=False,
                error="visual reminder text embedding is unavailable",
            )
        record = manager.create(
            target=input.target or "",
            message=input.message or "",
            target_embedding=outcome,
        )
        return self._result(
            {
                **record.model_dump(mode="json"),
                "count": len(manager.list_records()),
                "reminders": [],
            }
        )

    def _result(
        self,
        data: dict,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> ToolResult:
        output = VisualReminderManageOutput.model_validate(data).model_dump(mode="json")
        return ToolResult(
            tool_name=self.name,
            success=success,
            data=output,
            model_observation=output,
            voice_summary=_voice_summary(output),
            error=error,
        )


def _voice_summary(data: dict) -> str:
    status = data.get("status")
    if status == "pending":
        return "已创建当前视频连接的一次性视觉提醒。"
    if status == "cancelled":
        return "已取消视觉提醒。"
    if status == "available":
        return f"当前视频连接有 {data.get('count', 0)} 条视觉提醒记录。"
    if status == "not_found":
        return "没有找到该视觉提醒。"
    return "当前视频连接无法完成视觉提醒操作。"
