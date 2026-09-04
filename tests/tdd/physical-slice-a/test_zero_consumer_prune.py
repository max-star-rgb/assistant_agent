from assistant_agent.config import load_app_config
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import TextObservation
from assistant_agent.media.embedding.observability import InMemoryEmbeddingObserver
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider


def test_retired_web_settings_do_not_select_a_real_provider() -> None:
    config = load_app_config(
        {
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "tavily",
            "TAVILY_API_KEY": "retired-web-key",
            "WEB_SEARCH_BASE_URL": "https://retired-web.example/v1",
        }
    )

    assert config.chat.chat_provider == "mock"
    assert config.vision.vision_provider == "mock"
    assert config.tools.image_generation.image_generation_provider == "mock"


def test_embedding_without_consumers_emits_only_inference_lifecycle() -> None:
    observer = InMemoryEmbeddingObserver()
    coordinator = SessionEmbeddingCoordinator(
        "session-sentinel",
        MockMultimodalEmbeddingProvider(),
        observer=observer,
    )

    coordinator.embed_text(
        TextObservation(
            session_id="session-sentinel",
            observation_id="text-sentinel",
            text="提醒条件",
            source="test",
        )
    )
    coordinator.close()

    assert [event.event_name for event in observer.events] == [
        "embedding.requested",
        "embedding.started",
        "embedding.finished",
        "embedding.session_cleanup",
    ]
