from __future__ import annotations

from pathlib import Path

from assistant_agent.context.compaction import project_observations_for_context
from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _QueryAnsweringVisionClient:
    def __init__(self) -> None:
        self.requests: list[VisionUnderstandingRequest] = []

    def understand(
        self,
        request: VisionUnderstandingRequest,
    ) -> VisionUnderstandingResult:
        self.requests.append(request.model_copy(deep=True))
        return VisionUnderstandingResult(
            summary=f"基于最新帧回答：{request.user_query}",
            provider="query-vlm",
            model="query-vlm-sentinel",
            output_ref="provider://query-vlm/latest-frame",
        )


def _record(
    pool: SessionVisualSemanticStorePool,
    tmp_path: Path,
    *,
    sequence: int,
) -> Path:
    evidence = tmp_path / f"frame-{sequence}.jpg"
    evidence.write_bytes(f"frame-{sequence}".encode())
    pool.resolve("user-1", "session-1").record_success(
        VisualSemanticRecord(
            record_id=f"record-{sequence}",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 1_000,
            summary=f"后台通用描述-{sequence}",
            index_status="unavailable",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=sequence * 1_000 + 10,
        )
    )
    return evidence


def test_live_view_exposes_query_and_vlm_answers_it_from_latest_target_frame(
    tmp_path: Path,
) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    _record(pool, tmp_path, sequence=1)
    _record(pool, tmp_path, sequence=2)
    client = _QueryAnsweringVisionClient()
    registry = ToolRegistry()
    registry.register(
        LiveViewInspectTool(client=client, semantic_store_pool=pool)
    )
    registry.seal()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-1",
            session_id="session-1",
            text="用户原始指令",
            video_ids=["video-1"],
            metadata={
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": 2},
            },
        )
    )

    schema = registry.get_spec(LIVE_VIEW_INSPECT_TOOL_NAME).input_schema
    assert set(schema["properties"]) == {"query"}
    assert schema["required"] == ["query"]

    trace_store = InMemoryTraceStore()
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-live-view",
        LIVE_VIEW_INSPECT_TOOL_NAME,
        {"query": "杯子是什么颜色？"},
        trace_store=trace_store,
        trace_id=state.trace_id,
    )

    assert result.success is True
    assert result.model_observation is not None
    assert result.model_observation["summary"] == "基于最新帧回答：杯子是什么颜色？"
    assert len(client.requests) == 1
    assert client.requests[0].user_query == "杯子是什么颜色？"
    assert len(client.requests[0].frame_refs) == 1
    assert Path(client.requests[0].frame_refs[0]).read_bytes() == b"frame-2"
    assert client.requests[0].metadata["frame_sequence"] == 2
    vlm_finished = next(
        event
        for event in trace_store.events
        if event.canonical_event == "vlm.infer.finished"
    )
    assert vlm_finished.attributes["query_provided"] is True


def test_context_projection_does_not_drop_safe_list_elements_by_fixed_count() -> None:
    observation = {
        "tool_name": "probe_tool",
        "status": "success",
        "summary": "八条安全文本",
        "data": {
            "observations": [
                {"timestamp_ms": index * 1_000, "text": f"text-{index}"}
                for index in range(8)
            ]
        },
    }

    projected = project_observations_for_context([observation])[0]

    assert projected["data"]["observations"] == observation["data"]["observations"]
    assert "max_items_per_list" not in projected["compaction"]
