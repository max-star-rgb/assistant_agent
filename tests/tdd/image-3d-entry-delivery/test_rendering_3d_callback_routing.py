import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_non_media_job_callback_is_saved_without_media_delivery() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.runtime.image_to_3d_jobs import ImageTo3DJobRegistry
    from assistant_agent.gateway.artifact_delivery import GatewayArtifactDeliveryHub

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="user-sentinel",
        session_id="runtime-session-sentinel",
        source_image_id="cake",
    )
    app = FastAPI()
    app.include_router(
        create_rendering_3d_callback_router(
            GatewayArtifactDeliveryHub(),
            job_registry=jobs,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            f"/calling-agent-service/v1/{job.job_id}/0/3d-gen-back",
            json={"mediaType": "glb", "mediaUrl": "http://3d-service/model.glb"},
        )

    assert response.status_code == 200
    assert response.json() == {"code": "success"}
    completed = jobs.get(job.job_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.artifact is not None
    assert completed.artifact.model_dump(mode="json") == {
        "media_type": "glb",
        "media_url": "http://3d-service/model.glb",
        "image": None,
    }


def test_registered_job_callback_publishes_neutral_artifact_event() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.runtime.image_to_3d_jobs import ImageTo3DJobRegistry
    from assistant_agent.gateway.artifact_delivery import (
        ArtifactCompleted,
        GatewayArtifactDeliveryHub,
    )

    sent: list[ArtifactCompleted] = []

    async def sender(event: ArtifactCompleted) -> None:
        sent.append(event)

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="user-sentinel",
        session_id="runtime-session-sentinel",
        source_image_id="cake",
    )
    hub = GatewayArtifactDeliveryHub()
    asyncio.run(
        hub.register(
            session_id="runtime-session-sentinel",
            subscriber_id="connection-sentinel",
            sender=sender,
        )
    )
    app = FastAPI()
    app.include_router(
        create_rendering_3d_callback_router(hub, job_registry=jobs)
    )

    with TestClient(app) as client:
        response = client.post(
            f"/calling-agent-service/v1/{job.job_id}/0/3d-gen-back",
            json={"mediaType": "mp4", "mediaUrl": "http://3d-service/model.mp4"},
        )

    assert response.status_code == 200
    assert jobs.get(job.job_id).status == "completed"
    assert len(sent) == 1
    assert sent[0].model_dump(mode="json") == {
        "type": "artifact.completed",
        "artifact_id": job.job_id,
        "user_id": "user-sentinel",
        "session_id": "runtime-session-sentinel",
        "media_type": "mp4",
        "uri": "http://3d-service/model.mp4",
        "inline_data": None,
    }


def test_agent_service_subscriber_projects_callback_event_to_media_frame(
    monkeypatch,
) -> None:
    from assistant_agent.api import agent_service_websocket
    from assistant_agent.api.agent_service_websocket import (
        AgentServiceConnectionState,
        PreparedChat,
        _register_artifact_delivery_subscriber,
    )
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.gateway.artifact_delivery import GatewayArtifactDeliveryHub
    from assistant_agent.runtime.image_to_3d_jobs import ImageTo3DJobRegistry

    sent_text: list[str] = []

    class FakeWebSocket:
        async def send_text(self, raw: str) -> None:
            sent_text.append(raw)

    hub = GatewayArtifactDeliveryHub()
    monkeypatch.setattr(
        agent_service_websocket,
        "get_gateway_artifact_delivery_hub",
        lambda: hub,
    )
    state = AgentServiceConnectionState(
        session_id="protocol-session-sentinel",
        query_params={},
        runtime_session_id="runtime-session-sentinel",
        language="zh",
    )
    prepared = PreparedChat(
        session_id="runtime-session-sentinel",
        response_session_id="protocol-session-sentinel",
        body={"stream": True},
        chat_index="chat-sentinel",
        user_number="13800138000",
        latest_speech="生成3D",
        contents=[],
        video_ids=[],
        received_ns=1,
        accepted_ns=2,
        session_turn=1,
    )
    asyncio.run(
        _register_artifact_delivery_subscriber(
            FakeWebSocket(),
            state=state,
            prepared=prepared,
        )
    )

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="13800138000",
        session_id="runtime-session-sentinel",
        source_image_id="cake",
    )
    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(hub, job_registry=jobs))

    with TestClient(app) as client:
        response = client.post(
            f"/calling-agent-service/v1/{job.job_id}/0/3d-gen-back",
            json={"mediaType": "glb", "mediaUrl": "http://3d-service/model.glb"},
        )

    assert response.status_code == 200
    assert len(sent_text) == 1
    envelope = json.loads(sent_text[0])
    assert envelope["message"] == "chatResponse"
    body = json.loads(envelope["body"])
    assert body["number"] == "13800138000"
    assert body["message"]["content"]["intentResult"]["detail"] == [
        {"type": "TD_MODEL", "modelUrl": "http://3d-service/model.glb"}
    ]
