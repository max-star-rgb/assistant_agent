"""Shared contracts for trusted in-process Tool capability plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from assistant_agent.config import ProviderConfig

if TYPE_CHECKING:
    from assistant_agent.automation.durable_tasks.service import DurableTaskService
    from assistant_agent.media.video.realtime_video_memory import (
        RealtimeVideoMemoryStore,
    )
    from assistant_agent.media.video.video_context import VideoContextStore
    from assistant_agent.media.embedding.coordinator_store import (
        SessionEmbeddingCoordinatorStore,
    )
    from assistant_agent.media.video.semantic_store_pool import (
        SessionVisualSemanticStorePool,
    )
    from assistant_agent.media.video.visual_reminder import VisualReminderRegistry
    from assistant_agent.media.video.visual_memory_index import VisualMemoryTextIndex
    from assistant_agent.media.vision.vision_client import VisionUnderstandingClient
    from assistant_agent.media.visual_perception.history_probe import (
        VisualObservationHistoryProbe,
    )
    from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
        CalendarAdapter,
    )


@dataclass(frozen=True)
class ToolPluginContext:
    """Dependencies and structured enablement facts available to built-in plugins."""

    config: ProviderConfig
    video_context_store: VideoContextStore | None = None
    vision_client: VisionUnderstandingClient | None = None
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None
    durable_task_service: DurableTaskService | None = None
    calendar_adapter: CalendarAdapter | None = None
    embedding_coordinator_store: SessionEmbeddingCoordinatorStore | None = None
    visual_semantic_store_pool: SessionVisualSemanticStorePool | None = None
    visual_reminder_registry: VisualReminderRegistry | None = None
    visual_memory_text_index: VisualMemoryTextIndex | None = None
    visual_history_probe: VisualObservationHistoryProbe | None = None

    @property
    def mock_mode(self) -> bool:
        return self.config.provider_mode == "mock"
