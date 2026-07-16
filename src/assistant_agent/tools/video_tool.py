"""Video understanding tool backed by a video adapter."""

from collections.abc import Callable
from time import time

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.video_adapter import (
    VideoUnderstandingAdapter,
    create_video_understanding_adapter,
)
from assistant_agent.services.video_context import DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE, VideoContextStore
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore, RealtimeVideoSnapshot
from assistant_agent.tools.base import MockTool, ToolContext


class VideoUnderstandingTool(MockTool):
    name = "video_understanding"
    description = (
        "查询当前实时镜头或显式视频引用中的视觉事实。当前 turn 已有 active video 时，"
        "可只提供 user_query，由运行时绑定当前 turn 的视频引用；普通上传/API 场景使用 "
        "video_ref or video_ids。不要传内部帧路径、JPEG/base64、本地文件路径或 Provider 字段。"
        "使用当前 turn 的视频引用和工具结果回答；证据不足、过期或工具结果不确定时按结果表达不确定，"
        "不要编造当前画面。"
    )
    input_schema = VideoUnderstandingRequest
    output_schema = VideoUnderstandingResult

    def __init__(
        self,
        adapter: VideoUnderstandingAdapter | None = None,
        *,
        context_store: VideoContextStore | None = None,
        memory_store: RealtimeVideoMemoryStore | None = None,
        context_window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.adapter = adapter or create_video_understanding_adapter()
        self.context_store = context_store
        self.memory_store = memory_store
        self.context_window_size = context_window_size
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))

    def _run(self, input: VideoUnderstandingRequest, context: ToolContext) -> ToolResult:
        video_ref = input.video_ref or (input.video_ids[0] if input.video_ids else None)
        observation_mode = context.metadata.get("realtime_video_observation") is True
        snapshot = None
        if video_ref and not observation_mode and self.memory_store is not None:
            snapshot = self.memory_store.snapshot(video_ref)
            if snapshot is not None and snapshot.healthy:
                return self._memory_result(snapshot)

        input = self._with_context_frames(input)
        try:
            result = self.adapter.understand_video(input)
        except ValueError as exc:
            contract = build_capability_output_contract(
                capability="video_understanding",
                status="failed",
                errors=[{"code": _error_code(str(exc)), "message": str(exc), "recoverable": True}],
            )
            return ToolResult(tool_name=self.name, success=False, error=str(exc), contract=contract)

        source = "background_keyframe_observation" if observation_mode else "recent_frame_fallback"
        payload = {**result.model_dump(mode="json"), "source": source}
        output_ref = result.output_ref
        status = "failed" if result.errors else "succeeded"
        contract = build_capability_output_contract(
            capability="video_understanding",
            status=status,
            output_ref=output_ref,
            data=payload,
            errors=result.errors,
            metadata={
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "source": source,
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=not result.errors,
            data=payload,
            error=result.errors[0]["message"] if result.errors else None,
            output_ref=output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
            trace_summary=self._trace_summary(
                source=source,
                snapshot=snapshot,
                provider=result.provider,
                model=result.model,
            ),
        )

    def _memory_result(self, snapshot: RealtimeVideoSnapshot) -> ToolResult:
        output_ref = f"memory://realtime-video/{_safe_ref(snapshot.video_id)}"
        payload = {
            "summary": snapshot.current_state,
            "objects": list(snapshot.objects),
            "people": list(snapshot.people),
            "actions": list(snapshot.actions),
            "events": list(snapshot.events),
            "scene": snapshot.scene,
            "products": list(snapshot.products),
            "brands": list(snapshot.brands),
            "colors": list(snapshot.colors),
            "materials": list(snapshot.materials),
            "text_in_video": list(snapshot.text_in_video),
            "timestamps": [dict(item) for item in snapshot.timestamps],
            "style_tags": list(snapshot.style_tags),
            "confidence": snapshot.confidence,
            "provider": snapshot.provider or "rolling_video_memory",
            "model": snapshot.model,
            "output_ref": output_ref,
            "errors": [],
            "latency_ms": 0,
            "source": "rolling_video_memory",
            "snapshot_sequence": snapshot.last_success_sequence,
            "observed_timestamp_ms": snapshot.last_success_timestamp_ms,
            "keyframe_count": len(snapshot.keyframes),
        }
        contract = build_capability_output_contract(
            capability="video_understanding",
            status="succeeded",
            output_ref=output_ref,
            data=payload,
            metadata={
                "provider": payload["provider"],
                "model": payload["model"],
                "latency_ms": 0,
                "source": "rolling_video_memory",
                "snapshot_sequence": snapshot.last_success_sequence,
                "keyframe_count": len(snapshot.keyframes),
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=payload,
            output_ref=output_ref,
            latency_ms=0,
            contract=contract,
            trace_summary=self._trace_summary(
                source="rolling_video_memory",
                snapshot=snapshot,
                provider=snapshot.provider or "rolling_video_memory",
                model=snapshot.model,
            ),
        )

    def _trace_summary(
        self,
        *,
        source: str,
        snapshot: RealtimeVideoSnapshot | None,
        provider: str | None,
        model: str | None,
    ) -> dict[str, object]:
        diagnostics = snapshot.observation_diagnostics if snapshot is not None else None
        published_at_ms = diagnostics.published_at_ms if diagnostics is not None else None
        snapshot_age_ms = (
            max(0, self.wall_clock_ms() - published_at_ms)
            if published_at_ms is not None
            else None
        )
        return {
            "source": source,
            "snapshot_age_ms": snapshot_age_ms,
            "observation_latency_ms": (
                diagnostics.observation_latency_ms if diagnostics is not None else None
            ),
            "pending_count": snapshot.pending_count if snapshot is not None else 0,
            "in_flight": snapshot.in_flight if snapshot is not None else False,
            "fallback_used": source == "recent_frame_fallback",
            "snapshot_sequence": snapshot.last_success_sequence if snapshot is not None else None,
            "provider": provider,
            "model": model,
        }

    def _with_context_frames(self, input: VideoUnderstandingRequest) -> VideoUnderstandingRequest:
        video_ref = input.video_ref or (input.video_ids[0] if input.video_ids else None)
        if not video_ref:
            return input
        if input.frame_refs or self.context_store is None:
            return input.model_copy(update={"video_ref": video_ref})
        limit = input.max_frames or self.context_window_size
        frames = self.context_store.get_recent_frames(video_ref, limit=limit)
        if not frames:
            return input.model_copy(update={"video_ref": video_ref})
        metadata = {
            **input.metadata,
            "context_window_size": self.context_window_size,
            "context_frame_count": len(frames),
            "context_frame_ids": [frame.frame_id for frame in frames],
        }
        return input.model_copy(
            update={
                "video_ref": video_ref,
                "context_id": video_ref,
                "frame_refs": [frame.uri for frame in frames],
                "metadata": metadata,
            }
        )


def _error_code(message: str) -> str:
    if ":" in message:
        return message.split(":", maxsplit=1)[0]
    return "video_understanding_failed"


def _safe_ref(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return normalized or "video"
