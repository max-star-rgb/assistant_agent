from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from deepagents.backends import FilesystemBackend, LocalShellBackend
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, ToolException
from langgraph.prebuilt import ToolRuntime
from langgraph.runtime import Runtime

from assistant_agent.config.models import MediaConfig
from assistant_agent.media.runtime_media import (
    latest_runtime_media,
    without_uploaded_media_messages,
)
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.conditional_tool_exposure import (
    ConditionalToolExposureMiddleware,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.assistant_agent import (
    RuntimeConfigurableSummarizationMiddleware,
    build_assistant_agent,
    build_general_purpose_worker,
)
from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import (
    create_uploaded_media_inspect_tool,
)


class _FrameCheckingVisionClient:
    traces_as_chat_model = False

    def understand(
        self,
        request: VisionUnderstandingRequest,
        *,
        config=None,
    ) -> VisionUnderstandingResult:
        del config
        assert request.frame_refs == []
        assert request.video_ids == []
        assert 1 <= len(request.image_ids) <= 5
        assert all(
            Path(frame).read_bytes().startswith(b"\xff\xd8")
            for frame in request.image_ids
        )
        return VisionUnderstandingResult(
            summary="video understood",
            provider="test",
            output_ref="test://video",
            media_kind="explicit_video",
        )


class _UploadedMediaModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        observation = next(
            (
                message
                for message in messages
                if isinstance(message, ToolMessage)
                and message.name == "uploaded_media_inspect"
            ),
            None,
        )
        if observation is not None:
            if observation.status == "error":
                return AIMessage(content=f"final error: {observation.content}")
            summary = json.loads(observation.content[0]["text"])["summary"]
            return AIMessage(content=f"final: {summary}")
        tool_names = {
            item["function"]["name"]
            for item in kwargs.get("tools", [])
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        }
        if "uploaded_media_inspect" not in tool_names:
            return AIMessage(content="uploaded tool missing")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "uploaded_media_inspect",
                    "args": {"question": "describe it"},
                    "id": "uploaded-media-call",
                    "type": "tool_call",
                }
            ],
        )


class _ImageVisionClient:
    traces_as_chat_model = False

    def __init__(self) -> None:
        self.seen_ref: str | None = None

    def understand(
        self,
        request: VisionUnderstandingRequest,
        *,
        config=None,
    ) -> VisionUnderstandingResult:
        del config
        self.seen_ref = request.image_ids[0]
        if Path(self.seen_ref).read_bytes() != b"image-sentinel":
            raise ValueError("missing image data")
        return VisionUnderstandingResult(
            summary="vision evidence",
            provider="test",
            output_ref="test://image",
            media_kind="image",
            media_refs=["data:image/png;base64,secret"],
        )


def test_studio_image_block_is_treated_as_uploaded_media() -> None:
    encoded = base64.b64encode(b"image-sentinel").decode("ascii")
    media = latest_runtime_media(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "base64": encoded,
                            "mime_type": "image/png",
                        },
                    ]
                )
            ]
        }
    )

    assert media.uploaded_image_ids == (f"data:image/png;base64,{encoded}",)


def test_legacy_studio_image_url_data_block_is_treated_as_uploaded_media() -> None:
    encoded = base64.b64encode(b"image-sentinel").decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"

    media = latest_runtime_media(
        {
            "messages": [
                HumanMessage(
                    content=[{"type": "image_url", "image_url": {"url": data_url}}]
                )
            ]
        }
    )

    assert media.uploaded_image_ids == (data_url,)


def test_studio_video_block_is_treated_as_uploaded_media() -> None:
    encoded = base64.b64encode(b"video-sentinel").decode("ascii")
    media = latest_runtime_media(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {
                            "type": "video",
                            "base64": encoded,
                            "mime_type": "video/mp4",
                        }
                    ]
                )
            ]
        }
    )

    assert media.uploaded_video_ids == (f"data:video/mp4;base64,{encoded}",)


def test_live_camera_block_is_not_treated_as_uploaded_media() -> None:
    media = latest_runtime_media(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {
                            "type": "video",
                            "id": "live-video-sentinel",
                            "source": "live_camera",
                        }
                    ]
                )
            ]
        }
    )

    assert media.uploaded_video_ids == ()
    assert media.live_video_ids == ("live-video-sentinel",)


def test_non_video_file_block_does_not_expose_video_inspection() -> None:
    media = latest_runtime_media(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {
                            "type": "file",
                            "url": "https://example.com/report.pdf",
                            "mime_type": "application/pdf",
                        }
                    ]
                )
            ]
        }
    )

    assert media.has_uploaded_media is False


def test_uploaded_mp4_is_sampled_before_vlm_call(tmp_path: Path) -> None:
    video_path = tmp_path / "uploaded.mp4"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:d=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video_path),
        ],
        check=True,
    )
    encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": "summarize it"},
            {
                "type": "video",
                "base64": encoded,
                "mime_type": "video/mp4",
            },
        ]
    )
    tool = create_uploaded_media_inspect_tool(_FrameCheckingVisionClient())
    runtime = ToolRuntime(
        state={"messages": [message]},
        context=AssistantRunContext(),
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="tool-call-sentinel",
        store=None,
        tools=[tool],
        execution_info=SimpleNamespace(
            thread_id="thread-sentinel",
            run_id="run-sentinel",
        ),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-sentinel")),
    )

    content, artifact = tool.func(question="summarize it", runtime=runtime)

    assert json.loads(content[0]["text"])["summary"] == "video understood"
    assert artifact["media_kind"] == "explicit_video"


def test_uploaded_video_honors_configured_duration_limit(tmp_path: Path) -> None:
    video_path = tmp_path / "too-long.mp4"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:d=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video_path),
        ],
        check=True,
    )
    encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
    message = HumanMessage(
        content=[
            {
                "type": "video",
                "base64": encoded,
                "mime_type": "video/mp4",
            }
        ]
    )
    tool = create_uploaded_media_inspect_tool(
        _FrameCheckingVisionClient(),
        max_video_seconds=1,
    )
    runtime = ToolRuntime(
        state={"messages": [message]},
        context=AssistantRunContext(),
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="tool-call-sentinel",
        store=None,
        tools=[tool],
        execution_info=SimpleNamespace(thread_id="thread-sentinel", run_id=None),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-sentinel")),
    )

    try:
        tool.func(question="summarize it", runtime=runtime)
    except ToolException as exc:
        assert "uploaded_video_duration_invalid" in str(exc)
    else:
        raise AssertionError("video over configured duration was accepted")


def test_main_model_sees_text_and_tool_but_not_uploaded_binary() -> None:
    encoded = base64.b64encode(b"image-sentinel").decode("ascii")
    human = HumanMessage(
        content=[
            {"type": "text", "text": "describe it"},
            {
                "type": "image",
                "base64": encoded,
                "mime_type": "image/png",
            },
            {"type": "file", "source": "uploaded", "id": "legacy-video-id"},
        ]
    )
    tool = create_uploaded_media_inspect_tool(_FrameCheckingVisionClient())
    request = ModelRequest(
        model=MockAssistantChatModel(),
        messages=[human],
        tools=[tool],
        state={"messages": [human]},
        runtime=Runtime(context=AssistantRunContext()),
    )
    observed: dict[str, object] = {}

    def handler(updated: ModelRequest) -> ModelResponse:
        observed["content"] = updated.messages[-1].content
        observed["tools"] = [
            item.name for item in updated.tools if isinstance(item, BaseTool)
        ]
        return ModelResponse(result=[AIMessage(content="handled")])

    ConditionalToolExposureMiddleware().wrap_model_call(request, handler)

    assert observed == {
        "content": [{"type": "text", "text": "describe it"}],
        "tools": ["uploaded_media_inspect"],
    }


def test_invalid_uploaded_base64_is_still_removed_from_model_and_memory() -> None:
    human = HumanMessage(
        content=[
            {"type": "text", "text": "describe it"},
            {"type": "image", "base64": "not-base64!", "mime_type": "image/png"},
            {"type": "file", "source": "uploaded", "id": "legacy-video-id"},
        ]
    )

    sanitized = without_uploaded_media_messages([human])

    assert sanitized[0].content == [{"type": "text", "text": "describe it"}]
    assert human.content[1]["base64"] == "not-base64!"
    assert human.content[2]["id"] == "legacy-video-id"


def test_summarizer_never_receives_uploaded_binary(tmp_path: Path) -> None:
    human = HumanMessage(
        content=[
            {"type": "text", "text": "describe it"},
            {"type": "file", "source": "uploaded", "id": "legacy-video-id"},
        ]
    )
    middleware = RuntimeConfigurableSummarizationMiddleware(
        MockAssistantChatModel(),
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        trigger=("tokens", 100),
        keep=("tokens", 20),
        token_counter=lambda messages, **_kwargs: len(list(messages)),
        trim_tokens_to_summarize=None,
    )
    request = ModelRequest(
        model=MockAssistantChatModel(),
        messages=[human],
        tools=[],
        state={"messages": [human]},
        runtime=Runtime(context=AssistantRunContext()),
    )
    observed: list[object] = []

    def handler(updated: ModelRequest) -> ModelResponse:
        observed.append(updated.messages[-1].content)
        return ModelResponse(result=[AIMessage(content="handled")])

    middleware.wrap_model_call(request, handler)

    assert observed == [[{"type": "text", "text": "describe it"}]]


def test_standard_image_block_completes_native_graph_tool_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool.authenticated_user_identity",
        lambda _runtime: "user-sentinel",
    )
    model = _UploadedMediaModel()
    client = _ImageVisionClient()
    tool = create_uploaded_media_inspect_tool(client)
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=True)
    skills_backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    worker = build_general_purpose_worker(
        model,
        [tool],
        backend=backend,
        skills_backend=skills_backend,
    )
    graph = build_assistant_agent(
        model,
        [tool],
        backend=backend,
        worker_graph=worker,
        skills_backend=skills_backend,
    )
    encoded = base64.b64encode(b"image-sentinel").decode("ascii")

    result = graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {"type": "text", "text": "describe it"},
                        {
                            "type": "image",
                            "base64": encoded,
                            "mime_type": "image/png",
                        },
                    ]
                )
            ]
        },
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "studio-upload-thread"}},
    )

    assert result["messages"][-1].content == "final: vision evidence"
    observation = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "uploaded_media_inspect"
    )
    assert "media_refs" not in observation.content[0]["text"]
    assert "media_refs" not in observation.artifact
    assert client.seen_ref is not None and not Path(client.seen_ref).exists()


def test_invalid_image_is_a_bounded_toolnode_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool.authenticated_user_identity",
        lambda _runtime: "user-sentinel",
    )
    model = _UploadedMediaModel()
    tool = create_uploaded_media_inspect_tool(_ImageVisionClient())
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=True)
    graph = build_assistant_agent(
        model,
        [tool],
        backend=backend,
        worker_graph=build_general_purpose_worker(
            model,
            [tool],
            backend=backend,
            skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        ),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )

    result = graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content=[
                        {"type": "text", "text": "describe it"},
                        {
                            "type": "image",
                            "base64": "not-base64!",
                            "mime_type": "image/png",
                        },
                    ]
                )
            ]
        },
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "invalid-upload-thread"}},
    )

    assert result["messages"][-1].content.startswith("final error:")
    assert any(
        isinstance(message, ToolMessage) and message.status == "error"
        for message in result["messages"]
    )


def test_media_config_rejects_non_finite_video_limit() -> None:
    try:
        MediaConfig(max_video_seconds=float("nan"))
    except ValueError as exc:
        assert "finite and positive" in str(exc)
    else:
        raise AssertionError("non-finite video limit was accepted")


def test_uploaded_image_cannot_read_outside_run_cwd() -> None:
    message = HumanMessage(
        content=[
            {
                "type": "image",
                "id": "/etc/passwd",
                "source": "uploaded",
            }
        ]
    )
    tool = create_uploaded_media_inspect_tool(_ImageVisionClient())
    runtime = ToolRuntime(
        state={"messages": [message]},
        context=AssistantRunContext(
            cwd=Path("/home/lenovo1/pycharm_project/assistant_agent")
        ),
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="tool-call-sentinel",
        store=None,
        tools=[tool],
        execution_info=SimpleNamespace(thread_id="thread-sentinel", run_id=None),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-sentinel")),
    )

    try:
        tool.func(question="describe it", runtime=runtime)
    except ToolException as exc:
        assert "uploaded_image_ref_invalid" in str(exc)
    else:
        raise AssertionError("outside-cwd image reference was accepted")
