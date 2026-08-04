import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_non_media_job_callback_is_saved_without_media_delivery() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.image_to_3d_completion import ImageTo3DJobRegistry
    from assistant_agent.media.rendering_3d_relay import Rendering3DRelayRegistry

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="user-sentinel",
        session_id="runtime-session-sentinel",
        source_image_id="cake",
        delivery_target="none",
    )
    app = FastAPI()
    app.include_router(
        create_rendering_3d_callback_router(
            Rendering3DRelayRegistry(),
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


def test_agent_service_job_callback_is_saved_then_delivered() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.image_to_3d_completion import ImageTo3DJobRegistry
    from assistant_agent.media.rendering_3d_relay import Rendering3DRelayRegistry

    sent: list[dict] = []

    async def sender(frame: dict) -> None:
        sent.append(frame)

    jobs = ImageTo3DJobRegistry()
    job = jobs.register(
        user_id="user-sentinel",
        session_id="runtime-session-sentinel",
        source_image_id="cake",
        delivery_target="agent_service",
    )
    relays = Rendering3DRelayRegistry()
    asyncio.run(
        relays.register(
            session_id="runtime-session-sentinel",
            connection_id="connection-sentinel",
            number="13800138000",
            language="zh",
            sender=sender,
        )
    )
    app = FastAPI()
    app.include_router(
        create_rendering_3d_callback_router(relays, job_registry=jobs)
    )

    with TestClient(app) as client:
        response = client.post(
            f"/calling-agent-service/v1/{job.job_id}/0/3d-gen-back",
            json={"mediaType": "mp4", "mediaUrl": "http://3d-service/model.mp4"},
        )

    assert response.status_code == 200
    assert jobs.get(job.job_id).status == "completed"
    assert len(sent) == 1
    assert sent[0]["message"] == "chatResponse"
