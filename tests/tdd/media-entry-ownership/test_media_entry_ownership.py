from importlib.util import find_spec

from assistant_agent.media.video.understanding_service import (
    VideoUnderstandingService,
)
from assistant_agent.media.vision.models import VideoUnderstandingRequest


class _ExplodingClient:
    def __init__(self) -> None:
        self.calls = 0

    def understand(self, _request: object) -> object:
        self.calls += 1
        raise AssertionError("trusted realtime requests must not call the VLM")


def test_nested_agent_service_metadata_uses_the_live_text_path() -> None:
    client = _ExplodingClient()
    outcome = VideoUnderstandingService(client=client).inspect(
        VideoUnderstandingRequest(
            video_ref="video-1",
            user_id="user-1",
            session_id="thread-1",
            metadata={
                "request_metadata": {
                    "transport": "agent_service_websocket",
                    "gateway": {
                        "session_config": {"entry_profile": "agent_service"}
                    },
                }
            },
        )
    )

    assert outcome.status == "partial"
    assert client.calls == 0
    assert find_spec("assistant_agent.media.agent_service_entry") is None
