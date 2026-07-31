from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from assistant_agent.api.agent_service_websocket import (
    AgentServiceConnectionState,
    PreparedChat,
    _prepared_chat_response,
)
from assistant_agent.gateway import GatewaySessionManager
from assistant_agent.gateway.runtime_types import RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.gateway.turn_facade import GatewayTurnFacade, GatewayTurnRequest
from assistant_agent.observability.agent_service_delivery import AgentServiceDelivery
from assistant_agent.runtime import generated_artifacts


class ImageResultBackend:
    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        if event_sink is not None:
            await event_sink(
                RealtimeAgentEvent(
                    type="response.chunk",
                    text="image-ready-sentinel",
                )
            )
        return RealtimeAgentResult(
            status="completed",
            response_text="image-ready-sentinel",
            run_id=request.run_id,
            output_refs=["/artifacts/generated/image-sentinel.jpg"],
        )


def test_gateway_turn_preserves_output_refs_for_entry_adapter() -> None:
    async def run() -> None:
        manager = GatewaySessionManager(
            backend_factory=ImageResultBackend,
            start_reaper=False,
        )
        facade = GatewayTurnFacade(manager=manager)
        try:
            result = await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-sentinel",
                    session_id="session-sentinel",
                    text="generate-image-sentinel",
                )
            )
        finally:
            await facade.close()
            await manager.close()

        assert result.response_text == "image-ready-sentinel"
        assert result.payload["output_refs"] == [
            "/artifacts/generated/image-sentinel.jpg"
        ]

    asyncio.run(run())


def test_agent_service_success_terminal_embeds_generated_image_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    jpeg_bytes = b"\xff\xd8\xff\xe0image-sentinel\xff\xd9"
    artifact_dir = tmp_path / "generated"
    artifact_dir.mkdir()
    (artifact_dir / "image-sentinel.jpg").write_bytes(jpeg_bytes)
    monkeypatch.setattr(
        generated_artifacts,
        "GENERATED_ARTIFACT_DIR",
        artifact_dir,
    )

    response = _prepared_chat_response(
        PreparedChat(
            session_id="session-sentinel",
            response_session_id=None,
            body={"stream": True},
            chat_index="chat-sentinel",
            user_number="user-sentinel",
            latest_speech="generate-image-sentinel",
            contents=[],
            video_ids=[],
            received_ns=1,
            accepted_ns=2,
            session_turn=1,
        ),
        state=AgentServiceConnectionState(
            session_id="session-sentinel",
            query_params={},
            media_protocol=True,
        ),
        turn=SimpleNamespace(
            status="completed",
            response_text="image-ready-sentinel",
            payload={
                "output_refs": ["/artifacts/generated/image-sentinel.jpg"]
            },
        ),
        delivery=AgentServiceDelivery(
            delivery_id="delivery-sentinel",
            session_digest="session-digest-sentinel",
            chat_index_digest="chat-digest-sentinel",
            chat_index="chat-sentinel",
            expects_ack=False,
        ),
        sequence=2,
        streamed_text="image-ready-sentinel",
    )

    body = json.loads(response["body"])
    intent = body["message"]["content"]["intentResult"]
    assert intent["description"] == ""
    assert intent["status"] == "SUCCESS"
    assert intent["detail"] == [
        {
            "type": "IMAGE",
            "image": (
                "data:image/jpeg;base64,"
                + base64.b64encode(jpeg_bytes).decode("ascii")
            ),
        }
    ]
    assert body["final"] is True
    assert body["display_only"] is True
