from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant_agent.api.agent_service_websocket import (
    AgentServiceConnectionState,
    PreparedChat,
    _prepared_chat_response,
)
from assistant_agent.observability.agent_service_delivery import AgentServiceDelivery
from assistant_agent.runtime import generated_artifacts
from assistant_agent.tools.base import ToolContext


def _prepared_chat() -> PreparedChat:
    return PreparedChat(
        session_id="agent-service-session-sentinel",
        response_session_id=None,
        body={"stream": True},
        chat_index="chat-sentinel",
        user_number="13800138000",
        latest_speech="生成图片",
        contents=[],
        video_ids=[],
        received_ns=1,
        accepted_ns=2,
        session_turn=1,
    )


def test_generated_image_uses_rendering_brief_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    jpeg_bytes = b"\xff\xd8\xff\xe0rendering-sentinel\xff\xd9"
    artifact_dir = tmp_path / "generated"
    artifact_dir.mkdir()
    (artifact_dir / "rendering-sentinel.jpg").write_bytes(jpeg_bytes)
    monkeypatch.setattr(generated_artifacts, "GENERATED_ARTIFACT_DIR", artifact_dir)

    response = _prepared_chat_response(
        _prepared_chat(),
        state=AgentServiceConnectionState(
            session_id="13800138000",
            query_params={},
            media_protocol=True,
        ),
        turn=SimpleNamespace(
            status="completed",
            response_text="图片已生成",
            payload={"output_refs": ["/artifacts/generated/rendering-sentinel.jpg"]},
        ),
        delivery=AgentServiceDelivery(
            delivery_id="delivery-sentinel",
            session_digest="session-digest",
            chat_index_digest="chat-digest",
            chat_index="chat-sentinel",
            expects_ack=False,
        ),
        sequence=1,
    )

    body = json.loads(response["body"])
    assert body["number"] == "13800138000"
    assert body["message"]["type"] == "BRIEF"
    assert body["message"]["content"]["intentResult"]["detail"] == [
        {
            "type": "IMAGE",
            "imageId": "rendering-sentinel.jpg",
            "image": base64.b64encode(jpeg_bytes).decode("ascii"),
        }
    ]


def test_image_to_3d_adapter_reads_src_image_id_and_submits_base64(
    tmp_path: Path,
) -> None:
    from assistant_agent.media.image_to_3d import (
        ImageTo3DAdapter,
        ImageTo3DSettings,
    )

    image = tmp_path / "cake_001.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nrendering-sentinel")
    requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def request_json(method: str, url: str, body: bytes | None, headers: dict[str, str]):
        requests.append((method, url, body, headers))
        return {
            "errCode": 0,
            "errMessage": "success",
            "data": {"status": "generating", "json": {"status": "generating"}},
        }

    adapter = ImageTo3DAdapter(
        ImageTo3DSettings(
            td_gen_url="http://3dgen/3dgen/v1/openapi/img-to-3d",
            public_base_url="http://agent:8000",
            generated_artifact_path=tmp_path,
        ),
        request_json=request_json,
    )

    result = adapter.start(
        session_id="session-sentinel",
        src_image="cake_001",
    )

    assert result.status == "generating"
    assert result.media_id == "cake_001"
    assert len(requests) == 1
    assert requests[0][0] == "POST"
    assert requests[0][1] == "http://3dgen/3dgen/v1/openapi/img-to-3d"
    payload = json.loads((requests[0][2] or b"").decode("utf-8"))
    assert payload == {
        "sessionId": "session-sentinel",
        "image": base64.b64encode(image.read_bytes()).decode("ascii"),
        "pre_cb_url": (
            "http://agent:8000/calling-agent-service/v1/"
            "session-sentinel/0/3d-gen-back"
        ),
        "cb_url": (
            "http://agent:8000/calling-agent-service/v1/"
            "session-sentinel/0/3d-gen-back"
        ),
        "format": "mp4",
    }
    assert requests[0][3]["User-Agent"] == "AgentService/1.0"


def test_image_generation_exposes_id_without_mirroring_local_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assistant_agent.runtime import generated_artifacts
    from assistant_agent.tools.plugins.builtin.image_generation.models import (
        ImageGenerationResult,
    )
    from assistant_agent.tools.plugins.builtin.image_generation.tool import (
        ImageGenerationTool,
    )

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    image_bytes = b"\x89PNG\r\n\x1a\ncake-sentinel"
    (artifact_dir / "cake_001.png").write_bytes(image_bytes)
    monkeypatch.setattr(generated_artifacts, "GENERATED_ARTIFACT_DIR", artifact_dir)

    class Adapter:
        def generate(self, input):
            return ImageGenerationResult(
                task_id="task-sentinel",
                status="succeeded",
                image_url="/artifacts/generated/cake_001.png",
                image_urls=["/artifacts/generated/cake_001.png"],
                download_url="/artifacts/generated/cake_001.png",
                download_urls=["/artifacts/generated/cake_001.png"],
                prompt=input.prompt,
                output_ref="/artifacts/generated/cake_001.png",
            )

    result = ImageGenerationTool(adapter=Adapter()).run(
        {"prompt": "蛋糕"},
        ToolContext(user_id="user-sentinel", session_id="session-sentinel"),
    )

    assert result.success is True
    assert result.data["image_id"] == ["cake_001"]
    assert result.model_observation["image_id"] == ["cake_001"]
    assert list(artifact_dir.iterdir()) == [artifact_dir / "cake_001.png"]


def test_image_to_3d_adapter_rejects_unsafe_src_image_id(
    tmp_path: Path,
) -> None:
    from assistant_agent.media.image_to_3d import (
        ImageTo3DAdapter,
        ImageTo3DError,
        ImageTo3DSettings,
    )

    adapter = ImageTo3DAdapter(
        ImageTo3DSettings(
            td_gen_url="http://3dgen/img-to-3d",
            public_base_url="http://agent:8000",
            generated_artifact_path=tmp_path,
        )
    )

    with pytest.raises(ImageTo3DError, match="图片不存在"):
        adapter.start(
            session_id="session-sentinel",
            src_image="../generated-sentinel",
        )


def test_image_to_3d_adapter_normalizes_malformed_service_response(
    tmp_path: Path,
) -> None:
    from assistant_agent.media.image_to_3d import (
        ImageTo3DAdapter,
        ImageTo3DError,
        ImageTo3DSettings,
    )

    image = tmp_path / "cake_001.webp"
    image.write_bytes(b"RIFF\x00\x00\x00\x00WEBPrendering-sentinel")

    def request_json(method: str, url: str, body: bytes | None, headers: dict[str, str]):
        _ = (url, body, headers)
        return {"errCode": 0, "errMessage": "success", "data": ["invalid"]}

    adapter = ImageTo3DAdapter(
        ImageTo3DSettings(
            td_gen_url="http://3dgen/img-to-3d",
            public_base_url="http://agent:8000",
            generated_artifact_path=tmp_path,
        ),
        request_json=request_json,
    )

    with pytest.raises(ImageTo3DError, match="3D生成服务响应解析失败"):
        adapter.start(
            session_id="session-sentinel",
            src_image="cake_001",
        )


def test_3d_callback_delivers_td_model_on_registered_connection() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.media_relay_delivery import MediaRelayConnectionRegistry

    registry = MediaRelayConnectionRegistry()
    delivered: list[dict] = []

    async def send(response: dict) -> None:
        delivered.append(response)

    registry.register(
        connection_id="connection-sentinel",
        session_ids=["session-sentinel"],
        number="13800138000",
        send=send,
    )
    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(registry))

    with TestClient(app) as client:
        response = client.post(
            "/calling-agent-service/v1/session-sentinel/chat-sentinel/3d-gen-back",
            json={
                "mediaType": "glb",
                "mediaUrl": "http://renderer/model.glb",
            },
        )

    assert response.status_code == 200
    assert response.json()["errMessage"] == "success"
    body = json.loads(delivered[0]["body"])
    assert body["number"] == "13800138000"
    assert body["message"]["type"] == "BRIEF"
    assert body["message"]["chatIndex"] == "chat-sentinel"
    assert body["message"]["content"]["intentResult"]["detail"] == [
        {"type": "TD_MODEL", "modelUrl": "http://renderer/model.glb"}
    ]


def test_image_to_3d_tool_uses_runtime_owned_identity() -> None:
    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool

    calls: list[dict[str, str]] = []

    class Adapter:
        def start(self, *, session_id: str, src_image: str, output_format: str):
            calls.append(
                {
                    "session_id": session_id,
                    "src_image": src_image,
                    "output_format": output_format,
                }
            )
            return ImageTo3DSubmission(
                status="generating",
                media_id="media-sentinel.png",
                response={"errCode": 0, "errMessage": "success"},
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"format": "glb", "src_image": "cake_001"},
        ToolContext(
            user_id="13800138000",
            session_id="session-sentinel",
            run_id="run-sentinel",
            metadata={
                "request_metadata": {"transport": "agent_service_websocket"}
            },
        ),
    )

    assert result.success is True
    assert result.data == {
        "status": "generating",
        "media_id": "media-sentinel.png",
    }
    assert calls == [
        {
            "session_id": "session-sentinel",
            "src_image": "cake_001",
            "output_format": "glb",
        }
    ]


def test_image_to_3d_exposes_required_src_image_parameter() -> None:
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
        ImageTo3DTool,
        MockImageTo3DAdapter,
    )

    schema = ImageTo3DTool(adapter=MockImageTo3DAdapter()).input_schema.model_json_schema()

    assert "src_image" in schema["required"]
    assert schema["properties"]["src_image"]["type"] == "string"


def test_real_image_to_3d_plugin_requires_no_renderer_configuration() -> None:
    from assistant_agent.config import ProviderConfig
    from assistant_agent.tools.plugins.builtin.image_to_3d.plugin import (
        ImageTo3DToolPlugin,
    )
    from assistant_agent.tools.plugins.contracts import ToolPluginContext

    config = ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        qwen_api_key="test-only-key",
        td_gen_ip="10.0.0.3",
        td_gen_port=8000,
        public_ip="10.0.0.4",
        public_port=8001,
    )

    tools = ImageTo3DToolPlugin().build_tools(
        ToolPluginContext(config=config, mcp_server_configs=[])
    )

    assert [tool.name for tool in tools] == ["image_to_3d"]
    assert not hasattr(config, "rendering_url")
    assert not hasattr(config, "image_storage_path")


def test_image_to_3d_tool_rejects_non_agent_service_entry() -> None:
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
        ImageTo3DTool,
        MockImageTo3DAdapter,
    )

    result = ImageTo3DTool(adapter=MockImageTo3DAdapter()).run(
        {"format": "mp4", "src_image": "cake_001"},
        ToolContext(
            user_id="13800138000",
            session_id="session-sentinel",
            metadata={"request_metadata": {"transport": "http"}},
        ),
    )

    assert result.success is False
    assert result.error == "image_to_3d requires Agent-Service WebSocket entry"


def test_image_to_3d_tool_treats_failed_service_status_as_failure() -> None:
    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool

    class Adapter:
        def start(self, *, session_id: str, src_image: str, output_format: str):
            _ = (session_id, src_image, output_format)
            return ImageTo3DSubmission(
                status="failed",
                media_id="media-sentinel.png",
                response={"errCode": 0, "errMessage": "success"},
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"format": "mp4", "src_image": "cake_001"},
        ToolContext(
            user_id="13800138000",
            session_id="session-sentinel",
            metadata={
                "request_metadata": {"transport": "agent_service_websocket"}
            },
        ),
    )

    assert result.success is False
    assert result.data == {
        "status": "failed",
        "media_id": "media-sentinel.png",
    }


def test_3d_callback_projects_mp4_as_video_detail() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.media_relay_delivery import MediaRelayConnectionRegistry

    registry = MediaRelayConnectionRegistry()
    delivered: list[dict] = []

    async def send(response: dict) -> None:
        delivered.append(response)

    registry.register(
        connection_id="connection-video-sentinel",
        session_ids=["session-video-sentinel"],
        number="13800138000",
        send=send,
    )
    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(registry))

    with TestClient(app) as client:
        response = client.post(
            "/calling-agent-service/v1/session-video-sentinel/0/3d-gen-back",
            json={
                "mediaType": "mp4",
                "mediaUrl": "http://renderer/model.mp4",
            },
        )

    assert response.status_code == 200
    body = json.loads(delivered[0]["body"])
    assert body["message"]["content"]["intentResult"]["detail"] == [
        {"type": "VIDEO", "videoUrl": "http://renderer/model.mp4"}
    ]


def test_media_relay_registry_reports_closed_connection_as_not_delivered() -> None:
    from assistant_agent.media.media_relay_delivery import MediaRelayConnectionRegistry

    registry = MediaRelayConnectionRegistry()

    async def send(response: dict) -> None:
        _ = response
        raise RuntimeError("connection closed")

    registry.register(
        connection_id="closed-connection-sentinel",
        session_ids=["closed-session-sentinel"],
        number="13800138000",
        send=send,
    )

    delivered = asyncio.run(
        registry.deliver_3d_result(
            session_id="closed-session-sentinel",
            chat_index="0",
            media_type="glb",
            model_url="http://renderer/model.glb",
        )
    )

    assert delivered is False
