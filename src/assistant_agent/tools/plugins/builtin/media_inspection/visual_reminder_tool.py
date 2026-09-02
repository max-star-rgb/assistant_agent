"""Governed management Tool for connection-scoped visual reminders."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field, model_validator

from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, TextObservation
from assistant_agent.media.video.visual_reminder import (
    VisualReminderRegistry,
    validate_visual_reminder_target_embedding,
)
from assistant_agent.native_agent.context import AssistantRunContext, authenticated_user_identity
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)


VisualReminderAction = Literal["create", "list", "cancel"]


class VisualReminderManageInput(BaseModel):
    action: VisualReminderAction
    target: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="创建提醒时用于匹配后续画面的可见条件。",
    )
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="创建提醒时命中条件后发送给用户的通知文案。",
    )
    reminder_id: str | None = Field(default=None, min_length=1, max_length=120)
    session_id: str = ""

    @model_validator(mode="after")
    def validate_action_fields(self) -> "VisualReminderManageInput":
        if self.action == "create":
            if (
                self.target is None
                or self.message is None
                or self.reminder_id is not None
            ):
                raise ValueError("create requires target and message only")
        elif self.action == "list":
            if any(
                value is not None
                for value in (self.target, self.message, self.reminder_id)
            ):
                raise ValueError("list does not accept target, message, or reminder_id")
        elif (
            self.reminder_id is None
            or self.target is not None
            or self.message is not None
        ):
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


def create_visual_reminder_manage_tool(
    *,
    coordinator_store: SessionEmbeddingCoordinatorStore,
    reminder_registry: VisualReminderRegistry,
) -> BaseTool:
    """Create the native connection-scoped visual-reminder Tool."""

    @tool(VISUAL_REMINDER_MANAGE_TOOL_NAME, response_format="content_and_artifact")
    def visual_reminder_manage(
        action: Annotated[
            VisualReminderAction,
            Field(description="创建、列出或取消当前连接的视觉提醒。"),
        ],
        runtime: ToolRuntime[AssistantRunContext],
        target: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=500,
                description="创建提醒时用于匹配后续画面的可见条件。",
            ),
        ] = None,
        message: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=500,
                description="创建提醒时命中条件后发送给用户的通知文案。",
            ),
        ] = None,
        reminder_id: Annotated[
            str | None,
            Field(min_length=1, max_length=120),
        ] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """创建、列出或取消当前活动视频连接中的一次性视觉提醒。

        当后续画面匹配目标时发送指定消息，并返回提醒 ID 或当前记录。只有成功结果
        才能视为已创建或取消。提醒只在本次连接内有效，不持久化或跨连接重放。
        """

        try:
            output = _execute_visual_reminder_manage_from_runtime(
                action=action,
                target=target,
                message=message,
                reminder_id=reminder_id,
                runtime=runtime,
                coordinator_store=coordinator_store,
                reminder_registry=reminder_registry,
            )
            data = output.model_dump(mode="json")
            return native_content_and_artifact(data, data)
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(
                exc, tool_name=VISUAL_REMINDER_MANAGE_TOOL_NAME
            ) from exc

    return configure_builtin_tool(
        visual_reminder_manage,
        availability=ToolAvailability.VIDEO_FRAME_RECEIVED.value,
        bounded_expected_errors=True,
    )


def _execute_visual_reminder_manage_from_runtime(
    *,
    action: VisualReminderAction,
    target: str | None,
    message: str | None,
    reminder_id: str | None,
    runtime: ToolRuntime[AssistantRunContext],
    coordinator_store: SessionEmbeddingCoordinatorStore,
    reminder_registry: VisualReminderRegistry,
) -> VisualReminderManageOutput:
    return _execute_visual_reminder_manage(
        VisualReminderManageInput(
            action=action,
            target=target,
            message=message,
            reminder_id=reminder_id,
            session_id=runtime.execution_info.thread_id or "",
        ),
        user_id=authenticated_user_identity(runtime),
        run_id=runtime.execution_info.run_id,
        coordinator_store=coordinator_store,
        reminder_registry=reminder_registry,
    )


def _execute_visual_reminder_manage(
    input: VisualReminderManageInput,
    user_id: str,
    run_id: str | None,
    *,
    coordinator_store: SessionEmbeddingCoordinatorStore,
    reminder_registry: VisualReminderRegistry,
) -> VisualReminderManageOutput:
    manager = reminder_registry.peek(user_id, input.session_id)
    if manager is None:
        raise ToolException("visual reminder connection is unavailable")
    if input.action == "list":
        reminders = [
            record.model_dump(mode="json") for record in manager.list_records()
        ]
        return _visual_reminder_result(
            {
                "status": "available",
                "reminders": reminders,
                "count": len(reminders),
            }
        )
    if input.action == "cancel":
        operation = manager.cancel(input.reminder_id or "")
        return _visual_reminder_result(
            {
                **operation.model_dump(mode="json"),
                "count": len(manager.list_records()),
                "reminders": [],
            }
        )
    return _create_visual_reminder(
        input,
        user_id=user_id,
        run_id=run_id,
        manager=manager,
        coordinator_store=coordinator_store,
        reminder_registry=reminder_registry,
    )


def _create_visual_reminder(
    input: VisualReminderManageInput,
    user_id: str,
    run_id: str | None,
    manager,
    *,
    coordinator_store: SessionEmbeddingCoordinatorStore,
    reminder_registry: VisualReminderRegistry,
) -> VisualReminderManageOutput:
    lease = coordinator_store.acquire(user_id, input.session_id)
    try:
        readiness = lease.coordinator.provider.readiness()
        if not (
            readiness.image_ready
            and readiness.text_ready
            and readiness.embedding_space_id
            and readiness.dimension
        ):
            raise ToolException(
                "visual reminder joint embedding space is unavailable"
            )
        observation_id = f"visual-reminder:{run_id or 'run'}:{uuid4().hex}"
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
        raise ToolException("visual reminder text embedding is unavailable")
    if (
        outcome.embedding_space_id != readiness.embedding_space_id
        or outcome.dimension != readiness.dimension
        or outcome.model_id != readiness.model_id
        or (
            readiness.model_revision is not None
            and outcome.model_revision != readiness.model_revision
        )
    ):
        raise ToolException("visual reminder text embedding is incompatible")
    try:
        validate_visual_reminder_target_embedding(
            outcome,
            session_id=input.session_id,
        )
    except ValueError:
        raise ToolException("visual reminder text embedding is incompatible") from None
    record = manager.create(
        target=input.target or "",
        message=input.message or "",
        target_embedding=outcome,
        run_id=run_id,
        trace_id=None,
    )
    return _visual_reminder_result(
        {
            **record.model_dump(mode="json"),
            "count": len(manager.list_records()),
            "reminders": [],
        }
    )


def _visual_reminder_result(
    data: dict,
) -> VisualReminderManageOutput:
    return VisualReminderManageOutput.model_validate(data)
