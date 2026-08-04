from __future__ import annotations

import inspect
from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import SessionEmbeddingCoordinatorStore
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.decision_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME
from assistant_agent.tools.models import RunToolCatalog
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    VisualMemorySearchTool,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry


class _FixedTextProvider(MockMultimodalEmbeddingProvider):
    def embed_text(self, observation):
        return EmbeddingEvent(
            event_id="query",
            modality="text",
            vector=[1.0, 0.0],
            embedding_space_id="visual-text-test",
            model_id="fixed",
            model_revision="v1",
            dimension=2,
            normalized=True,
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            text_source=observation.source,
            latency_ms=0,
        )


def _stores_with_history(tmp_path: Path):
    coordinator_store = SessionEmbeddingCoordinatorStore(
        factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
            session_id,
            _FixedTextProvider(),
        )
    )
    coordinator_store.resolve("user-1", "session-1")
    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    semantic_pool.resolve("user-1", "session-1").record_success(
        VisualSemanticRecord(
            record_id="record-1",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=1,
            captured_at_ms=100,
            summary="白色低帮运动鞋",
            scene="商品展示台",
            objects=["白色低帮运动鞋"],
            search_embedding=[1.0, 0.0],
            embedding_space_id="visual-text-test",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=100,
        )
    )
    return coordinator_store, semantic_pool


def test_visual_memory_tool_exposes_only_model_owned_fields(tmp_path: Path) -> None:
    coordinator_store, semantic_pool = _stores_with_history(tmp_path)
    registry = create_default_registry(
        ProviderConfig(),
        embedding_coordinator_store=coordinator_store,
        visual_semantic_store_pool=semantic_pool,
    )

    schema = registry.get_spec(VISUAL_MEMORY_SEARCH_TOOL_NAME).input_schema

    assert set(schema["properties"]) == {"query", "time_window", "search_mode"}


def test_tool_constructor_has_no_vision_client() -> None:
    assert "vision_client" not in inspect.signature(VisualMemorySearchTool).parameters


def test_visual_memory_tool_runs_through_validator_executor_registry(tmp_path: Path) -> None:
    coordinator_store, semantic_pool = _stores_with_history(tmp_path)
    registry = create_default_registry(
        ProviderConfig(),
        embedding_coordinator_store=coordinator_store,
        visual_semantic_store_pool=semantic_pool,
    )
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="鞋在哪里",
        metadata={"_trusted_visual_memory_available": True},
    )
    state = AgentState.from_request(request, run_id="run-1")
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=[VISUAL_MEMORY_SEARCH_TOOL_NAME]
    )
    decision = AssistantToolCall(
        tool_name=VISUAL_MEMORY_SEARCH_TOOL_NAME,
        tool_input={"query": "白色低帮运动鞋"},
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-1",
        VISUAL_MEMORY_SEARCH_TOOL_NAME,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert validation.accepted is True
    assert result.success is True
    assert result.data["status"] == "confirmed"
    assert "evidence_ref" not in str(result.data)
    assert "search_embedding" not in str(result.data)
    coordinator_store.close()
    semantic_pool.close()
