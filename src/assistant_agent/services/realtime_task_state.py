"""Session-scoped task state for realtime assistant turns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult, ToolSideEffectLevel, ToolSideEffectPolicy
from assistant_agent.services.context.compaction import compact_observation_for_context
from assistant_agent.tools.registry import tool_side_effect_policy


REALTIME_TASK_STATE_SCHEMA_VERSION = "realtime_task_state_v1"
REALTIME_TASK_STATE_METADATA_KEY = "realtime_task_state"
REALTIME_TASK_STATE_TEXT_METADATA_KEY = "realtime_task_state_text"

RealtimeTaskStatus = Literal[
    "active",
    "revising",
    "waiting_for_user",
    "completed",
    "cancelled",
    "blocked",
]
IntentRevisionType = Literal[
    "add_constraint",
    "replace_constraint",
    "change_goal",
    "cancel_goal",
    "confirm",
    "clarify",
]
ContinuationStrategy = Literal[
    "restart",
    "reuse_and_replan",
    "resume_from_checkpoint",
    "ask_confirmation",
    "compensate",
    "report_committed",
]
RealtimeTtsState = Literal["idle", "speaking", "interrupted", "superseded"]
RealtimeBargeInSource = Literal[
    "transcript",
    "explicit_cancel",
    "hangup",
    "media_relay_control",
    "unknown",
]
TaskArtifactKind = Literal[
    "observation",
    "tool_result",
    "media_ref",
    "draft",
    "decision",
    "checkpoint",
]
ArtifactReusePolicy = Literal[
    "reusable",
    "stale",
    "requires_validation",
    "do_not_reuse",
]

_INTERRUPT_CONTROLS = {"interrupt", "barge_in", "cancel_previous"}
_REALTIME_SOURCES = {
    "gateway_websocket",
    "realtime_agent_backend",
    "realtime_media_websocket",
    "phone_runtime",
}
_MAX_TEXT_CHARS = 1_200
_MAX_SOURCE_IDS = 24
_MAX_ARTIFACTS = 12
_MAX_SNAPSHOT_ARTIFACTS = 6
_MAX_SIDE_EFFECTS = 12
_MAX_SNAPSHOT_SIDE_EFFECTS = 6
_MAX_CHECKPOINT_ARTIFACT_REFS = 6
_REUSABLE_TOOL_NAMES = {
    "product_search",
    "price_compare",
    "vision_understanding",
    "video_understanding",
    "memory_retrieval",
}
_REQUIRES_VALIDATION_TOOL_NAMES = {
    "image_generation",
    "render_3d",
}
_DO_NOT_REUSE_TOOL_NAMES = {
    "delegate_to_agent",
    "memory_save",
    "shell_command",
}
_INVALIDATE_ARTIFACT_MARKERS = (
    "重新搜索",
    "重新找",
    "重新查",
    "重新比",
    "不要之前",
    "不用之前",
    "别用之前",
    "换一批",
    "全部换",
    "from scratch",
    "start over",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IntentRevision(BaseModel):
    """One user correction that changes the active realtime task."""

    revision_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    turn_id: str | None = None
    run_id: str | None = None
    user_text: str = ""
    revision_type: IntentRevisionType = "add_constraint"
    strategy: ContinuationStrategy = "restart"
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskArtifact(BaseModel):
    """Prompt-safe task artifact captured from completed realtime work."""

    artifact_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str | None = None
    kind: TaskArtifactKind
    reuse_policy: ArtifactReusePolicy = "requires_validation"
    summary: str = ""
    tool_name: str | None = None
    output_ref: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)


class SideEffectRecord(BaseModel):
    """Prompt-safe side-effect lifecycle record for realtime interruption."""

    record_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str | None = None
    tool_name: str = Field(min_length=1)
    effect_level: ToolSideEffectLevel
    requires_confirmation: bool = False
    confirmation_id: str | None = None
    compensation_hint: str | None = None
    summary: str = ""
    output_ref: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)


class RealtimeTaskState(BaseModel):
    """Current task state for one realtime user/session."""

    schema_version: str = REALTIME_TASK_STATE_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)
    status: RealtimeTaskStatus = "active"
    source_turn_ids: list[str] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    latest_user_text: str = ""
    latest_turn_id: str | None = None
    latest_run_id: str | None = None
    continuation_strategy: ContinuationStrategy | None = None
    revisions: list[IntentRevision] = Field(default_factory=list)
    artifacts: list[TaskArtifact] = Field(default_factory=list)
    side_effects: list[SideEffectRecord] = Field(default_factory=list)
    pending_tool: dict[str, Any] | None = None
    tts_state: RealtimeTtsState = "idle"
    last_spoken_progress: dict[str, Any] | None = None
    speech_turn_id: str | None = None
    barge_in_source: RealtimeBargeInSource | None = None
    last_realtime_event_ids: list[str] = Field(default_factory=list)


class RealtimeTaskStateSnapshot(BaseModel):
    """Prompt-safe snapshot injected into assistant context."""

    schema_version: str = REALTIME_TASK_STATE_SCHEMA_VERSION
    task_id: str
    status: RealtimeTaskStatus
    objective: str
    constraints: list[str] = Field(default_factory=list)
    current_user_text: str = ""
    current_turn_id: str | None = None
    current_run_id: str | None = None
    source_turn_ids: list[str] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    continuation_strategy: ContinuationStrategy | None = None
    revision_count: int = Field(default=0, ge=0)
    latest_revision: dict[str, Any] | None = None
    reusable_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    artifact_count: int = Field(default=0, ge=0)
    stale_artifact_count: int = Field(default=0, ge=0)
    side_effects: list[dict[str, Any]] = Field(default_factory=list)
    side_effect_count: int = Field(default=0, ge=0)
    pending_confirmation_count: int = Field(default=0, ge=0)
    committed_side_effect_count: int = Field(default=0, ge=0)
    compensatable_side_effect_count: int = Field(default=0, ge=0)
    pending_tool: dict[str, Any] | None = None
    tts_state: RealtimeTtsState = "idle"
    last_spoken_progress: dict[str, Any] | None = None
    speech_turn_id: str | None = None
    barge_in_source: RealtimeBargeInSource | None = None
    last_realtime_event_ids: list[str] = Field(default_factory=list)


class RealtimeTaskStateStore(Protocol):
    """Storage boundary for realtime session task state."""

    def get(self, user_id: str, session_id: str) -> RealtimeTaskState | None:
        """Return the current task state for a user/session."""

    def save(self, state: RealtimeTaskState) -> None:
        """Persist the current task state."""

    def clear(self, user_id: str, session_id: str) -> None:
        """Clear task state for a user/session."""


class InMemoryRealtimeTaskStateStore:
    """Process-local realtime task state store keyed by user/session."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], RealtimeTaskState] = {}

    def get(self, user_id: str, session_id: str) -> RealtimeTaskState | None:
        state = self._states.get((user_id, session_id))
        return state.model_copy(deep=True) if state is not None else None

    def save(self, state: RealtimeTaskState) -> None:
        self._states[(state.user_id, state.session_id)] = state.model_copy(deep=True)

    def clear(self, user_id: str, session_id: str) -> None:
        self._states.pop((user_id, session_id), None)


_DEFAULT_REALTIME_TASK_STATE_STORE = InMemoryRealtimeTaskStateStore()


def get_default_realtime_task_state_store() -> RealtimeTaskStateStore:
    """Return the process-wide realtime task-state store."""

    return _DEFAULT_REALTIME_TASK_STATE_STORE


def prepare_realtime_task_state_request(
    request: UserRequest,
    *,
    store: RealtimeTaskStateStore | None = None,
) -> UserRequest:
    """Attach realtime task-state metadata to a request when enabled."""

    if not realtime_task_state_enabled(request):
        return request

    resolved_store = store or get_default_realtime_task_state_store()
    if request.metadata.get("reset_conversation") is True:
        resolved_store.clear(request.user_id, request.session_id)

    state = resolved_store.get(request.user_id, request.session_id)
    state = _updated_state_for_request(request, state)
    resolved_store.save(state)
    snapshot = snapshot_from_task_state(state)
    metadata = dict(request.metadata)
    snapshot_payload = snapshot.model_dump(mode="json")
    metadata[REALTIME_TASK_STATE_METADATA_KEY] = snapshot_payload
    metadata[REALTIME_TASK_STATE_TEXT_METADATA_KEY] = format_realtime_task_state_snapshot(snapshot)
    metadata["realtime_task_state_enabled"] = True
    return request.model_copy(update={"metadata": metadata}, deep=True)


def record_realtime_task_state_run_artifacts(
    state: Any,
    *,
    store: RealtimeTaskStateStore | None = None,
) -> None:
    """Record prompt-safe artifacts from a completed realtime run."""

    request = getattr(state, "request", None)
    if not isinstance(request, UserRequest) or not realtime_task_state_enabled(request):
        return
    if getattr(state, "status", None) != "completed":
        return
    resolved_store = store or get_default_realtime_task_state_store()
    task_state = resolved_store.get(request.user_id, request.session_id)
    if task_state is None:
        return

    run_id = _metadata_string(getattr(state, "run_id", None))
    media_artifacts = _media_artifacts_from_request(request, task_state=task_state, run_id=run_id)
    tool_artifacts = _tool_artifacts_from_state(state, task_state=task_state)
    checkpoint_artifacts = _checkpoint_artifacts_from_tool_artifacts(
        tool_artifacts,
        task_state=task_state,
        run_id=run_id,
    )
    new_artifacts = [*media_artifacts, *tool_artifacts, *checkpoint_artifacts]
    new_side_effects = _side_effect_records_from_state(state, task_state=task_state)
    if not new_artifacts and not new_side_effects:
        return

    task_state.artifacts = _merge_artifacts(task_state.artifacts, new_artifacts)
    task_state.side_effects = _merge_side_effects(task_state.side_effects, new_side_effects)
    task_state.updated_at = _utc_now()
    resolved_store.save(task_state)


def realtime_task_state_progress_payload(request: UserRequest) -> dict[str, Any] | None:
    """Return progress metadata for a prepared realtime task-state request."""

    snapshot = request.metadata.get(REALTIME_TASK_STATE_METADATA_KEY)
    if not isinstance(snapshot, dict) or snapshot.get("status") != "revising":
        return None
    latest_revision = snapshot.get("latest_revision")
    strategy = (
        latest_revision.get("strategy")
        if isinstance(latest_revision, dict)
        else snapshot.get("continuation_strategy")
    )
    reusable_artifacts = snapshot.get("reusable_artifacts")
    reusable_count = len(reusable_artifacts) if isinstance(reusable_artifacts, list) else 0
    checkpoint_count = (
        sum(1 for artifact in reusable_artifacts if isinstance(artifact, dict) and artifact.get("kind") == "checkpoint")
        if isinstance(reusable_artifacts, list)
        else 0
    )
    return {
        "stage": "task_state",
        "status": "revising",
        "current_step": "intent_revision",
        "strategy": strategy or "restart",
        "reusable_artifact_count": reusable_count,
        "checkpoint_count": checkpoint_count,
        "stale_artifact_count": snapshot.get("stale_artifact_count", 0),
        "pending_confirmation_count": snapshot.get("pending_confirmation_count", 0),
        "committed_side_effect_count": snapshot.get("committed_side_effect_count", 0),
        "compensatable_side_effect_count": snapshot.get("compensatable_side_effect_count", 0),
    }


def realtime_task_state_enabled(request: UserRequest) -> bool:
    """Return whether a request should receive realtime task-state metadata."""

    metadata = request.metadata
    if metadata.get("realtime_task_state_enabled") is True:
        return True
    if metadata.get("enable_realtime_task_state") is True:
        return True
    source = _metadata_string(metadata.get("source"))
    if source in _REALTIME_SOURCES:
        return True
    if isinstance(metadata.get("gateway"), dict):
        return True
    realtime = metadata.get("realtime")
    return isinstance(realtime, dict) and ("run_id" in realtime or "turn_id" in realtime)


def realtime_metadata_requests_interrupt(metadata: dict[str, Any]) -> bool:
    """Return whether normalized runtime metadata represents an interrupt."""

    if metadata.get("interrupt") is True:
        return True
    control = _metadata_string(metadata.get("control"))
    if control in _INTERRUPT_CONTROLS:
        return True
    realtime = metadata.get("realtime")
    if isinstance(realtime, dict):
        if realtime.get("interrupt") is True:
            return True
        realtime_control = _metadata_string(realtime.get("control"))
        if realtime_control in _INTERRUPT_CONTROLS:
            return True
    gateway = metadata.get("gateway")
    if isinstance(gateway, dict):
        if gateway.get("interrupt") is True:
            return True
        gateway_control = _metadata_string(gateway.get("control"))
        if gateway_control in _INTERRUPT_CONTROLS:
            return True
    return False


def reduce_realtime_task_state_event(
    state: RealtimeTaskState,
    *,
    event_type: str,
    text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RealtimeTaskState:
    """Apply one prompt-safe realtime event update to task state."""

    event_payload = dict(payload or {})
    updated = state.model_copy(deep=True)
    event_id = _metadata_string(
        event_payload.get("event_id") or event_payload.get("id") or event_payload.get("frame_id")
    )
    if event_id:
        updated.last_realtime_event_ids = _append_unique_limited(
            updated.last_realtime_event_ids,
            event_id,
        )
    speech_turn_id = _metadata_string(event_payload.get("speech_turn_id"))

    if event_type == "tool.started":
        updated.pending_tool = _pending_tool_from_event(event_payload, default_status="working")
    elif event_type == "run.progress":
        progress = _spoken_progress_from_event(text=text, payload=event_payload)
        if progress is not None:
            updated.last_spoken_progress = progress
            updated.tts_state = "speaking"
        if event_payload.get("stage") == "tool" and _metadata_string(event_payload.get("tool_name")):
            updated.pending_tool = _pending_tool_from_event(event_payload, default_status="working")
    elif event_type in {"tool.finished", "tool.failed"}:
        tool_name = _metadata_string(event_payload.get("tool_name") or event_payload.get("name"))
        if _pending_tool_matches(updated.pending_tool, tool_name):
            updated.pending_tool = None
    elif event_type in {"response.chunk", "response.final"} and _metadata_string(text):
        updated.tts_state = "speaking"
    elif event_type == "tts.started":
        updated.tts_state = "speaking"
        if speech_turn_id:
            updated.speech_turn_id = speech_turn_id
    elif event_type == "tts.finished":
        updated.tts_state = "idle"
        if speech_turn_id:
            updated.speech_turn_id = speech_turn_id
    elif event_type in {"tts.superseded", "display.superseded"}:
        updated.tts_state = "superseded"
        if speech_turn_id:
            updated.speech_turn_id = speech_turn_id
    elif event_type in {"run.cancel", "call.hangup"}:
        updated.pending_tool = None
        updated.tts_state = "interrupted"
        cancel_source = _metadata_string(event_payload.get("cancel_source"))
        updated.barge_in_source = (
            "hangup" if event_type == "call.hangup" or cancel_source == "gateway_hangup" else "explicit_cancel"
        )

    updated.updated_at = _utc_now()
    return updated


def snapshot_from_task_state(state: RealtimeTaskState) -> RealtimeTaskStateSnapshot:
    """Build a concise prompt-safe task-state snapshot."""

    latest_revision = state.revisions[-1].model_dump(mode="json") if state.revisions else None
    return RealtimeTaskStateSnapshot(
        task_id=state.task_id,
        status=state.status,
        objective=state.objective,
        constraints=list(state.constraints),
        current_user_text=state.latest_user_text,
        current_turn_id=state.latest_turn_id,
        current_run_id=state.latest_run_id,
        source_turn_ids=list(state.source_turn_ids),
        source_run_ids=list(state.source_run_ids),
        continuation_strategy=state.continuation_strategy,
        revision_count=len(state.revisions),
        latest_revision=latest_revision,
        reusable_artifacts=_snapshot_reusable_artifacts(state),
        artifact_count=len(state.artifacts),
        stale_artifact_count=sum(1 for artifact in state.artifacts if artifact.reuse_policy == "stale"),
        side_effects=_snapshot_side_effects(state),
        side_effect_count=len(state.side_effects),
        pending_confirmation_count=sum(
            1 for side_effect in state.side_effects if side_effect.effect_level == "pending_confirmation"
        ),
        committed_side_effect_count=sum(
            1 for side_effect in state.side_effects if side_effect.effect_level == "committed"
        ),
        compensatable_side_effect_count=sum(
            1 for side_effect in state.side_effects if side_effect.effect_level == "compensatable"
        ),
        pending_tool=state.pending_tool,
        tts_state=state.tts_state,
        last_spoken_progress=state.last_spoken_progress,
        speech_turn_id=state.speech_turn_id,
        barge_in_source=state.barge_in_source,
        last_realtime_event_ids=list(state.last_realtime_event_ids),
    )


def format_realtime_task_state_snapshot(snapshot: RealtimeTaskStateSnapshot) -> str:
    """Render a compact human-readable snapshot for prompt/context debugging."""

    lines = [
        f"schema_version: {snapshot.schema_version}",
        f"task_id: {snapshot.task_id}",
        f"status: {snapshot.status}",
        f"objective: {snapshot.objective}",
    ]
    if snapshot.current_user_text:
        lines.append(f"current_user_text: {snapshot.current_user_text}")
    if snapshot.constraints:
        lines.append("constraints:")
        lines.extend(f"- {constraint}" for constraint in snapshot.constraints)
    if snapshot.latest_revision is not None:
        revision_text = str(snapshot.latest_revision.get("user_text") or "")
        strategy = str(snapshot.latest_revision.get("strategy") or "")
        lines.append(f"latest_revision: {revision_text}")
        lines.append(f"revision_strategy: {strategy}")
    if snapshot.pending_tool:
        tool_name = str(snapshot.pending_tool.get("tool_name") or "tool")
        status = str(snapshot.pending_tool.get("status") or "working")
        lines.append(f"pending_tool: {tool_name} [{status}]")
    if snapshot.tts_state != "idle":
        lines.append(f"tts_state: {snapshot.tts_state}")
    if snapshot.last_spoken_progress:
        spoken_text = str(snapshot.last_spoken_progress.get("text") or "")
        lines.append(f"last_spoken_progress: {spoken_text}")
    if snapshot.speech_turn_id:
        lines.append(f"speech_turn_id: {snapshot.speech_turn_id}")
    if snapshot.barge_in_source:
        lines.append(f"barge_in_source: {snapshot.barge_in_source}")
    if snapshot.reusable_artifacts:
        lines.append("reusable_artifacts:")
        for artifact in snapshot.reusable_artifacts:
            tool_name = str(artifact.get("tool_name") or artifact.get("kind") or "artifact")
            summary = str(artifact.get("summary") or "")
            lines.append(f"- {tool_name}: {summary}")
    if snapshot.side_effects:
        lines.append("side_effects:")
        for side_effect in snapshot.side_effects:
            tool_name = str(side_effect.get("tool_name") or "tool")
            level = str(side_effect.get("effect_level") or "unknown")
            summary = str(side_effect.get("summary") or "")
            confirmation_id = str(side_effect.get("confirmation_id") or "")
            suffix = f", confirmation_id={confirmation_id}" if confirmation_id else ""
            lines.append(f"- {tool_name} [{level}]{suffix}: {summary}")
    return "\n".join(lines)


def _updated_state_for_request(
    request: UserRequest,
    state: RealtimeTaskState | None,
) -> RealtimeTaskState:
    text = _clip_text(request.text or "")
    metadata = request.metadata
    realtime = metadata.get("realtime")
    turn_id = _metadata_string(realtime.get("turn_id")) if isinstance(realtime, dict) else None
    run_id = _metadata_string(realtime.get("run_id")) if isinstance(realtime, dict) else None
    interrupt = realtime_metadata_requests_interrupt(metadata)
    speech_turn_id = _speech_turn_id_from_request(request, realtime=realtime, turn_id=turn_id)
    now = _utc_now()

    if state is None:
        state = RealtimeTaskState(
            task_id=_task_id(request.user_id, request.session_id),
            user_id=request.user_id,
            session_id=request.session_id,
            created_at=now,
            updated_at=now,
            objective=text,
            latest_user_text=text,
            latest_turn_id=turn_id,
            latest_run_id=run_id,
            speech_turn_id=speech_turn_id,
            status="revising" if interrupt else "active",
        )
    else:
        state.updated_at = now
        state.latest_user_text = text
        state.latest_turn_id = turn_id
        state.latest_run_id = run_id
        state.speech_turn_id = speech_turn_id or state.speech_turn_id
        if not state.objective:
            state.objective = text
        state.status = "revising" if interrupt else "active"

    if turn_id:
        state.source_turn_ids = _append_unique_limited(state.source_turn_ids, turn_id)
    if run_id:
        state.source_run_ids = _append_unique_limited(state.source_run_ids, run_id)

    if interrupt and text:
        if _interrupt_invalidates_artifacts(text):
            state.artifacts = [_stale_artifact(artifact) for artifact in state.artifacts]
        strategy = _select_continuation_strategy(state)
        state.continuation_strategy = strategy
        state.tts_state = "interrupted"
        state.barge_in_source = _barge_in_source_from_request(request)
        state.constraints = _append_unique_limited(state.constraints, text)
        state.revisions.append(
            IntentRevision(
                revision_id=f"rev_{len(state.revisions) + 1}",
                task_id=state.task_id,
                turn_id=turn_id,
                run_id=run_id,
                user_text=text,
                revision_type="add_constraint",
                strategy=strategy,
                created_at=now,
                metadata={"source": "realtime_interrupt"},
            )
        )
    elif not interrupt:
        state.continuation_strategy = None

    return state


def _tool_artifacts_from_state(state: Any, *, task_state: RealtimeTaskState) -> list[TaskArtifact]:
    artifacts: list[TaskArtifact] = []
    request = getattr(state, "request", None)
    request_text = request.text if isinstance(request, UserRequest) else None
    run_id = _metadata_string(getattr(state, "run_id", None))
    tool_results = getattr(state, "tool_results", [])
    for index, result in enumerate(tool_results):
        if not isinstance(result, ToolResult) or not result.success:
            continue
        observation = observation_from_tool_result(result, request_text=request_text)
        context = compact_observation_for_context(observation.model_dump(mode="json"))
        summary = _clip_text(str(context.get("summary") or observation.summary), max_chars=360)
        if not summary:
            continue
        artifacts.append(
            TaskArtifact(
                artifact_id=_artifact_id(
                    task_state.task_id,
                    "tool",
                    run_id or "run",
                    str(index),
                    result.tool_name,
                ),
                task_id=task_state.task_id,
                run_id=run_id,
                kind="observation",
                reuse_policy=_reuse_policy_for_tool(result.tool_name),
                summary=summary,
                tool_name=result.tool_name,
                output_ref=result.output_ref,
                context=context,
            )
        )
    return artifacts


def _checkpoint_artifacts_from_tool_artifacts(
    tool_artifacts: list[TaskArtifact],
    *,
    task_state: RealtimeTaskState,
    run_id: str | None,
) -> list[TaskArtifact]:
    reusable_steps = [
        artifact
        for artifact in tool_artifacts
        if artifact.kind == "observation"
        and artifact.reuse_policy == "reusable"
        and artifact.tool_name in _REUSABLE_TOOL_NAMES
    ]
    if len(reusable_steps) < 2:
        return []

    refs = [
        {
            "tool_name": artifact.tool_name,
            "output_ref": artifact.output_ref,
            "summary": _checkpoint_artifact_summary(artifact),
        }
        for artifact in reusable_steps[:_MAX_CHECKPOINT_ARTIFACT_REFS]
    ]
    completed_tools = [
        str(artifact.tool_name)
        for artifact in reusable_steps[:_MAX_CHECKPOINT_ARTIFACT_REFS]
        if artifact.tool_name
    ]
    context = {
        "schema_version": "realtime_checkpoint_v1",
        "completed_step_count": len(reusable_steps),
        "completed_tools": completed_tools,
        "artifact_refs": refs,
    }
    return [
        TaskArtifact(
            artifact_id=_artifact_id(
                task_state.task_id,
                "checkpoint",
                run_id or "run",
                str(len(reusable_steps)),
            ),
            task_id=task_state.task_id,
            run_id=run_id,
            kind="checkpoint",
            reuse_policy="reusable",
            summary=f"Completed {len(reusable_steps)} reusable tool steps.",
            context=context,
        )
    ]


def _checkpoint_artifact_summary(artifact: TaskArtifact) -> str:
    structured_output = artifact.context.get("structured_output")
    if artifact.tool_name == "product_search" and isinstance(structured_output, dict):
        items = structured_output.get("items")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                title = _metadata_string(first.get("title"))
                if title:
                    return _clip_text(title, max_chars=180)
    summary = _metadata_string(artifact.context.get("summary")) or artifact.summary
    return _clip_text(summary, max_chars=180)


def _side_effect_records_from_state(state: Any, *, task_state: RealtimeTaskState) -> list[SideEffectRecord]:
    records: list[SideEffectRecord] = []
    request = getattr(state, "request", None)
    request_text = request.text if isinstance(request, UserRequest) else None
    run_id = _metadata_string(getattr(state, "run_id", None))
    tool_results = getattr(state, "tool_results", [])
    for index, result in enumerate(tool_results):
        if not isinstance(result, ToolResult):
            continue
        record = _side_effect_record_from_result(
            result,
            task_state=task_state,
            run_id=run_id,
            index=index,
            request_text=request_text,
        )
        if record is not None:
            records.append(record)
    return records


def _side_effect_record_from_result(
    result: ToolResult,
    *,
    task_state: RealtimeTaskState,
    run_id: str | None,
    index: int,
    request_text: str | None,
) -> SideEffectRecord | None:
    policy = _side_effect_policy_from_result(result)
    data = result.data if isinstance(result.data, dict) else {}
    pending_confirmation = _result_requires_confirmation(data)
    if not result.success and not pending_confirmation:
        return None

    effect_level = _effect_level_for_result(
        result,
        policy=policy,
        pending_confirmation=pending_confirmation,
    )
    if effect_level == "none":
        return None

    observation = observation_from_tool_result(result, request_text=request_text)
    confirmation_id = _metadata_string(data.get("confirmation_id"))
    summary = _side_effect_summary(result, policy=policy, observation_summary=observation.summary)
    return SideEffectRecord(
        record_id=_artifact_id(
            task_state.task_id,
            "effect",
            run_id or "run",
            str(index),
            result.tool_name,
        ),
        task_id=task_state.task_id,
        run_id=run_id,
        tool_name=result.tool_name,
        effect_level=effect_level,
        requires_confirmation=effect_level == "pending_confirmation",
        confirmation_id=confirmation_id,
        compensation_hint=_metadata_string(data.get("compensation_hint")) or policy.compensation_hint,
        summary=summary,
        output_ref=result.output_ref,
    )


def _side_effect_policy_from_result(result: ToolResult) -> ToolSideEffectPolicy:
    data = result.data if isinstance(result.data, dict) else {}
    side_effect_payload = data.get("side_effect")
    if isinstance(side_effect_payload, dict):
        return ToolSideEffectPolicy.model_validate(side_effect_payload)
    policy = tool_side_effect_policy(result.tool_name)
    explicit_level = _metadata_string(data.get("side_effect_level") or data.get("effect_level"))
    if explicit_level in {
        "none",
        "local_read",
        "external_read",
        "pending_confirmation",
        "committed",
        "compensatable",
    }:
        return policy.model_copy(update={"level": explicit_level}, deep=True)
    return policy


def _result_requires_confirmation(data: dict[str, Any]) -> bool:
    return data.get("requires_confirmation") is True or bool(_metadata_string(data.get("confirmation_id")))


def _effect_level_for_result(
    result: ToolResult,
    *,
    policy: ToolSideEffectPolicy,
    pending_confirmation: bool,
) -> ToolSideEffectLevel:
    if pending_confirmation:
        return "pending_confirmation"
    if result.success and policy.level == "pending_confirmation":
        return "committed"
    return policy.level


def _side_effect_summary(
    result: ToolResult,
    *,
    policy: ToolSideEffectPolicy,
    observation_summary: str,
) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    summary = (
        _metadata_string(result.voice_summary)
        or _metadata_string(data.get("side_effect_summary"))
        or _metadata_string(data.get("summary"))
        or _metadata_string(policy.description)
        or observation_summary
    )
    return _clip_text(summary, max_chars=360)


def _media_artifacts_from_request(
    request: UserRequest,
    *,
    task_state: RealtimeTaskState,
    run_id: str | None,
) -> list[TaskArtifact]:
    artifacts: list[TaskArtifact] = []
    media_groups: tuple[tuple[str, list[str]], ...] = (
        ("image_ids", list(request.image_ids)),
        ("video_ids", list(request.video_ids)),
        ("audio_id", [request.audio_id] if request.audio_id else []),
    )
    for media_type, refs in media_groups:
        refs = [ref for ref in refs if isinstance(ref, str) and ref]
        if not refs:
            continue
        summary = f"User supplied {media_type}: {', '.join(refs[:3])}"
        artifacts.append(
            TaskArtifact(
                artifact_id=_artifact_id(
                    task_state.task_id,
                    "media",
                    run_id or "run",
                    media_type,
                ),
                task_id=task_state.task_id,
                run_id=run_id,
                kind="media_ref",
                reuse_policy="reusable",
                summary=summary,
                context={"media_type": media_type, "refs": refs[:6]},
            )
        )
    return artifacts


def _merge_artifacts(existing: list[TaskArtifact], new_artifacts: list[TaskArtifact]) -> list[TaskArtifact]:
    by_id = {artifact.artifact_id: artifact for artifact in existing}
    for artifact in new_artifacts:
        by_id[artifact.artifact_id] = artifact
    return list(by_id.values())[-_MAX_ARTIFACTS:]


def _merge_side_effects(
    existing: list[SideEffectRecord],
    new_side_effects: list[SideEffectRecord],
) -> list[SideEffectRecord]:
    by_id = {side_effect.record_id: side_effect for side_effect in existing}
    for side_effect in new_side_effects:
        by_id[side_effect.record_id] = side_effect
    return list(by_id.values())[-_MAX_SIDE_EFFECTS:]


def _reuse_policy_for_tool(tool_name: str) -> ArtifactReusePolicy:
    if tool_name in _REUSABLE_TOOL_NAMES:
        return "reusable"
    if tool_name in _REQUIRES_VALIDATION_TOOL_NAMES:
        return "requires_validation"
    if tool_name in _DO_NOT_REUSE_TOOL_NAMES:
        return "do_not_reuse"
    return "requires_validation"


def _select_continuation_strategy(state: RealtimeTaskState) -> ContinuationStrategy:
    side_effect_strategy = _side_effect_continuation_strategy(state)
    if side_effect_strategy is not None:
        return side_effect_strategy
    reusable_artifacts = [artifact for artifact in state.artifacts if artifact.reuse_policy == "reusable"]
    if any(artifact.kind == "checkpoint" for artifact in reusable_artifacts):
        return "resume_from_checkpoint"
    if any(
        artifact.kind in {"observation", "media_ref"} or artifact.tool_name in _REUSABLE_TOOL_NAMES
        for artifact in reusable_artifacts
    ):
        return "reuse_and_replan"
    return "restart"


def _side_effect_continuation_strategy(state: RealtimeTaskState) -> ContinuationStrategy | None:
    if any(side_effect.effect_level == "committed" for side_effect in state.side_effects):
        return "report_committed"
    if any(side_effect.effect_level == "pending_confirmation" for side_effect in state.side_effects):
        return "ask_confirmation"
    if any(side_effect.effect_level == "compensatable" for side_effect in state.side_effects):
        return "compensate"
    return None


def _snapshot_reusable_artifacts(state: RealtimeTaskState) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for artifact in state.artifacts:
        if artifact.reuse_policy != "reusable":
            continue
        artifacts.append(
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "reuse_policy": artifact.reuse_policy,
                "tool_name": artifact.tool_name,
                "output_ref": artifact.output_ref,
                "summary": artifact.summary,
                "context": artifact.context,
            }
        )
    return artifacts[-_MAX_SNAPSHOT_ARTIFACTS:]


def _snapshot_side_effects(state: RealtimeTaskState) -> list[dict[str, Any]]:
    side_effects: list[dict[str, Any]] = []
    for side_effect in state.side_effects:
        side_effects.append(
            {
                "record_id": side_effect.record_id,
                "tool_name": side_effect.tool_name,
                "effect_level": side_effect.effect_level,
                "requires_confirmation": side_effect.requires_confirmation,
                "confirmation_id": side_effect.confirmation_id,
                "compensation_hint": side_effect.compensation_hint,
                "summary": side_effect.summary,
                "output_ref": side_effect.output_ref,
            }
        )
    return side_effects[-_MAX_SNAPSHOT_SIDE_EFFECTS:]


def _interrupt_invalidates_artifacts(text: str) -> bool:
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in _INVALIDATE_ARTIFACT_MARKERS)


def _stale_artifact(artifact: TaskArtifact) -> TaskArtifact:
    if artifact.reuse_policy != "reusable":
        return artifact
    return artifact.model_copy(update={"reuse_policy": "stale"}, deep=True)


def _pending_tool_from_event(
    payload: dict[str, Any],
    *,
    default_status: str,
) -> dict[str, Any] | None:
    tool_name = _metadata_string(payload.get("tool_name") or payload.get("name"))
    current_step = _metadata_string(payload.get("current_step") or payload.get("step_id")) or tool_name
    if tool_name is None and current_step is None:
        return None
    result: dict[str, Any] = {
        "tool_name": tool_name or current_step,
        "status": _metadata_string(payload.get("status")) or default_status,
    }
    if current_step:
        result["current_step"] = current_step
    run_id = _metadata_string(payload.get("run_id"))
    if run_id:
        result["run_id"] = run_id
    pre_tool_call = payload.get("pre_tool_call")
    if isinstance(pre_tool_call, dict):
        side_effect = pre_tool_call.get("side_effect")
        if isinstance(side_effect, dict):
            result["side_effect"] = {
                key: value
                for key, value in side_effect.items()
                if key in {"level", "requires_confirmation", "confirmation_kind", "compensation_hint"}
            }
        confirmation = pre_tool_call.get("confirmation")
        if isinstance(confirmation, dict) and isinstance(confirmation.get("required"), bool):
            result["requires_confirmation"] = confirmation["required"]
        risk_gate = pre_tool_call.get("risk_gate")
        if isinstance(risk_gate, dict):
            result["risk_gate"] = {
                key: value
                for key, value in risk_gate.items()
                if key
                in {
                    "schema_version",
                    "level",
                    "side_effect_level",
                    "enabled",
                    "allow_execute",
                    "requires_confirmation",
                    "confirmation_kind",
                    "reason",
                }
            }
        idempotency = pre_tool_call.get("idempotency")
        if isinstance(idempotency, dict):
            result["idempotency"] = {
                key: value
                for key, value in idempotency.items()
                if key in {"key", "present", "required", "generated", "duplicate_suppressed", "status"}
            }
    return result


def _spoken_progress_from_event(
    *,
    text: str | None,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    message = _metadata_string(text) or _metadata_string(payload.get("message"))
    if message is None:
        return None

    progress: dict[str, Any] = {"text": _clip_text(message, max_chars=240)}
    for key in ("source", "current_step"):
        value = _metadata_string(payload.get(key))
        if value is not None:
            progress[key] = value
    for key in ("replaceable", "display_only"):
        if isinstance(payload.get(key), bool):
            progress[key] = payload[key]
    return progress


def _pending_tool_matches(pending_tool: dict[str, Any] | None, tool_name: str | None) -> bool:
    if pending_tool is None:
        return False
    if tool_name is None:
        return True
    return pending_tool.get("tool_name") == tool_name or pending_tool.get("current_step") == tool_name


def _speech_turn_id_from_request(
    request: UserRequest,
    *,
    realtime: Any,
    turn_id: str | None,
) -> str | None:
    metadata = request.metadata
    for value in (
        metadata.get("speech_turn_id"),
        realtime.get("speech_turn_id") if isinstance(realtime, dict) else None,
    ):
        parsed = _metadata_string(value)
        if parsed:
            return parsed
    return request.audio_id or turn_id


def _barge_in_source_from_request(request: UserRequest) -> RealtimeBargeInSource:
    metadata = request.metadata
    explicit = _metadata_string(metadata.get("barge_in_source"))
    if explicit in {"transcript", "explicit_cancel", "hangup", "media_relay_control", "unknown"}:
        return explicit  # type: ignore[return-value]

    cancel_source = _metadata_string(metadata.get("cancel_source"))
    if cancel_source == "gateway_hangup":
        return "hangup"
    if cancel_source in {"gateway_cancel", "gateway_interrupt"}:
        return "explicit_cancel"

    source = _metadata_string(metadata.get("source"))
    if source == "realtime_media_websocket":
        if request.audio_id or request.text:
            return "transcript"
        return "media_relay_control"

    control = _metadata_string(metadata.get("control"))
    if control in {"interrupt", "barge_in", "cancel_previous"}:
        return "explicit_cancel"
    return "unknown"


def _artifact_id(task_id: str, *parts: str) -> str:
    cleaned_parts = [part.replace(":", "_").replace("/", "_") for part in parts if part]
    return ":".join([task_id, *cleaned_parts])


def _append_unique_limited(values: list[str], value: str) -> list[str]:
    next_values = [item for item in values if item != value]
    next_values.append(value)
    return next_values[-_MAX_SOURCE_IDS:]


def _task_id(user_id: str, session_id: str) -> str:
    return f"rtask:{user_id}:{session_id}"


def _metadata_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clip_text(value: str, max_chars: int = _MAX_TEXT_CHARS) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15].rstrip() + "...[trimmed]"
