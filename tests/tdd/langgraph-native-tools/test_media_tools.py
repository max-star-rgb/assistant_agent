from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

from langchain.agents import AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.media.embedding.coordinator_store import SessionEmbeddingCoordinatorStore
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore, SemanticKeyframeRecord
from assistant_agent.media.video.semantic_store_pool import SessionVisualSemanticStorePool
from assistant_agent.media.video.visual_memory_index import UnavailableVisualMemoryTextIndex
from assistant_agent.media.video.visual_reminder import VisualReminderManager, VisualReminderRegistry
from assistant_agent.media.visual_perception.module import LiveViewProjection
from assistant_agent.media.video.understanding_service import VideoUnderstandingService
from assistant_agent.media.vision.models import VideoInspectionOutcome, VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.native_agent.context import AssistantRunContext, AssistantRuntimeFacts, assistant_runtime_metadata
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import create_uploaded_media_inspect_tool
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import create_visual_memory_search_tool
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import create_visual_reminder_manage_tool


class _SuccessfulClient:
    def understand(self, _request):  # type: ignore[no-untyped-def]
        from assistant_agent.media.vision.models import VisionUnderstandingResult

        return VisionUnderstandingResult(
            summary="一只猫在窗边。",
            objects=["猫"],
            provider="mock",
            output_ref="mock://visual/cat",
        )


class _FailingClient:
    def understand(self, _request):  # type: ignore[no-untyped-def]
        raise ValueError("provider_unavailable: api_key=media-secret-sentinel")


class _User(dict):
    identity = "user"
    permissions = ()


def test_video_understanding_service_is_domain_only_and_projects_outcomes() -> None:
    request = VideoUnderstandingRequest(video_ref="uploaded-video", user_id="user", session_id="thread")
    succeeded = VideoUnderstandingService(client=_SuccessfulClient()).inspect(request)
    failed = VideoUnderstandingService(client=_FailingClient()).inspect(request)

    assert isinstance(succeeded, VideoInspectionOutcome)
    assert succeeded.status == "succeeded"
    assert succeeded.data["summary"] == "一只猫在窗边。"
    assert succeeded.model_observation["summary"] == "一只猫在窗边。"
    assert succeeded.model_observation["objects"] == ["猫"]
    assert failed.status == "failed"
    assert failed.error is not None
    assert "media-secret-sentinel" not in failed.error

    module_path = Path(__file__).parents[3] / "src/assistant_agent/media/video/understanding_service.py"
    imports = _imported_names(module_path)
    assert not any(name == "assistant_agent.tools" or name.startswith("assistant_agent.tools.") for name in imports)
    assert not (Path(__file__).parents[3] / "src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py").exists()


def test_live_service_reads_exact_target_or_returns_bounded_unavailable() -> None:
    memory = RealtimeVideoMemoryStore()
    memory.record_success(
        "video-1", SemanticKeyframeRecord(frame_id="frame-3", uri="memory://frame", sequence=3, timestamp_ms=300),
        VideoUnderstandingResult(summary="目标画面", provider="mock", output_ref="mock://target"),
    )
    metadata = {
        "entry_profile": "agent_service", "visual_target_sequence": 3,
        "visual_window_start_sequence": 3, "visual_window_sequences": (3,), "visual_window_id": "window-1",
    }
    ready = VideoUnderstandingService(client=_SuccessfulClient(), memory_store=memory).inspect(
        VideoUnderstandingRequest(video_ref="video-1", user_id="user", session_id="thread", metadata=metadata)
    )
    unavailable = VideoUnderstandingService(client=_SuccessfulClient()).inspect(
        VideoUnderstandingRequest(video_ref="video-1", user_id="user", session_id="thread", metadata=metadata)
    )

    assert ready.status == "succeeded"
    assert ready.data["target_ready"] is True
    assert ready.model_observation["vlm_response"] == "目标画面"
    assert unavailable.status == "partial"
    assert unavailable.data["usable_visual_text"] is False
    assert len(unavailable.model_observation["vlm_response"]) <= 200


def test_uploaded_media_toolnode_projects_image_and_video_and_redacts_schema_input() -> None:
    image = _invoke(
        create_uploaded_media_inspect_tool(_SuccessfulClient()),
        {"question": "图片是什么？"},
        [{"type": "image", "source": "uploaded", "id": "image-1"}],
    )
    video = _invoke(
        create_uploaded_media_inspect_tool(_SuccessfulClient()),
        {"question": "视频是什么？"},
        [{"type": "video", "source": "uploaded", "id": "video-1"}],
    )
    invalid = _invoke(
        create_uploaded_media_inspect_tool(_SuccessfulClient()),
        {"question": {"raw": "media-schema-sentinel"}},
        [{"type": "image", "source": "uploaded", "id": "image-1"}],
    )
    failed = _invoke(
        create_uploaded_media_inspect_tool(_FailingClient()),
        {"question": "图片是什么？"},
        [{"type": "image", "source": "uploaded", "id": "image-1"}],
    )

    assert image.status == video.status == "success"
    assert json.loads(image.content[0]["text"])["summary"] == "一只猫在窗边。"
    assert image.artifact["output_ref"] == "mock://visual/cat"
    assert json.loads(video.content[0]["text"])["summary"] == "一只猫在窗边。"
    assert video.artifact["media_kind"] == "explicit_video"
    assert invalid.status == "error"
    assert "media-schema-sentinel" not in str(invalid.content)
    assert failed.status == "error"
    assert "media-secret-sentinel" not in str(failed.content)


def test_visual_memory_and_reminder_toolnodes_keep_content_artifact_and_metadata(tmp_path: Path) -> None:
    projection = LiveViewProjection(
        live_video_ids=("video-1",), window_id="window-1", window_start_sequence=1,
        target_sequence=1, target_video_id="video-1",
    )
    pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic")
    memory = create_visual_memory_search_tool(
        semantic_store_pool=pool,
        text_index=UnavailableVisualMemoryTextIndex(code="offline", message="offline"),
        live_view_resolver=lambda *_: projection,
    )
    registry = VisualReminderRegistry()
    registry.register(VisualReminderManager(user_id="user", session_id="thread"))
    reminder = create_visual_reminder_manage_tool(
        coordinator_store=SessionEmbeddingCoordinatorStore(factory=lambda *_: None),  # type: ignore[arg-type]
        reminder_registry=registry,
    )
    try:
        history = _invoke(memory, {"query": "钥匙"})
        listed = _invoke(reminder, {"action": "list"})
    finally:
        pool.close()

    assert history.status == listed.status == "success"
    assert json.loads(history.content[0]["text"])["status"] == "empty"
    assert history.artifact["status"] == "empty"
    assert json.loads(listed.content[0]["text"])["count"] == 0
    assert listed.artifact["reminders"] == []
    assert memory.name == "visual_memory_search"
    assert reminder.name == "visual_reminder_manage"
    assert memory.metadata["availability"] == ToolAvailability.VISUAL_HISTORY_AVAILABLE.value
    assert reminder.metadata["availability"] == ToolAvailability.VIDEO_FRAME_RECEIVED.value


def test_media_tool_modules_are_direct_handlers() -> None:
    root = Path(__file__).parents[3] / "src"
    for relative in (
        "assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py",
        "assistant_agent/tools/plugins/builtin/media_inspection/live_tool.py",
        "assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py",
        "assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py",
    ):
        imports = _imported_names(root / relative)
        assert "assistant_agent.tools.models" not in imports
        assert "assistant_agent.tools.runtime" not in imports


def _invoke(
    tool: BaseTool,
    args: dict[str, object],
    content: list[dict[str, object]] | None = None,
) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool], handle_tool_errors=lambda error: str(error)))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = asyncio.run(builder.compile().ainvoke(
        {"messages": [
            *([HumanMessage(content=content)] if content is not None else []),
            AIMessage(content="", tool_calls=[{"name": tool.name, "args": args, "id": "call", "type": "tool_call"}]),
        ]},
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "thread", "run_id": "run", "langgraph_auth_user": _User()},
                "metadata": assistant_runtime_metadata(AssistantRuntimeFacts(visual_capability_token="capability"))},
    ))
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names
