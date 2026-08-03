"""Video understanding Tool backed by a shared video adapter."""

from collections.abc import Callable
from time import time
from typing import Any

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.media.agent_service_entry import (
    is_trusted_agent_service_request,
)
from assistant_agent.media.video.video_adapter import (
    VideoUnderstandingAdapter,
    create_video_understanding_adapter,
)
from assistant_agent.media.vision.vision_client import (
    AdapterVisionUnderstandingClient,
    VisionUnderstandingClient,
    video_result_from_vision_result,
    vision_request_from_video_request,
)
from assistant_agent.media.video.video_context import (
    DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    VideoContextStore,
)
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoSnapshot,
)
from assistant_agent.tools.ids import (
    MEDIA_INSPECT_TOOL_NAME,
    VIDEO_UNDERSTANDING_CAPABILITY,
)
from assistant_agent.tools.base import ToolBase, ToolContext


LIVE_VIEW_SNAPSHOT_WAIT_SECONDS = 10.0


class VideoUnderstandingBranch(ToolBase):
    """Shared explicit-video and governed live-view execution branch."""

    name = MEDIA_INSPECT_TOOL_NAME
    description = (
        "查询当前实时镜头或显式视频引用中的视觉事实。当前 turn 已有 active video 时，"
        "可只提供 user_query，由运行时绑定当前 turn 的视频引用；普通上传/API 场景使用 "
        "video_ref 或 video_ids。不要传内部帧路径、JPEG/base64、本地文件路径或 Provider 字段。"
        "使用当前 turn 的视频引用和工具结果回答；证据不足、过期或工具结果不确定时按结果表达不确定，"
        "不要编造当前画面。"
    )
    input_schema = VideoUnderstandingRequest
    output_schema = VideoUnderstandingResult
    category = "read"
    requires_media = ["video"]

    def __init__(
        self,
        adapter: VideoUnderstandingAdapter | None = None,
        *,
        client: VisionUnderstandingClient | None = None,
        context_store: VideoContextStore | None = None,
        memory_store: RealtimeVideoMemoryStore | None = None,
        context_window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.adapter = (
            adapter
            or getattr(client, "video_adapter", None)
            or create_video_understanding_adapter()
        )
        self.client = client or AdapterVisionUnderstandingClient(video_adapter=self.adapter)
        self.context_store = context_store
        self.memory_store = memory_store
        self.context_window_size = context_window_size
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))

    def _run(
        self, input: VideoUnderstandingRequest, context: ToolContext
    ) -> ToolResult:
        video_ref = input.video_ref or (input.video_ids[0] if input.video_ids else None)
        observation_mode = context.metadata.get("realtime_video_observation") is True
        agent_service_text_only = (
            not observation_mode and _is_agent_service_realtime_video_tool_call(context)
        )
        snapshot = None
        status_snapshot = None
        visual_target_sequence = self._live_target_sequence(context)
        if (
            video_ref
            and agent_service_text_only
            and self.memory_store is not None
        ):
            snapshot = self._live_snapshot_for_request(
                video_ref,
                context=context,
            )
            status_snapshot = snapshot
            if snapshot is None and visual_target_sequence is not None:
                status_snapshot = self.memory_store.snapshot(video_ref)
            if snapshot is not None and (
                snapshot.healthy
                or (
                    agent_service_text_only
                    and snapshot.last_success_sequence is not None
                )
            ):
                return self._memory_result(
                    snapshot,
                    target_sequence=visual_target_sequence,
                )
        if agent_service_text_only:
            status_override = None
            if (
                video_ref
                and visual_target_sequence is not None
                and snapshot is None
                and self.memory_store is not None
            ):
                status_override = (
                    "failed"
                    if self.memory_store.sequence_failed(
                        video_ref,
                        target_sequence=visual_target_sequence,
                    )
                    else "pending"
                )
            return self._memory_unavailable_result(
                video_ref=video_ref,
                snapshot=status_snapshot,
                target_sequence=visual_target_sequence,
                status_override=status_override,
            )

        input = self._with_context_frames(input)
        self._sync_client_video_adapter()
        try:
            result = video_result_from_vision_result(
                self.client.understand(vision_request_from_video_request(input))
            )
        except ValueError as exc:
            contract = build_capability_output_contract(
                capability=VIDEO_UNDERSTANDING_CAPABILITY,
                status="failed",
                errors=[
                    {
                        "code": _error_code(str(exc)),
                        "message": str(exc),
                        "recoverable": True,
                    }
                ],
            )
            return ToolResult(
                tool_name=self.name, success=False, error=str(exc), contract=contract
            )

        source = (
            "background_keyframe_observation"
            if observation_mode
            else "explicit_video"
        )
        payload = {
            **result.model_dump(mode="json"),
            "source": source,
            "media_kind": (
                "live_view" if observation_mode else "explicit_video"
            ),
            "media_refs": [video_ref] if video_ref else [],
        }
        output_ref = result.output_ref
        status = "failed" if result.errors else "succeeded"
        contract = build_capability_output_contract(
            capability=VIDEO_UNDERSTANDING_CAPABILITY,
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
            model_observation=_video_model_observation(payload),
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

    def _live_snapshot_for_request(
        self,
        video_ref: str,
        *,
        context: ToolContext,
    ) -> RealtimeVideoSnapshot | None:
        if self.memory_store is None:
            return None
        target_sequence = self._live_target_sequence(context)
        if target_sequence is not None:
            return self.memory_store.wait_for_snapshot_sequence(
                video_ref,
                target_sequence=target_sequence,
                timeout_seconds=LIVE_VIEW_SNAPSHOT_WAIT_SECONDS,
            )
        return self.memory_store.snapshot(video_ref)

    @staticmethod
    def _live_target_sequence(context: ToolContext) -> int | None:
        request_metadata = context.metadata.get("request_metadata")
        agent_service = (
            request_metadata.get("agent_service")
            if isinstance(request_metadata, dict)
            else None
        )
        target_sequence = (
            agent_service.get("visual_target_sequence")
            if isinstance(agent_service, dict)
            else None
        )
        if (
            not isinstance(target_sequence, bool)
            and isinstance(target_sequence, int)
            and target_sequence >= 0
        ):
            return target_sequence
        return None

    def _sync_client_video_adapter(self) -> None:
        if (
            isinstance(self.client, AdapterVisionUnderstandingClient)
            and self.client.video_adapter is not self.adapter
        ):
            self.client.video_adapter = self.adapter

    def _memory_result(
        self,
        snapshot: RealtimeVideoSnapshot,
        *,
        target_sequence: int | None = None,
    ) -> ToolResult:
        output_ref = f"memory://realtime-video/{_safe_ref(snapshot.video_id)}"
        status = _snapshot_status(snapshot, target_sequence=target_sequence)
        description = _memory_description(snapshot, status=status)
        sequence_gap = _sequence_gap(snapshot, target_sequence=target_sequence)
        payload = {
            "status": status,
            "summary": snapshot.current_state,
            "description": description,
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
            "media_kind": "live_view",
            "media_refs": [snapshot.video_id],
            "snapshot_sequence": snapshot.last_success_sequence,
            "target_sequence": target_sequence,
            "sequence_gap": sequence_gap,
            "fallback_used": sequence_gap > 0,
            "observed_timestamp_ms": snapshot.last_success_timestamp_ms,
            "keyframe_count": len(snapshot.keyframes),
        }
        contract = build_capability_output_contract(
            capability=VIDEO_UNDERSTANDING_CAPABILITY,
            status="succeeded",
            output_ref=output_ref,
            data=payload,
            metadata={
                "provider": payload["provider"],
                "model": payload["model"],
                "latency_ms": 0,
                "source": "rolling_video_memory",
                "snapshot_sequence": snapshot.last_success_sequence,
                "target_sequence": target_sequence,
                "sequence_gap": sequence_gap,
                "keyframe_count": len(snapshot.keyframes),
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=payload,
            model_observation=_video_model_observation(payload),
            output_ref=output_ref,
            latency_ms=0,
            contract=contract,
            trace_summary=self._trace_summary(
                source="rolling_video_memory",
                snapshot=snapshot,
                provider=snapshot.provider or "rolling_video_memory",
                model=snapshot.model,
                target_sequence=target_sequence,
            ),
        )

    def _memory_unavailable_result(
        self,
        *,
        video_ref: str | None,
        snapshot: RealtimeVideoSnapshot | None,
        target_sequence: int | None = None,
        status_override: str | None = None,
    ) -> ToolResult:
        output_ref = (
            f"memory://realtime-video/{_safe_ref(video_ref or 'video')}/pending"
        )
        status = status_override or _snapshot_status(
            snapshot,
            target_sequence=target_sequence,
        )
        sequence_gap = _sequence_gap(snapshot, target_sequence=target_sequence)
        pending_count = snapshot.pending_count if snapshot is not None else 0
        in_flight = snapshot.in_flight if snapshot is not None else False
        error_code = _snapshot_error_code(snapshot)
        description = _memory_unavailable_description(
            status,
            pending_count=pending_count,
            in_flight=in_flight,
            error_code=error_code,
        )
        payload = {
            "status": status,
            "summary": description,
            "description": description,
            "objects": [],
            "people": [],
            "actions": [],
            "events": [],
            "scene": None,
            "products": [],
            "brands": [],
            "colors": [],
            "materials": [],
            "text_in_video": [],
            "timestamps": [],
            "style_tags": [],
            "confidence": None,
            "provider": "rolling_video_memory",
            "model": None,
            "output_ref": output_ref,
            "errors": [],
            "latency_ms": 0,
            "source": "realtime_video_memory_unavailable",
            "media_kind": "live_view",
            "media_refs": [video_ref] if video_ref else [],
            "snapshot_sequence": (
                snapshot.last_success_sequence if snapshot is not None else None
            ),
            "target_sequence": target_sequence,
            "sequence_gap": sequence_gap,
            "fallback_used": sequence_gap > 0,
            "observed_timestamp_ms": (
                snapshot.last_success_timestamp_ms if snapshot is not None else None
            ),
            "pending_count": pending_count,
            "in_flight": in_flight,
            "error_code": error_code,
            "usable_visual_text": False,
        }
        model_observation = {
            "status": status,
            "summary": description,
            "description": description,
            "source": "realtime_video_memory_unavailable",
            "media_kind": "live_view",
            "media_refs": [video_ref] if video_ref else [],
                "snapshot_sequence": payload["snapshot_sequence"],
                "target_sequence": target_sequence,
                "sequence_gap": sequence_gap,
                "fallback_used": sequence_gap > 0,
            "pending_count": pending_count,
            "in_flight": in_flight,
            "error_code": error_code,
            "usable_visual_text": False,
        }
        contract = build_capability_output_contract(
            capability=VIDEO_UNDERSTANDING_CAPABILITY,
            status="partial",
            output_ref=output_ref,
            data=payload,
            metadata={
                "provider": payload["provider"],
                "model": payload["model"],
                "latency_ms": 0,
                "source": "realtime_video_memory_unavailable",
                "status": status,
                "pending_count": pending_count,
                "in_flight": in_flight,
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=payload,
            voice_summary=description,
            model_observation=model_observation,
            output_ref=output_ref,
            latency_ms=0,
            contract=contract,
            trace_summary=self._trace_summary(
                source="realtime_video_memory_unavailable",
                snapshot=snapshot,
                provider="rolling_video_memory",
                model=None,
                target_sequence=target_sequence,
            ),
        )

    def _trace_summary(
        self,
        *,
        source: str,
        snapshot: RealtimeVideoSnapshot | None,
        provider: str | None,
        model: str | None,
        target_sequence: int | None = None,
    ) -> dict[str, object]:
        diagnostics = snapshot.observation_diagnostics if snapshot is not None else None
        published_at_ms = (
            diagnostics.published_at_ms if diagnostics is not None else None
        )
        snapshot_age_ms = (
            max(0, self.wall_clock_ms() - published_at_ms)
            if published_at_ms is not None
            else None
        )
        sequence_gap = _sequence_gap(snapshot, target_sequence=target_sequence)
        return {
            "source": source,
            "snapshot_age_ms": snapshot_age_ms,
            "observation_latency_ms": (
                diagnostics.observation_latency_ms if diagnostics is not None else None
            ),
            "semantic_publish_latency_ms": (
                diagnostics.semantic_publish_latency_ms if diagnostics is not None else None
            ),
            "pending_count": snapshot.pending_count if snapshot is not None else 0,
            "in_flight": snapshot.in_flight if snapshot is not None else False,
            "fallback_used": sequence_gap > 0,
            "target_sequence": target_sequence,
            "sequence_gap": sequence_gap,
            "snapshot_sequence": snapshot.last_success_sequence
            if snapshot is not None
            else None,
            "provider": provider,
            "model": model,
        }

    def _with_context_frames(
        self, input: VideoUnderstandingRequest
    ) -> VideoUnderstandingRequest:
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


def _is_agent_service_realtime_video_tool_call(context: ToolContext) -> bool:
    request_metadata = context.metadata.get("request_metadata")
    if not isinstance(request_metadata, dict):
        return False
    return is_trusted_agent_service_request(request_metadata)


def _sequence_gap(
    snapshot: RealtimeVideoSnapshot | None,
    *,
    target_sequence: int | None,
) -> int:
    if target_sequence is None:
        return 0
    snapshot_sequence = (
        snapshot.last_success_sequence if snapshot is not None else None
    )
    if snapshot_sequence is None:
        return target_sequence
    return max(0, target_sequence - snapshot_sequence)


def _snapshot_status(
    snapshot: RealtimeVideoSnapshot | None,
    *,
    target_sequence: int | None = None,
) -> str:
    if snapshot is None:
        return "unavailable"
    has_success = snapshot.last_success_sequence is not None
    pending = snapshot.in_flight or snapshot.pending_count > 0
    if has_success and _sequence_gap(snapshot, target_sequence=target_sequence) > 0:
        return "stale"
    if has_success and pending:
        return "refreshing"
    if has_success and snapshot.last_observation_status == "failed":
        return "stale"
    if has_success:
        return "ready"
    if pending:
        return "pending"
    if snapshot.last_observation_status == "failed":
        return "failed"
    return "unavailable"


def _snapshot_error_code(snapshot: RealtimeVideoSnapshot | None) -> str | None:
    if snapshot is None or not isinstance(snapshot.last_error, dict):
        return None
    code = snapshot.last_error.get("code")
    return str(code) if code else None


def _memory_description(snapshot: RealtimeVideoSnapshot, *, status: str) -> str:
    state = snapshot.current_state.strip()
    if status == "ready":
        return (
            f"后台视觉理解已返回可用文本：{state}"
            if state
            else "后台视觉理解已返回可用文本。"
        )
    if status == "refreshing":
        return (
            f"后台视觉理解已有一段可用文本，但最新画面仍在刷新：{state}"
            if state
            else "后台视觉理解已有可用文本，但最新画面仍在刷新。"
        )
    if status == "stale":
        return (
            f"后台视觉理解已有一段旧文本，最新观察没有成功更新；回答时需要说明可能不是当前画面：{state}"
            if state
            else "后台视觉理解已有旧文本，最新观察没有成功更新；回答时需要说明可能不是当前画面。"
        )
    return (
        f"后台视觉理解文本状态为 {status}：{state}"
        if state
        else f"后台视觉理解文本状态为 {status}。"
    )


def _memory_unavailable_description(
    status: str,
    *,
    pending_count: int,
    in_flight: bool,
    error_code: str | None,
) -> str:
    if status == "pending":
        if in_flight or pending_count > 0:
            return (
                "后台视觉理解正在处理当前画面，还没有产出可用的文字描述。"
                "请告诉用户正在获取画面信息，暂时不能确认画面内容。"
            )
        return "后台视觉理解还没有产出可用的文字描述。请告诉用户暂时不能确认画面内容。"
    if status == "failed":
        suffix = f"错误类型：{error_code}。" if error_code else ""
        return (
            "后台视觉理解没有成功返回可用文本，当前没有可靠的画面描述。"
            f"{suffix}请告诉用户暂时不能确认画面内容，可以稍后再试。"
        )
    if status == "unavailable":
        return (
            "当前还没有收到后台视觉理解文本，无法可靠判断画面内容。"
            "请告诉用户暂时没有可用画面信息。"
        )
    return (
        f"后台视觉理解当前状态为 {status}，没有可用的画面文字描述。"
        "请告诉用户暂时不能确认画面内容。"
    )


def _safe_ref(value: str) -> str:
    normalized = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value
    )
    return normalized or "video"


def _video_model_observation(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "summary",
        "description",
        "objects",
        "people",
        "actions",
        "events",
        "scene",
        "products",
        "brands",
        "colors",
        "materials",
        "target_sequence",
        "sequence_gap",
        "fallback_used",
        "text_in_video",
        "timestamps",
        "style_tags",
        "confidence",
        "source",
        "media_kind",
        "media_refs",
        "snapshot_sequence",
        "observed_timestamp_ms",
        "pending_count",
        "in_flight",
        "error_code",
        "usable_visual_text",
        "errors",
    )
    observation = {
        key: payload[key] for key in keys if payload.get(key) not in (None, "", [], {})
    }
    if "timestamps" in observation:
        observation["timestamps"] = [
            _timestamp_model_observation(item)
            for item in observation["timestamps"]
            if isinstance(item, dict)
        ]
    return observation


def _timestamp_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("start_ms", "end_ms", "description")
        if item.get(key) not in (None, "", [], {})
    }
