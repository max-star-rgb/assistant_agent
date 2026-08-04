from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import TextObservation
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.state import AgentState


class _TextConsumer:
    consumer_id = "text-recorder"
    modalities = frozenset({"text"})

    def __init__(self) -> None:
        self.observations = []

    def accept(self, _outcome, observation) -> None:
        if isinstance(observation, TextObservation):
            self.observations.append(observation)

    def close(self) -> None:
        return None


def test_coordinator_reports_text_consumers_structurally() -> None:
    coordinator = SessionEmbeddingCoordinator("session-1", MockMultimodalEmbeddingProvider())
    assert coordinator.has_consumer_for("text") is False
    consumer = _TextConsumer()
    coordinator.register_consumer(consumer)

    assert coordinator.has_consumer_for("text") is True
    assert coordinator.has_consumer_for("image") is False
    coordinator.close()


def test_runtime_text_observation_helper_ignores_empty_text() -> None:
    from assistant_agent.runtime.runtime import _stable_text_observation

    assert _stable_text_observation("session-1", "run-1", "") is None
    assert _stable_text_observation("session-1", "run-1", "   ") is None
    observation = _stable_text_observation("session-1", "run-1", " 刚才的钥匙在哪里 ", now_ms=12)
    assert observation is not None
    assert observation.text == "刚才的钥匙在哪里"
    assert observation.source == "user_request"
    assert observation.occurred_at_ms == 12


class _RecordingCoordinator:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.text_observations = []

    def has_consumer_for(self, modality: str) -> bool:
        return self.enabled and modality == "text"

    def embed_text(self, observation) -> None:
        self.text_observations.append(observation)


class _Store:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.resolve_calls = 0

    def resolve(self, _user_id, _session_id):
        self.resolve_calls += 1
        return self.coordinator


def _runtime_with_store(store) -> AgentGraphRuntime:
    runtime = object.__new__(AgentGraphRuntime)
    runtime.embedding_coordinator_store = store
    return runtime


def test_runtime_embeds_stable_text_only_when_session_has_text_consumer() -> None:
    coordinator = _RecordingCoordinator(enabled=True)
    runtime = _runtime_with_store(_Store(coordinator))
    request = UserRequest(user_id="user-1", session_id="session-1", text="刚才的钥匙在哪里")
    state = AgentState.from_request(request, run_id="run-1")

    runtime._embed_stable_request_text(state, request)

    assert coordinator.text_observations[0].source == "user_request"
    assert coordinator.text_observations[0].observation_id == "run-1"


def test_runtime_does_not_resolve_coordinator_for_empty_text() -> None:
    coordinator = _RecordingCoordinator(enabled=True)
    store = _Store(coordinator)
    runtime = _runtime_with_store(store)
    request = UserRequest(user_id="user-1", session_id="session-1", text="")
    state = AgentState.from_request(request, run_id="run-1")

    runtime._embed_stable_request_text(state, request)

    assert store.resolve_calls == 0
    assert coordinator.text_observations == []
