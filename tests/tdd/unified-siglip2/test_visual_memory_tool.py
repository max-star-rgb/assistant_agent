from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.consumers.temporal_memory import TemporalVisualMemory
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import SessionEmbeddingCoordinatorStore
from assistant_agent.media.embedding.models import EmbeddingEvent, ImageObservation
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.decision_models import AssistantToolCall
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.models import RunToolCatalog


def _store_with_history(tmp_path):
    def factory(_user_id, session_id):
        coordinator = SessionEmbeddingCoordinator(
            session_id, MockMultimodalEmbeddingProvider(dimension=2)
        )
        memory = TemporalVisualMemory(root=tmp_path / "evidence")
        source = tmp_path / "frame.jpg"
        source.write_bytes(b"jpeg")
        event = EmbeddingEvent(
            event_id="event-frame",
            modality="image",
            vector=[1.0, 0.0],
            embedding_space_id="mock-multimodal-space-v1",
            model_id="mock-multimodal-embedding",
            model_revision="mock-v1",
            dimension=2,
            normalized=True,
            session_id=session_id,
            source_observation_id="frame-1",
            frame_sequence=1,
            captured_at_ms=100,
            latency_ms=0,
        )
        memory.accept(
            event,
            ImageObservation(
                session_id=session_id,
                observation_id="frame-1",
                image_ref=str(source),
                frame_sequence=1,
                captured_at_ms=100,
            ),
        )
        coordinator.temporal_visual_memory = memory
        return coordinator

    store = SessionEmbeddingCoordinatorStore(factory=factory)
    store.resolve("user-1", "session-1")
    return store


def test_visual_memory_tool_exposes_only_model_owned_fields(tmp_path) -> None:
    registry = create_default_registry(
        ProviderConfig(), embedding_coordinator_store=_store_with_history(tmp_path)
    )

    schema = registry.get_spec(VISUAL_MEMORY_SEARCH_TOOL_NAME).input_schema

    assert set(schema["properties"]) == {"query", "time_window", "search_mode"}


def test_visual_memory_tool_runs_through_validator_executor_registry(tmp_path) -> None:
    store = _store_with_history(tmp_path)
    registry = create_default_registry(ProviderConfig(), embedding_coordinator_store=store)
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="钥匙在哪里",
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
        decision=decision, registry=registry, request=request, state=state
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
    store.close()
