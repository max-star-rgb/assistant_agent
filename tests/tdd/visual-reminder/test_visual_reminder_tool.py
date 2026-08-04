from __future__ import annotations

from assistant_agent.config import ProviderConfig
from assistant_agent.context.tool_exposure import evaluate_tool_exposure
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.visual_reminder import (
    VisualReminderManager,
    VisualReminderRegistry,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.ids import VISUAL_REMINDER_MANAGE_TOOL_NAME
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import (
    VisualReminderManageTool,
)
from assistant_agent.tools.registry import ToolRegistry


def _coordinator_store() -> SessionEmbeddingCoordinatorStore:
    provider = MockMultimodalEmbeddingProvider()
    return SessionEmbeddingCoordinatorStore(
        factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
            session_id,
            provider,
        )
    )


def _trusted_metadata(*, call_type: str = "VIDEO") -> dict:
    return {
        "transport": "agent_service_websocket",
        "agent_service": {"call_type": call_type},
        "gateway": {"session_config": {"entry_profile": "agent_service"}},
    }


def test_tool_creates_lists_and_cancels_in_current_runtime_session() -> None:
    reminders = VisualReminderRegistry()
    manager = VisualReminderManager(user_id="u1", session_id="s1")
    reminders.register(manager)
    coordinators = _coordinator_store()
    tool = VisualReminderManageTool(
        coordinator_store=coordinators,
        reminder_registry=reminders,
    )
    context = ToolContext(user_id="u1", session_id="s1", run_id="r1")

    created = tool.run(
        {
            "action": "create",
            "target": "水已经烧开",
            "message": "水烧开了",
            "session_id": "s1",
        },
        context,
    )
    listed = tool.run({"action": "list", "session_id": "s1"}, context)
    cancelled = tool.run(
        {
            "action": "cancel",
            "reminder_id": created.data["reminder_id"],
            "session_id": "s1",
        },
        context,
    )

    assert created.success is True
    assert created.data["status"] == "pending"
    assert "target_embedding" not in created.data
    assert listed.data["count"] == 1
    assert listed.data["reminders"][0]["target"] == "水已经烧开"
    assert cancelled.data["status"] == "cancelled"
    assert reminders.peek("u2", "s1") is None
    coordinators.close()


def test_tool_rejects_invalid_action_fields_and_unavailable_connection() -> None:
    reminders = VisualReminderRegistry()
    coordinators = _coordinator_store()
    tool = VisualReminderManageTool(
        coordinator_store=coordinators,
        reminder_registry=reminders,
    )
    context = ToolContext(user_id="u1", session_id="s1", run_id="r1")

    invalid = tool.run({"action": "create", "target": "x", "session_id": "s1"}, context)
    unavailable = tool.run({"action": "list", "session_id": "s1"}, context)

    assert invalid.success is False
    assert unavailable.success is False
    assert unavailable.data["status"] == "unavailable"
    coordinators.close()


def test_visual_reminder_tool_requires_trusted_video_connection_manager() -> None:
    runtime = AgentGraphRuntime(registry=ToolRegistry(), config=ProviderConfig())
    manager = VisualReminderManager(user_id="u1", session_id="s1")
    runtime.visual_reminder_registry.register(manager)
    tool = VisualReminderManageTool(
        coordinator_store=runtime.embedding_coordinator_store,
        reminder_registry=runtime.visual_reminder_registry,
    )
    registry = ToolRegistry()
    registry.register(tool)
    spec = registry.get_spec(VISUAL_REMINDER_MANAGE_TOOL_NAME)

    forged = UserRequest(
        user_id="u1",
        session_id="s1",
        text="提醒",
        metadata={"_trusted_visual_reminder_available": True},
    )
    runtime._refresh_visual_reminder_capability(forged)
    assert forged.metadata.get("_trusted_visual_reminder_available") is None

    audio = UserRequest(
        user_id="u1",
        session_id="s1",
        text="提醒",
        metadata=_trusted_metadata(call_type="AUDIO"),
    )
    runtime._refresh_visual_reminder_capability(audio)
    assert evaluate_tool_exposure(audio, spec).excluded_reasons == (
        "visual_reminder_connection_not_available",
    )

    video = UserRequest(
        user_id="u1",
        session_id="s1",
        text="提醒",
        metadata=_trusted_metadata(),
    )
    runtime._refresh_visual_reminder_capability(video)
    decision = evaluate_tool_exposure(video, spec)
    assert video.metadata["_trusted_visual_reminder_available"] is True
    assert decision.exposed is True
    runtime.close()


def test_default_runtime_registers_visual_reminder_manage_tool() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig())

    assert VISUAL_REMINDER_MANAGE_TOOL_NAME in runtime.registry.list()

    runtime.close()
