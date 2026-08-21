"""Video understanding Tool backed by a shared video adapter."""

from collections.abc import Callable
from datetime import datetime
from time import perf_counter_ns, time
from typing import Any
from zoneinfo import ZoneInfo

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
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
from assistant_agent.media.vision.observability import (
    VisionInferenceTraceLink,
    observe_vision_inference,
)
from assistant_agent.media.video.video_context import (
    DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    REALTIME_VISUAL_TARGET_WINDOW_SIZE,
    VideoContextStore,
)
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    RealtimeVideoSnapshot,
    SemanticKeyframeRecord,
)
from assistant_agent.media.video.semantic_store import (
    VisualSemanticRecord,
    VisualSemanticSnapshot,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.tools.ids import (
    VIDEO_UNDERSTANDING_CAPABILITY,
)
from assistant_agent.tools.runtime import ToolContext
from assistant_agent.observability.trace_store import append_observability_event


LIVE_VIEW_SNAPSHOT_WAIT_SECONDS = 4.0
LIVE_VIEW_TEXT_TIMELINE_LIMIT = 8


class VideoUnderstandingBranch:
    """Shared explicit-video and governed live-view execution branch."""

    def __init__(
        self,
        adapter: VideoUnderstandingAdapter | None = None,
        *,
        tool_name: str = "uploaded_media_inspect",
        client: VisionUnderstandingClient | None = None,
        context_store: VideoContextStore | None = None,
        memory_store: RealtimeVideoMemoryStore | None = None,
        semantic_store_pool: SessionVisualSemanticStorePool | None = None,
        context_window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.name = tool_name
        if client is None:
            self.adapter = adapter or create_video_understanding_adapter()
            self.client = AdapterVisionUnderstandingClient(video_adapter=self.adapter)
        else:
            self.adapter = adapter or getattr(client, "video_adapter", None)
            self.client = client
        self.context_store = context_store
        self.memory_store = memory_store
        self.semantic_store_pool = semantic_store_pool
        self.context_window_size = context_window_size
        self.wall_clock_ms = wall_clock_ms or (lambda: int(time() * 1000))

    def execute(
        self, input: VideoUnderstandingRequest, context: ToolContext
    ) -> ToolResult:
        video_ref = input.video_ref or (input.video_ids[0] if input.video_ids else None)
        observation_mode = context.metadata.get("realtime_video_observation") is True
        agent_service_text_only = (
            not observation_mode and _is_agent_service_realtime_video_tool_call(context)
        )
        snapshot = None
        status_snapshot = None
        text_observations: list[dict[str, object]] | None = None
        visual_target_sequence = self._live_target_sequence(context)
        visual_window_sequences = self._live_window_sequences(context)
        visual_window_timestamps_ms = self._live_window_timestamps_ms(context)
        visual_window_start_sequence = self._live_window_start_sequence(context)
        barrier_started_ns: int | None = None
        if (
            video_ref
            and agent_service_text_only
            and (self.semantic_store_pool is not None or self.memory_store is not None)
        ):
            if visual_target_sequence is not None:
                barrier_started_ns = perf_counter_ns()
                self._record_target_barrier(
                    context,
                    canonical_event="visual.target_barrier.started",
                    window_start_sequence=visual_window_start_sequence,
                    target_sequence=visual_target_sequence,
                )
            snapshot = self._live_snapshot_for_request(
                video_ref,
                context=context,
            )
            text_observations = self._live_text_observations(
                video_ref,
                context=context,
                snapshot=snapshot,
            )
            status_snapshot = snapshot
            if snapshot is None and visual_target_sequence is not None:
                status_snapshot = self.memory_store.snapshot(video_ref)
            exact_target_ready = visual_target_sequence is None or (
                snapshot is not None
                and snapshot.last_success_sequence == visual_target_sequence
            )
            if (
                snapshot is not None
                and exact_target_ready
                and (
                    snapshot.healthy
                    or (
                        agent_service_text_only
                        and snapshot.last_success_sequence is not None
                    )
                )
            ):
                result = self._memory_result(
                    snapshot,
                    window_start_sequence=visual_window_start_sequence,
                    target_sequence=visual_target_sequence,
                    window_sequences=visual_window_sequences,
                    window_timestamps_ms=visual_window_timestamps_ms,
                    observations=text_observations,
                )
                self._record_target_barrier_finished(
                    context,
                    result=result,
                    started_ns=barrier_started_ns,
                    window_start_sequence=visual_window_start_sequence,
                    target_sequence=visual_target_sequence,
                )
                return result
        if agent_service_text_only:
            status_override = None
            target_status = None
            if (
                video_ref
                and visual_target_sequence is not None
            ):
                semantic_store = self._semantic_store(context)
                target_failed = (
                    semantic_store.sequence_failed(
                        video_ref,
                        sequence=visual_target_sequence,
                    )
                    if semantic_store is not None
                    else self.memory_store is not None
                    and self.memory_store.sequence_failed(
                        video_ref,
                        target_sequence=visual_target_sequence,
                    )
                )
                target_status = "failed" if target_failed else "timeout"
                status_override = target_status
            result = self._memory_unavailable_result(
                video_ref=video_ref,
                snapshot=status_snapshot,
                window_start_sequence=visual_window_start_sequence,
                target_sequence=visual_target_sequence,
                window_sequences=visual_window_sequences,
                window_timestamps_ms=visual_window_timestamps_ms,
                status_override=status_override,
                target_status=target_status,
            )
            self._record_target_barrier_finished(
                context,
                result=result,
                started_ns=barrier_started_ns,
                window_start_sequence=visual_window_start_sequence,
                target_sequence=visual_target_sequence,
            )
            return result

        input = self._with_context_frames(input)
        self._sync_client_video_adapter()
        trace_links: list[VisionInferenceTraceLink] = []
        source = (
            "background_keyframe_observation" if observation_mode else "explicit_video"
        )
        media_kind = "live_view" if observation_mode else "explicit_video"
        prompt_version = (
            "realtime-single-frame-v1" if observation_mode else "video-understanding-v1"
        )
        frame_sequence = (
            input.metadata.get("frame_sequence")
            if isinstance(input.metadata.get("frame_sequence"), int)
            and not isinstance(input.metadata.get("frame_sequence"), bool)
            else None
        )
        try:
            result = video_result_from_vision_result(
                observe_vision_inference(
                    lambda: self.client.understand(
                        vision_request_from_video_request(input)
                    ),
                    context=context,
                    capability=VIDEO_UNDERSTANDING_CAPABILITY,
                    source=source,
                    media_kind=media_kind,
                    media_count=max(
                        len(input.frame_refs),
                        len(input.video_ids),
                        1 if input.video_ref else 0,
                    ),
                    frame_sequence=frame_sequence,
                    prompt_version=prompt_version,
                    local_input_content=self._local_vlm_input_content(
                        input,
                        mode=source,
                        media_kind=media_kind,
                        prompt_version=prompt_version,
                        frame_sequence=frame_sequence,
                    ),
                    trace_link_callback=trace_links.append,
                )
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

        payload = {
            **result.model_dump(mode="json"),
            "source": source,
            "media_kind": ("live_view" if observation_mode else "explicit_video"),
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
                trace_link=trace_links[-1] if trace_links else None,
            ),
        )

    def _live_snapshot_for_request(
        self,
        video_ref: str,
        *,
        context: ToolContext,
    ) -> RealtimeVideoSnapshot | None:
        semantic_store = self._semantic_store(context)
        target_sequence = self._live_target_sequence(context)
        if semantic_store is not None:
            if target_sequence is not None:
                semantic_store.wait_for_sequence(
                    video_ref,
                    sequence=target_sequence,
                    timeout_seconds=LIVE_VIEW_SNAPSHOT_WAIT_SECONDS,
                )
                record = semantic_store.exact_sequence(
                    video_ref,
                    sequence=target_sequence,
                    visual_window_id=self._live_window_id(context),
                )
            else:
                record = semantic_store.latest(video_ref)
            visual_snapshot = semantic_store.snapshot(video_ref)
            return _project_visual_semantic_snapshot(
                video_ref,
                visual_snapshot,
                record,
            )
        if self.memory_store is None:
            return None
        if target_sequence is not None:
            return self.memory_store.wait_for_snapshot_sequence(
                video_ref,
                target_sequence=target_sequence,
                timeout_seconds=LIVE_VIEW_SNAPSHOT_WAIT_SECONDS,
            )
        return self.memory_store.snapshot(video_ref)

    def _semantic_store(self, context: ToolContext):
        if (
            self.semantic_store_pool is None
            or not context.user_id
            or not context.session_id
        ):
            return None
        return self.semantic_store_pool.peek(context.user_id, context.session_id)

    def _live_text_observations(
        self,
        video_ref: str,
        *,
        context: ToolContext,
        snapshot: RealtimeVideoSnapshot | None,
    ) -> list[dict[str, object]]:
        semantic_store = self._semantic_store(context)
        target_sequence = self._live_target_sequence(context)
        window_start_sequence = self._live_window_start_sequence(context)
        window_sequences = self._live_window_sequences(context)
        if semantic_store is not None:
            sequence = target_sequence
            if sequence is None and snapshot is not None:
                sequence = snapshot.last_success_sequence
            if sequence is not None:
                records = (
                    semantic_store.records_in_sequence_range(
                        video_ref,
                        start_sequence=window_start_sequence,
                        end_sequence=sequence,
                    )
                    if window_start_sequence is not None
                    else semantic_store.recent_at_or_before(
                        video_ref,
                        sequence=sequence,
                        limit=LIVE_VIEW_TEXT_TIMELINE_LIMIT,
                    )
                )
                if window_sequences:
                    allowed_sequences = set(window_sequences)
                    records = [
                        record
                        for record in records
                        if record.frame_sequence in allowed_sequences
                    ]
                return [
                    {
                        "sequence": record.frame_sequence,
                        "role": (
                            "target"
                            if record.frame_sequence == target_sequence
                            else "context"
                        ),
                        "timestamp_ms": (
                            record.captured_at_ms
                            if record.captured_at_ms is not None
                            else record.created_at_ms
                        ),
                        "text": record.summary,
                    }
                    for record in records
                    if record.summary
                ]
        if snapshot is None or not snapshot.current_state:
            return []
        observation: dict[str, object] = {"text": snapshot.current_state}
        if snapshot.last_success_timestamp_ms is not None:
            observation["timestamp_ms"] = snapshot.last_success_timestamp_ms
        return [observation]

    @staticmethod
    def _live_target_sequence(context: ToolContext) -> int | None:
        direct_target = context.metadata.get("visual_target_sequence")
        if (
            not isinstance(direct_target, bool)
            and isinstance(direct_target, int)
            and direct_target >= 0
        ):
            return direct_target
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

    @classmethod
    def _live_window_start_sequence(cls, context: ToolContext) -> int | None:
        window_sequences = cls._live_window_sequences(context)
        if window_sequences:
            return window_sequences[0]
        target_sequence = cls._live_target_sequence(context)
        start_sequence = context.metadata.get("visual_window_start_sequence")
        if (
            target_sequence is not None
            and not isinstance(start_sequence, bool)
            and isinstance(start_sequence, int)
            and 0 <= start_sequence <= target_sequence
            and target_sequence - start_sequence < REALTIME_VISUAL_TARGET_WINDOW_SIZE
        ):
            return start_sequence
        return None

    @classmethod
    def _live_window_sequences(cls, context: ToolContext) -> tuple[int, ...]:
        value = context.metadata.get("visual_window_sequences")
        if not isinstance(value, (list, tuple)) or not value:
            return ()
        if len(value) > REALTIME_VISUAL_TARGET_WINDOW_SIZE:
            return ()
        sequences: list[int] = []
        for sequence in value:
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                return ()
            if sequences and sequence <= sequences[-1]:
                return ()
            sequences.append(sequence)
        target_sequence = cls._live_target_sequence(context)
        if target_sequence is None or sequences[-1] != target_sequence:
            return ()
        return tuple(sequences)

    @classmethod
    def _live_window_timestamps_ms(
        cls,
        context: ToolContext,
    ) -> tuple[int | None, ...]:
        sequences = cls._live_window_sequences(context)
        value = context.metadata.get("visual_window_timestamps_ms")
        if not sequences or not isinstance(value, (list, tuple)):
            return tuple(None for _ in sequences)
        if len(value) != len(sequences):
            return tuple(None for _ in sequences)
        timestamps: list[int | None] = []
        for timestamp_ms in value:
            if timestamp_ms is None:
                timestamps.append(None)
            elif (
                isinstance(timestamp_ms, int)
                and not isinstance(timestamp_ms, bool)
                and timestamp_ms >= 0
            ):
                timestamps.append(timestamp_ms)
            else:
                return tuple(None for _ in sequences)
        return tuple(timestamps)

    @staticmethod
    def _live_window_id(context: ToolContext) -> str | None:
        value = context.metadata.get("visual_window_id")
        return value if isinstance(value, str) and 1 <= len(value) <= 160 else None

    def _record_target_barrier(
        self,
        context: ToolContext,
        *,
        canonical_event: str,
        window_start_sequence: int | None,
        target_sequence: int,
        extra_attributes: dict[str, object] | None = None,
    ) -> None:
        if not context.trace_id or not context.run_id:
            return
        attributes: dict[str, object] = {
            "target_sequence": target_sequence,
        }
        window_id = self._live_window_id(context)
        if window_id is not None:
            attributes["visual_window_id"] = window_id
        if window_start_sequence is not None:
            attributes["window_start_sequence"] = window_start_sequence
        if extra_attributes:
            attributes.update(extra_attributes)
        try:
            append_observability_event(
                context.trace_store,
                trace_id=context.trace_id,
                run_id=context.run_id,
                user_id=context.user_id,
                session_id=context.session_id,
                canonical_event=canonical_event,
                observation_type="span",
                observation_name="visual.target_barrier",
                observation_scope="iteration",
                node_name="live_view",
                status=(
                    str(extra_attributes.get("target_status"))
                    if extra_attributes and extra_attributes.get("target_status")
                    else "started"
                ),
                attributes=attributes,
            )
        except Exception:
            return

    def _record_target_barrier_finished(
        self,
        context: ToolContext,
        *,
        result: ToolResult,
        started_ns: int | None,
        window_start_sequence: int | None,
        target_sequence: int | None,
    ) -> None:
        if started_ns is None or target_sequence is None:
            return
        data = result.data if isinstance(result.data, dict) else {}
        ready_sequences = data.get("ready_sequences")
        missing_sequences = data.get("missing_sequences")
        self._record_target_barrier(
            context,
            canonical_event="visual.target_barrier.finished",
            window_start_sequence=window_start_sequence,
            target_sequence=target_sequence,
            extra_attributes={
                "target_status": str(data.get("target_status") or "unavailable"),
                "ready_count": (
                    len(ready_sequences) if isinstance(ready_sequences, list) else 0
                ),
                "missing_count": (
                    len(missing_sequences)
                    if isinstance(missing_sequences, list)
                    else 0
                ),
                "wait_ms": max(0, (perf_counter_ns() - started_ns) // 1_000_000),
            },
        )

    def _sync_client_video_adapter(self) -> None:
        if (
            self.adapter is not None
            and isinstance(self.client, AdapterVisionUnderstandingClient)
            and self.client.video_adapter is not self.adapter
        ):
            self.client.video_adapter = self.adapter

    def _local_vlm_input_content(
        self,
        request: VideoUnderstandingRequest,
        *,
        mode: str,
        media_kind: str,
        prompt_version: str,
        frame_sequence: int | None,
    ) -> dict[str, object]:
        resolved_instructions = None
        resolve = getattr(self.adapter, "resolved_instructions", None)
        if callable(resolve):
            try:
                candidate = resolve(request)
            except Exception:  # noqa: BLE001 - observability must remain fail-open.
                candidate = None
            if isinstance(candidate, str) and candidate:
                resolved_instructions = candidate
        frame_count = len(request.frame_refs)
        return {
            "mode": mode,
            "prompt_version": prompt_version,
            "resolved_instructions": resolved_instructions,
            "query": request.user_query,
            "media_kind": media_kind,
            "frame_sequence": frame_sequence,
            "frame_count": frame_count,
            "history_frame_count": max(0, frame_count - 1),
            "memory_context_present": bool(request.memory_context),
        }

    def _memory_result(
        self,
        snapshot: RealtimeVideoSnapshot,
        *,
        window_start_sequence: int | None = None,
        target_sequence: int | None = None,
        window_sequences: tuple[int, ...] = (),
        window_timestamps_ms: tuple[int | None, ...] = (),
        observations: list[dict[str, object]] | None = None,
    ) -> ToolResult:
        output_ref = f"memory://realtime-video/{_safe_ref(snapshot.video_id)}"
        target_ready = (
            target_sequence is not None
            and snapshot.last_success_sequence == target_sequence
        )
        status = (
            "ready"
            if target_ready
            else _snapshot_status(snapshot, target_sequence=target_sequence)
        )
        description = _memory_description(snapshot, status=status)
        sequence_gap = _sequence_gap(snapshot, target_sequence=target_sequence)
        ready_sequences = [
            int(item["sequence"])
            for item in observations or []
            if isinstance(item.get("sequence"), int)
            and not isinstance(item.get("sequence"), bool)
        ]
        missing_sequences = _missing_window_sequences(
            window_start_sequence,
            target_sequence,
            ready_sequences,
            window_sequences=window_sequences,
        )
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
            "window_start_sequence": window_start_sequence,
            "target_sequence": target_sequence,
            "window_sequences": list(window_sequences),
            "window_timestamps_ms": list(window_timestamps_ms),
            "ready_sequences": ready_sequences,
            "missing_sequences": missing_sequences,
            "target_ready": target_ready,
            "target_status": "ready" if target_ready else status,
            "sequence_gap": sequence_gap,
            "fallback_used": sequence_gap > 0,
            "observed_timestamp_ms": snapshot.last_success_timestamp_ms,
            "keyframe_count": len(snapshot.keyframes),
            "observations": observations or [],
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
            model_observation=_live_view_model_observation(payload),
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
        window_start_sequence: int | None = None,
        target_sequence: int | None = None,
        window_sequences: tuple[int, ...] = (),
        window_timestamps_ms: tuple[int | None, ...] = (),
        status_override: str | None = None,
        target_status: str | None = None,
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
            "window_start_sequence": window_start_sequence,
            "target_sequence": target_sequence,
            "window_sequences": list(window_sequences),
            "window_timestamps_ms": list(window_timestamps_ms),
            "ready_sequences": [],
            "missing_sequences": _missing_window_sequences(
                window_start_sequence,
                target_sequence,
                [],
                window_sequences=window_sequences,
            ),
            "target_ready": False,
            "target_status": target_status or status,
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
            model_observation=_live_view_model_observation(payload),
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
        trace_link: VisionInferenceTraceLink | None = None,
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
                diagnostics.semantic_publish_latency_ms
                if diagnostics is not None
                else None
            ),
            "h264_decode_latency_ms": (
                diagnostics.h264_decode_latency_ms if diagnostics is not None else None
            ),
            "keyframe_selection_latency_ms": (
                diagnostics.keyframe_selection_latency_ms
                if diagnostics is not None
                else None
            ),
            "queue_wait_latency_ms": (
                diagnostics.queue_wait_latency_ms if diagnostics is not None else None
            ),
            "text_embedding_latency_ms": (
                diagnostics.text_embedding_latency_ms
                if diagnostics is not None
                else None
            ),
            "visual_memory_index_latency_ms": (
                diagnostics.visual_memory_index_latency_ms
                if diagnostics is not None
                else None
            ),
            "semantic_store_write_latency_ms": (
                diagnostics.semantic_store_write_latency_ms
                if diagnostics is not None
                else None
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
            "source_vision_trace_id": (
                trace_link.trace_id
                if trace_link is not None
                else snapshot.source_vision_trace_id
                if snapshot is not None
                else None
            ),
            "source_vision_run_id": (
                trace_link.run_id
                if trace_link is not None
                else snapshot.source_vision_run_id
                if snapshot is not None
                else None
            ),
            "source_vlm_span_id": (
                trace_link.span_id
                if trace_link is not None
                else snapshot.source_vlm_span_id
                if snapshot is not None
                else None
            ),
            "source_visual_record_id": (
                snapshot.source_visual_record_id if snapshot is not None else None
            ),
            "jpeg_prepare_latency_ms": (
                diagnostics.jpeg_prepare_latency_ms if diagnostics is not None else None
            ),
            "connection_setup_latency_ms": (
                diagnostics.connection_setup_latency_ms
                if diagnostics is not None
                else None
            ),
            "instruction_update_latency_ms": (
                diagnostics.instruction_update_latency_ms
                if diagnostics is not None
                else None
            ),
            "media_commit_latency_ms": (
                diagnostics.media_commit_latency_ms if diagnostics is not None else None
            ),
            "response_first_delta_latency_ms": (
                diagnostics.response_first_delta_latency_ms
                if diagnostics is not None
                else None
            ),
            "response_tail_latency_ms": (
                diagnostics.response_tail_latency_ms
                if diagnostics is not None
                else None
            ),
            "response_latency_ms": (
                diagnostics.response_latency_ms if diagnostics is not None else None
            ),
            "result_parse_latency_ms": (
                diagnostics.result_parse_latency_ms if diagnostics is not None else None
            ),
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
    if context.metadata.get("media_source") == "uploaded":
        return False
    if context.metadata.get("entry_profile") == "agent_service":
        return True
    request_metadata = context.metadata.get("request_metadata")
    if not isinstance(request_metadata, dict):
        return False
    return is_trusted_agent_service_request(request_metadata)


def _project_visual_semantic_snapshot(
    video_id: str,
    snapshot: VisualSemanticSnapshot | None,
    record: VisualSemanticRecord | None,
) -> RealtimeVideoSnapshot | None:
    if snapshot is None and record is None:
        return None
    return RealtimeVideoSnapshot(
        video_id=video_id,
        current_state=record.summary if record is not None else "",
        objects=list(record.objects) if record is not None else [],
        people=list(record.people) if record is not None else [],
        actions=list(record.actions) if record is not None else [],
        events=list(record.events) if record is not None else [],
        scene=record.scene if record is not None else None,
        products=list(record.products) if record is not None else [],
        brands=list(record.brands) if record is not None else [],
        colors=list(record.colors) if record is not None else [],
        materials=list(record.materials) if record is not None else [],
        text_in_video=list(record.text_in_video) if record is not None else [],
        timestamps=(
            [dict(item) for item in record.timestamps] if record is not None else []
        ),
        style_tags=list(record.style_tags) if record is not None else [],
        confidence=record.confidence if record is not None else None,
        provider=record.provider if record is not None else None,
        model=record.model if record is not None else None,
        source_vision_trace_id=(
            record.source_vision_trace_id if record is not None else None
        ),
        source_vision_run_id=(
            record.source_vision_run_id if record is not None else None
        ),
        source_vlm_span_id=(record.source_vlm_span_id if record is not None else None),
        source_visual_record_id=(record.record_id if record is not None else None),
        keyframes=(
            [
                SemanticKeyframeRecord(
                    frame_id=record.record_id,
                    uri=record.evidence_ref,
                    sequence=record.frame_sequence,
                    timestamp_ms=record.captured_at_ms,
                )
            ]
            if record is not None
            else []
        ),
        last_success_sequence=(record.frame_sequence if record is not None else None),
        last_success_timestamp_ms=(
            record.captured_at_ms if record is not None else None
        ),
        last_observation_status=(
            snapshot.last_observation_status if snapshot is not None else "succeeded"
        ),
        last_error=snapshot.last_error if snapshot is not None else None,
        pending_count=snapshot.pending_count if snapshot is not None else 0,
        in_flight=snapshot.in_flight if snapshot is not None else False,
        observation_diagnostics=(
            RealtimeVideoObservationDiagnostics(published_at_ms=record.created_at_ms)
            if record is not None
            else None
        ),
    )


def _sequence_gap(
    snapshot: RealtimeVideoSnapshot | None,
    *,
    target_sequence: int | None,
) -> int:
    if target_sequence is None:
        return 0
    snapshot_sequence = snapshot.last_success_sequence if snapshot is not None else None
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
        "window_start_sequence",
        "ready_sequences",
        "missing_sequences",
        "target_ready",
        "target_status",
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


def _live_view_model_observation(payload: dict[str, Any]) -> dict[str, Any]:
    sequences = payload.get("window_sequences")
    if not isinstance(sequences, list):
        sequences = []
    timestamps = payload.get("window_timestamps_ms")
    if not isinstance(timestamps, list) or len(timestamps) != len(sequences):
        timestamps = [None] * len(sequences)
    return {
        "window": [
            {
                "sequence": sequence,
                "captured_at": _beijing_time(timestamp_ms),
            }
            for sequence, timestamp_ms in zip(sequences, timestamps)
        ],
        "vlm_response": str(
            payload.get("summary")
            or payload.get("description")
            or "当前没有可用的视觉理解文本。"
        ),
    }


def _beijing_time(timestamp_ms: Any) -> str | None:
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms < 0
    ):
        return None
    try:
        captured_at = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=ZoneInfo("Asia/Shanghai"),
        )
    except (OverflowError, OSError, ValueError):
        return None
    return captured_at.isoformat(timespec="milliseconds")


def _missing_window_sequences(
    start_sequence: int | None,
    target_sequence: int | None,
    ready_sequences: list[int],
    *,
    window_sequences: tuple[int, ...] = (),
) -> list[int]:
    if window_sequences:
        ready = set(ready_sequences)
        return [sequence for sequence in window_sequences if sequence not in ready]
    if start_sequence is None or target_sequence is None:
        return []
    ready = set(ready_sequences)
    return [
        sequence
        for sequence in range(start_sequence, target_sequence + 1)
        if sequence not in ready
    ]
