from __future__ import annotations

import base64
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
            expects_ack=True,
        ),
        sequence=1,
    )

    body = json.loads(response["body"])
    assert body["chatIndex"] == "chat-sentinel"
    assert body["number"] == "13800138000"
    assert body["messageType"] == "ANSWER"
    assert body["display_only"] is False
    assert "displayOnly" not in body
    assert "sequence" not in body
    assert "final" not in body
    assert "deliveryId" not in body
    content = body["message"]["content"]
    assert content["intentExecution"] == {
        "description": "",
        "plans": [],
        "messageType": "ANSWER",
    }
    assert content["intentResult"] == {
        "description": "图片已生成",
        "status": "SUCCESS",
        "plan": [],
        "messageType": "ANSWER",
        "detail": [
            {
                "type": "IMAGE",
                "imageId": "rendering-sentinel",
                "image": base64.b64encode(jpeg_bytes).decode("ascii"),
            }
        ],
    }
    assert content["intentWeb"] == {
        "description": "",
        "resourceType": "",
        "resourceUrl": "",
    }


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
        chat_index="chat-sentinel",
        src_image="cake_001",
    )

    assert result.status == "generating"
    assert result.source_image_id == "cake_001"
    assert len(requests) == 1
    assert requests[0][0] == "POST"
    assert requests[0][1] == "http://3dgen/3dgen/v1/openapi/img-to-3d"
    payload = json.loads((requests[0][2] or b"").decode("utf-8"))
    assert payload == {
        "sessionId": "session-sentinel",
        "image": base64.b64encode(image.read_bytes()).decode("ascii"),
        "pre_cb_url": (
            "http://agent:8000/calling-agent-service/v1/"
            "session-sentinel/chat-sentinel/3d-gen-back"
        ),
        "cb_url": (
            "http://agent:8000/calling-agent-service/v1/"
            "session-sentinel/chat-sentinel/3d-gen-back"
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


def test_local_image_fixture_returns_managed_artifact(tmp_path: Path) -> None:
    from assistant_agent.tools.plugins.builtin.image_generation.backend import (
        LocalFixtureImageGenerationAdapter,
    )
    from assistant_agent.tools.plugins.builtin.image_generation.models import (
        ImageGenerationRequest,
    )

    (tmp_path / "cake.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    result = LocalFixtureImageGenerationAdapter(
        "cake.png",
        artifact_dir=tmp_path,
    ).generate(ImageGenerationRequest(prompt="蛋糕"))

    assert result.status == "succeeded"
    assert result.output_ref == "/artifacts/generated/cake.png"
    assert result.provider == "local_fixture"


@pytest.mark.parametrize("fixture_id", ["../cake.png", "/tmp/cake.png", "missing.png"])
def test_local_image_fixture_fails_closed(
    tmp_path: Path,
    fixture_id: str,
) -> None:
    from assistant_agent.providers.provider_errors import ProviderAdapterError
    from assistant_agent.tools.plugins.builtin.image_generation.backend import (
        LocalFixtureImageGenerationAdapter,
    )
    from assistant_agent.tools.plugins.builtin.image_generation.models import (
        ImageGenerationRequest,
    )

    adapter = LocalFixtureImageGenerationAdapter(fixture_id, artifact_dir=tmp_path)

    with pytest.raises(ProviderAdapterError) as captured:
        adapter.generate(ImageGenerationRequest(prompt="蛋糕"))

    assert captured.value.code == "provider_unavailable"


def test_real_image_plugin_fixture_does_not_build_real_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from assistant_agent.config import ProviderConfig
    from assistant_agent.tools.plugins.builtin.image_generation import plugin as plugin_module
    from assistant_agent.tools.plugins.contracts import ToolPluginContext

    (tmp_path / "cake.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    monkeypatch.setattr(plugin_module, "GENERATED_ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(plugin_module, "DEVELOPMENT_IMAGE_FIXTURE_ID", "cake.png")

    def reject_real_provider(config):
        _ = config
        raise AssertionError("real image provider must not be constructed")

    monkeypatch.setattr(
        plugin_module,
        "create_image_generation_adapter",
        reject_real_provider,
    )
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        qwen_api_key="test-only-key",
    )

    tools = plugin_module.ImageGenerationToolPlugin().build_tools(
        ToolPluginContext(config=config, mcp_server_configs=[])
    )
    result = tools[0].run(
        {"prompt": "蛋糕"},
        ToolContext(user_id="user-sentinel", session_id="session-sentinel"),
    )

    assert result.success is True
    assert result.output_ref == "/artifacts/generated/cake.png"
    assert result.data["image_id"] == ["cake"]


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


def test_image_to_3d_adapter_accepts_nested_generating_status(
    tmp_path: Path,
) -> None:
    from assistant_agent.media.image_to_3d import (
        ImageTo3DAdapter,
        ImageTo3DSettings,
    )

    (tmp_path / "cake_001.png").write_bytes(b"\x89PNG\r\n\x1a\nrendering-sentinel")

    def request_json(method: str, url: str, body: bytes | None, headers: dict[str, str]):
        _ = (method, url, body, headers)
        return {
            "errCode": 0,
            "errMessage": "success",
            "data": {"json": {"status": "generating"}},
        }

    result = ImageTo3DAdapter(
        ImageTo3DSettings(
            td_gen_url="http://3dgen/img-to-3d",
            public_base_url="http://agent:8000",
            generated_artifact_path=tmp_path,
        ),
        request_json=request_json,
    ).start(session_id="session-sentinel", src_image="cake_001")

    assert result.status == "generating"


def test_image_to_3d_adapter_accepts_current_queued_response(
    tmp_path: Path,
) -> None:
    from assistant_agent.media.image_to_3d import (
        ImageTo3DAdapter,
        ImageTo3DSettings,
    )

    (tmp_path / "cake_001.png").write_bytes(b"\x89PNG\r\n\x1a\nrendering-sentinel")

    def request_json(method: str, url: str, body: bytes | None, headers: dict[str, str]):
        _ = (method, url, body, headers)
        return {"status": "queued", "queue_position": 1}

    result = ImageTo3DAdapter(
        ImageTo3DSettings(
            td_gen_url="http://3dgen/img-to-3d",
            public_base_url="http://agent:8000",
            generated_artifact_path=tmp_path,
        ),
        request_json=request_json,
    ).start(session_id="session-sentinel", src_image="cake_001")

    assert result.status == "queued"


@pytest.mark.parametrize(
    ("media_type", "media_url", "expected_detail"),
    [
        (
            "ply",
            "http://renderer/model.ply",
            {"type": "TD_MODEL", "modelUrl": "http://renderer/model.ply"},
        ),
        (
            "glb",
            "http://renderer/model.glb",
            {"type": "TD_MODEL", "modelUrl": "http://renderer/model.glb"},
        ),
        (
            "mp4",
            "http://renderer/model.mp4",
            {"type": "VIDEO", "videoUrl": "http://renderer/model.mp4"},
        ),
    ],
)
def test_3d_callback_relays_media_result(
    media_type: str,
    media_url: str,
    expected_detail: dict[str, str],
) -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.rendering_3d_relay import Rendering3DRelayRegistry

    sent: list[dict[str, Any]] = []

    async def sender(frame: dict[str, Any]) -> None:
        sent.append(frame)

    registry = Rendering3DRelayRegistry()
    asyncio.run(
        registry.register(
            session_id="session-sentinel",
            connection_id="connection-sentinel",
            number="13800138000",
            sender=sender,
        )
    )
    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(registry))

    with TestClient(app) as client:
        response = client.post(
            "/calling-agent-service/v1/session-sentinel/chat-sentinel/3d-gen-back",
            json={
                "mediaType": media_type,
                "mediaUrl": media_url,
                "image": None,
            },
        )

    assert response.status_code == 200
    assert response.json()["errMessage"] == "success"
    assert response.json()["data"] == {"result": "SUCCESS"}
    assert len(sent) == 1
    assert sent[0]["message"] == "chatResponse"
    assert set(sent[0]) == {"message", "body"}
    body = json.loads(sent[0]["body"])
    assert body["chatIndex"] == "chat-sentinel"
    assert body["number"] == "13800138000"
    assert body["messageType"] == "ANSWER"
    assert body["display_only"] is False
    assert body["message"]["type"] == "BRIEF"
    assert body["message"]["chatIndex"] == "chat-sentinel"
    content = body["message"]["content"]
    assert content["intentExecution"] == {
        "description": "",
        "plans": [],
        "messageType": "ANSWER",
    }
    assert content["intentResult"]["status"] == "SUCCESS"
    assert content["intentResult"]["plan"] == []
    assert content["intentResult"]["messageType"] == "ANSWER"
    assert content["intentResult"]["detail"] == [expected_detail]
    assert content["intentWeb"] == {
        "description": "",
        "resourceType": "",
        "resourceUrl": "",
    }


def test_3d_callback_without_active_connection_does_not_ack_success() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.rendering_3d_relay import Rendering3DRelayRegistry

    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(Rendering3DRelayRegistry()))

    with TestClient(app) as client:
        response = client.post(
            "/calling-agent-service/v1/missing-session/chat-sentinel/3d-gen-back",
            json={
                "mediaType": "glb",
                "mediaUrl": "http://renderer/model.glb",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["errCode"] == 1


def test_3d_callback_send_failure_does_not_ack_success() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.rendering_3d_relay import Rendering3DRelayRegistry

    async def sender(frame: dict[str, Any]) -> None:
        _ = frame
        raise RuntimeError("socket-sentinel")

    registry = Rendering3DRelayRegistry()
    asyncio.run(
        registry.register(
            session_id="session-sentinel",
            connection_id="connection-sentinel",
            number="13800138000",
            sender=sender,
        )
    )
    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(registry))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/calling-agent-service/v1/session-sentinel/chat-sentinel/3d-gen-back",
            json={
                "mediaType": "mp4",
                "mediaUrl": "http://renderer/model.mp4",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"]["errCode"] == 1


def test_3d_callback_rejects_unsupported_media_type() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    from assistant_agent.media.rendering_3d_relay import Rendering3DRelayRegistry

    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router(Rendering3DRelayRegistry()))

    with TestClient(app) as client:
        response = client.post(
            "/calling-agent-service/v1/session-sentinel/chat-sentinel/3d-gen-back",
            json={
                "mediaType": "image",
                "mediaUrl": "http://renderer/preview.png",
            },
        )

    assert response.status_code == 422


def test_rendering_3d_relay_registry_tracks_current_connection() -> None:
    from assistant_agent.media.rendering_3d_relay import (
        Rendering3DRelayRegistry,
        Rendering3DRelayUnavailable,
    )

    async def scenario() -> None:
        first_sent: list[dict[str, Any]] = []
        second_sent: list[dict[str, Any]] = []

        async def first_sender(frame: dict[str, Any]) -> None:
            first_sent.append(frame)

        async def second_sender(frame: dict[str, Any]) -> None:
            second_sent.append(frame)

        registry = Rendering3DRelayRegistry()
        await registry.register(
            session_id="session-sentinel",
            connection_id="connection-1",
            number="13800138000",
            sender=first_sender,
        )
        first_binding = await registry.send(
            "session-sentinel",
            lambda active: {
                "message": "chatResponse",
                "body": json.dumps({"number": active.number}),
            },
        )
        assert first_binding.connection_id == "connection-1"
        assert json.loads(first_sent[0]["body"]) == {"number": "13800138000"}

        await registry.register(
            session_id="session-sentinel",
            connection_id="connection-2",
            number="13900139000",
            sender=second_sender,
        )
        await registry.unregister(
            session_id="session-sentinel",
            connection_id="connection-1",
        )
        second_binding = await registry.send(
            "session-sentinel",
            lambda active: {
                "message": "chatResponse",
                "body": json.dumps({"number": active.number}),
            },
        )
        assert second_binding.connection_id == "connection-2"
        assert json.loads(second_sent[0]["body"]) == {"number": "13900139000"}

        await registry.unregister(
            session_id="session-sentinel",
            connection_id="connection-2",
        )
        with pytest.raises(Rendering3DRelayUnavailable):
            await registry.send("session-sentinel", lambda active: {})

    asyncio.run(scenario())


def test_image_to_3d_tool_uses_runtime_owned_identity() -> None:
    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool

    calls: list[dict[str, str]] = []

    class Adapter:
        def start(
            self,
            *,
            session_id: str,
            chat_index: str,
            src_image: str,
            output_format: str,
        ):
            calls.append(
                {
                    "session_id": session_id,
                    "chat_index": chat_index,
                    "src_image": src_image,
                    "output_format": output_format,
                }
            )
            return ImageTo3DSubmission(
                status="generating",
                source_image_id="cake_001",
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"src_image": "cake_001"},
        ToolContext(
            user_id="13800138000",
            session_id="session-sentinel",
            run_id="run-sentinel",
            metadata={
                "request_metadata": {
                    "transport": "agent_service_websocket",
                    "agent_service": {"chat_index": "chat-sentinel"},
                }
            },
        ),
    )

    assert result.success is True
    assert result.data == {
        "status": "generating",
        "source_image_id": "cake_001",
    }
    assert calls == [
        {
            "session_id": "session-sentinel",
            "chat_index": "chat-sentinel",
            "src_image": "cake_001",
            "output_format": "mp4",
        }
    ]


def test_image_to_3d_exposes_optional_src_image_parameter() -> None:
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
        ImageTo3DTool,
        MockImageTo3DAdapter,
    )

    schema = ImageTo3DTool(adapter=MockImageTo3DAdapter()).input_schema.model_json_schema()

    assert "src_image" not in schema.get("required", [])
    assert any(
        option.get("type") == "string"
        for option in schema["properties"]["src_image"]["anyOf"]
    )
    assert "format" not in schema["properties"]


def test_image_to_3d_defaults_to_latest_generated_image() -> None:
    from assistant_agent.runtime.state import AgentState
    from assistant_agent.runtime.tool_executor import ToolExecutor
    from assistant_agent.runtime.requests import UserRequest
    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.models import ToolResult
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool
    from assistant_agent.tools.registry import ToolRegistry

    calls: list[dict[str, str]] = []

    class Adapter:
        def start(
            self,
            *,
            session_id: str,
            chat_index: str,
            src_image: str,
            output_format: str,
        ):
            _ = chat_index
            calls.append(
                {
                    "session_id": session_id,
                    "src_image": src_image,
                    "output_format": output_format,
                }
            )
            return ImageTo3DSubmission(
                status="generating",
                source_image_id=src_image,
            )

    registry = ToolRegistry()
    registry.register(ImageTo3DTool(adapter=Adapter()))
    registry.seal()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="生成3D",
        )
    )
    state.tool_results.append(
        ToolResult(
            tool_name="image_generation",
            success=True,
            data={"image_id": ["generated-cake"]},
            output_ref="/artifacts/generated/generated-cake.png",
        )
    )

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-sentinel",
        "image_to_3d",
        {},
        failure_mode="continue_to_model",
    )

    assert result.success is True
    assert calls == [
        {
            "session_id": "session-sentinel",
            "src_image": "generated-cake",
            "output_format": "mp4",
        }
    ]


def test_image_to_3d_defaults_to_previous_agent_service_turn_image() -> None:
    from assistant_agent.runtime.state import AgentState
    from assistant_agent.runtime.tool_executor import ToolExecutor
    from assistant_agent.runtime.requests import UserRequest
    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool
    from assistant_agent.tools.registry import ToolRegistry

    calls: list[str] = []

    class Adapter:
        def start(
            self,
            *,
            session_id: str,
            chat_index: str,
            src_image: str,
            output_format: str,
        ):
            _ = (session_id, chat_index, output_format)
            calls.append(src_image)
            return ImageTo3DSubmission(
                status="queued",
                source_image_id=src_image,
            )

    registry = ToolRegistry()
    registry.register(ImageTo3DTool(adapter=Adapter()))
    registry.seal()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="继续生成3D",
            metadata={
                "agent_service": {
                    "latest_generated_image_id": "previous-turn-cake",
                }
            },
        )
    )

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-sentinel",
        "image_to_3d",
        {},
        failure_mode="continue_to_model",
    )

    assert result.success is True
    assert calls == ["previous-turn-cake"]


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


def test_image_to_3d_tool_does_not_require_media_relay_entry() -> None:
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
        ImageTo3DTool,
        MockImageTo3DAdapter,
    )

    result = ImageTo3DTool(adapter=MockImageTo3DAdapter()).run(
        {"src_image": "cake_001"},
        ToolContext(
            user_id="13800138000",
            session_id="session-sentinel",
            metadata={"request_metadata": {"transport": "http"}},
        ),
    )

    assert result.success is True
    assert result.data["status"] == "generating"


def test_image_to_3d_tool_treats_failed_service_status_as_failure() -> None:
    from assistant_agent.media.image_to_3d import ImageTo3DSubmission
    from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool

    class Adapter:
        def start(
            self,
            *,
            session_id: str,
            chat_index: str,
            src_image: str,
            output_format: str,
        ):
            _ = (session_id, chat_index, src_image, output_format)
            return ImageTo3DSubmission(
                status="failed",
                source_image_id="cake_001",
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"src_image": "cake_001"},
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
        "source_image_id": "cake_001",
    }


def test_3d_callback_requires_media_type_and_url() -> None:
    from assistant_agent.api.rendering_3d_callback import create_rendering_3d_callback_router
    app = FastAPI()
    app.include_router(create_rendering_3d_callback_router())

    with TestClient(app) as client:
        response = client.post(
            "/calling-agent-service/v1/session-video-sentinel/0/3d-gen-back",
            json={"mediaType": "mp4"},
        )

    assert response.status_code == 422
