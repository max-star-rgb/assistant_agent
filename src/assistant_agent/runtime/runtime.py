"""Default LangGraph runtime for agent execution."""

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, time
from typing import TYPE_CHECKING, Any, Literal

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.cancellation import AgentRunCancelled, raise_if_cancelled
from assistant_agent.runtime.assistant_graph_app import (
    AssistantTurnGraphApp,
    GraphExecutionIdentity,
)
from assistant_agent.runtime.assistant_interrupts import AssistantInterruptRequest
from assistant_agent.runtime.assistant_graph_state import (
    apply_assistant_turn_state_to_agent_state,
    assistant_turn_state_from_loop_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.runtime.run_phase import RunPhase
from assistant_agent.runtime.state import AgentError, AgentState
from assistant_agent.runtime.event_stream import AgentRunStream, AsyncQueueEventSink
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_execution_backend import ToolExecutionBackend
from assistant_agent.runtime.tool_operation_barrier import (
    SQLiteToolOperationStore,
    ToolOperationStore,
)
from assistant_agent.runtime.provider_streaming import (
    ProviderStreamingTurnRunner,
    supports_async_streaming_chat,
)
from assistant_agent.memory.factory import create_long_term_memory_service
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.decision_models import (
    native_tool_call_to_assistant_decision,
)
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.event_publisher import (
    RunStartedFact,
    RunTerminalFact,
    RuntimeEventPublisher,
)
from assistant_agent.automation.durable_tasks.models import (
    DurableTaskSnapshot,
    TaskCheckpoint,
    TrustedTaskBinding,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.identifiers import new_run_id
from assistant_agent.runtime.requests import (
    AgentResponse,
    UserRequest,
    normalize_task_execution_mode,
)
from assistant_agent.runtime.generated_artifacts import with_generated_artifact_delivery
from assistant_agent.multi_agent.models import DEFAULT_AGENT_ID
from assistant_agent.observability.trace_context import (
    RuntimeExportTraceContext,
    RuntimeTraceContext,
)
from assistant_agent.observability.workflow_otel import (
    create_workflow_otel_observer_from_env,
    workflow_attempt_span_id,
)
from assistant_agent.observability.workflow_trace import workflow_root_span_id
from assistant_agent.observability.otel_mapping import langfuse_trace_id
from assistant_agent.tools.models import ToolResult, ToolSpec
from assistant_agent.media.agent_service_entry import is_trusted_agent_service_request
from assistant_agent.runtime.event_sink import EventSink
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.models import (
    WorkflowStepAcceptanceContract,
    WorkflowSubmission,
)
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.observed_store import ObservedWorkflowStore
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.runtime.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    ChatResult,
    create_chat_adapter,
)
from assistant_agent.context.observability import build_traced_assistant_context_pack
from assistant_agent.context.compactor import ContextCompactor, create_context_compactor
from assistant_agent.context.token_counter import (
    ContextTokenCounter,
    create_context_token_counter,
    create_visual_context_token_counter,
)
from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.context.service import ContextService
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.media.video.realtime_video_memory import (
    project_realtime_video_context,
)
from assistant_agent.media.video.visual_context_compactor import (
    create_visual_context_compactor,
)
from assistant_agent.media.video.visual_timeline_compactor import (
    create_visual_timeline_compactor,
)
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineContextService,
)
from assistant_agent.context.soul_source import (
    SOUL_COMPILED_MAX_CHARS,
    SOUL_SOURCE_ID,
    SoulContextSource,
)
from assistant_agent.context.user_profile_source import (
    USER_PROFILE_COMPILED_MAX_CHARS,
    USER_PROFILE_SOURCE_ID,
    UserProfileContextSource,
)
from assistant_agent.context.sources import (
    ContextSourceCoordinator,
    ContextSourceRequest,
)
from assistant_agent.runtime.run_history import RunHistoryStore
from assistant_agent.runtime.session_store import SessionStore, create_session_store
from assistant_agent.runtime.capability_grants import CapabilityGrantController
from assistant_agent.skills.loading import default_repo_root
from assistant_agent.tools.ids import (
    LIVE_VIEW_INSPECT_TOOL_NAME,
    VISUAL_MEMORY_SEARCH_TOOL_NAME,
)
from assistant_agent.observability.trace_store import (
    InMemoryTraceStore,
    TraceStore,
    append_observability_event,
)
from assistant_agent.observability.turn_summary import append_runtime_turn_summary
from assistant_agent.media.video.video_context import (
    InMemoryVideoContextStore,
    VideoContextStore,
)
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.visual_reminder import VisualReminderRegistry
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.qdrant_visual_memory_index import (
    create_visual_memory_text_index,
)
from assistant_agent.media.video.visual_memory_index import VisualMemoryTextIndex
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.provider import (
    create_multimodal_embedding_provider,
)
from assistant_agent.media.embedding.consumers.alignment import (
    CrossModalAlignmentConsumer,
)
from assistant_agent.media.embedding.consumers.attention import VisualAttentionConsumer
from assistant_agent.media.embedding.models import TextObservation
from assistant_agent.media.embedding.observability import (
    EmbeddingObserver,
    LoggingEmbeddingObserver,
)
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from assistant_agent.automation.durable_tasks.worker import TaskQuantumResult


def _trusted_memory_session_metadata(
    session_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(session_config, Mapping):
        return {}
    try:
        entry_profile = session_config.get("entry_profile")
    except Exception:
        return {}
    if type(entry_profile) is not str or not 0 < len(entry_profile) <= 128:
        return {}
    return {
        "gateway": {
            "session_config": {"entry_profile": entry_profile},
        }
    }


def _stable_text_observation(
    session_id: str,
    run_id: str,
    text_value: str | None,
    *,
    now_ms: int | None = None,
) -> TextObservation | None:
    """Normalize already-final request text; audio/ASR remains upstream of this boundary."""

    normalized = (text_value or "").strip()
    if not normalized:
        return None
    return TextObservation(
        session_id=session_id,
        observation_id=run_id,
        text=normalized[:4_000],
        source="user_request",
        occurred_at_ms=now_ms if now_ms is not None else int(time() * 1000),
        final=True,
    )


@dataclass
class _PreparedGraphRun:
    request: UserRequest
    state: AgentState
    workflow_work_item: bool
    deep_research_start: bool
    run_event_sink: EventSink | None
    runtime_event_publisher: RuntimeEventPublisher
    run_started_at: float
    runtime_context: GraphRuntimeContext
    initial_state: dict[str, Any]
    identity: GraphExecutionIdentity
    cancel_token: Any | None
    pre_terminal_state_hook: Callable[[AgentState], None] | None


class AgentGraphRuntime:
    """Run agent requests through the compiled LangGraph workflow."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        long_term_memory_service: LongTermMemoryService | None = None,
        config: ProviderConfig | None = None,
        run_history: RunHistoryStore | None = None,
        session_store: SessionStore | None = None,
        event_sink: EventSink | None = None,
        trace_store: TraceStore | None = None,
        chat_adapter: ChatAdapter | None = None,
        context_compactor: ContextCompactor | None = None,
        context_token_counter: ContextTokenCounter | None = None,
        video_context_store: VideoContextStore | None = None,
        realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
        embedding_coordinator_store: SessionEmbeddingCoordinatorStore | None = None,
        embedding_observer: EmbeddingObserver | None = None,
        visual_semantic_store_pool: SessionVisualSemanticStorePool | None = None,
        visual_reminder_registry: VisualReminderRegistry | None = None,
        visual_memory_text_index: VisualMemoryTextIndex | None = None,
        checkpointer: Any | None = None,
        context_source_coordinator: ContextSourceCoordinator | None = None,
        durable_task_service: DurableTaskService | None = None,
        workflow_service: WorkflowService | None = None,
        workflow_artifact_store: LocalWorkflowArtifactStore | None = None,
        agent_id: str = DEFAULT_AGENT_ID,
        tool_execution_backend: ToolExecutionBackend | None = None,
        tool_operation_store: ToolOperationStore | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.tool_execution_backend = tool_execution_backend
        self.tool_operation_store = tool_operation_store or SQLiteToolOperationStore(
            Path(".local") / "langgraph" / "tool_operations.sqlite3"
        )
        self.config = config or ProviderConfig.from_env()
        self.video_context_store = video_context_store or InMemoryVideoContextStore()
        self.realtime_video_memory_store = (
            realtime_video_memory_store or RealtimeVideoMemoryStore()
        )
        self.embedding_provider = create_multimodal_embedding_provider(self.config)
        self.embedding_observer = embedding_observer or LoggingEmbeddingObserver()
        self.embedding_coordinator_store = (
            embedding_coordinator_store
            or SessionEmbeddingCoordinatorStore(
                factory=self._create_session_embedding_coordinator
            )
        )
        self.visual_semantic_store_pool = (
            visual_semantic_store_pool
            or SessionVisualSemanticStorePool(
                root=Path(".data") / "visual_semantic_memory",
                observer=self.embedding_observer,
            )
        )
        self.visual_memory_text_index = (
            visual_memory_text_index or create_visual_memory_text_index(self.config)
        )
        self.visual_reminder_registry = (
            visual_reminder_registry
            or VisualReminderRegistry(
                delivery_timeout_seconds=(
                    self.config.proactive_message_delivery_timeout_seconds
                )
            )
        )
        self.long_term_memory_service = (
            long_term_memory_service or create_long_term_memory_service(self.config)
        )
        self.workflow_service = workflow_service
        self.workflow_artifact_store = workflow_artifact_store
        if self.config.durable_workflows_enabled:
            if self.workflow_service is None:
                workflow_store = SQLiteWorkflowStore(self.config.durable_workflow_path)
                workflow_observer = create_workflow_otel_observer_from_env()
                if workflow_observer is not None:
                    workflow_store = ObservedWorkflowStore(
                        inner=workflow_store,
                        observer=workflow_observer,
                    )
                self.workflow_service = WorkflowService(
                    store=workflow_store,
                    definitions=default_workflow_definitions(),
                )
            self.workflow_artifact_store = (
                self.workflow_artifact_store
                or LocalWorkflowArtifactStore(
                    self.config.durable_workflow_artifact_path
                )
            )
        self.durable_task_service = durable_task_service
        self.notification_outbox_store = None
        if self.config.durable_tasks_enabled and (
            self.durable_task_service is None
            or self.durable_task_service.notification_outbox is None
        ):
            from assistant_agent.automation.proactive_wake.store import (
                SQLiteProactiveWakeStore,
            )

            self.notification_outbox_store = SQLiteProactiveWakeStore(
                self.config.durable_notification_path
            )
        if registry is None:
            if self.config.durable_tasks_enabled and self.durable_task_service is None:
                bootstrap_registry = ToolRegistry()
                self.durable_task_service = DurableTaskService(
                    store=SQLiteTaskStore(self.config.durable_task_path),
                    registry=bootstrap_registry,
                    max_plan_steps=self.config.max_plan_steps,
                    lease_seconds=self.config.durable_task_lease_seconds,
                    max_task_seconds=self.config.durable_task_max_seconds,
                    max_workflow_quanta=(self.config.durable_workflow_max_quanta),
                    notification_outbox=self.notification_outbox_store,
                )
            self.registry = create_default_registry(
                self.config,
                video_context_store=self.video_context_store,
                realtime_video_memory_store=self.realtime_video_memory_store,
                embedding_coordinator_store=self.embedding_coordinator_store,
                visual_semantic_store_pool=self.visual_semantic_store_pool,
                visual_reminder_registry=self.visual_reminder_registry,
                visual_memory_text_index=self.visual_memory_text_index,
                durable_task_service=self.durable_task_service,
            )
            if self.durable_task_service is not None:
                self.durable_task_service.registry = self.registry
        else:
            self.registry = registry
            if self.config.durable_tasks_enabled:
                self.durable_task_service = (
                    self.durable_task_service
                    or DurableTaskService(
                        store=SQLiteTaskStore(self.config.durable_task_path),
                        registry=self.registry,
                        max_plan_steps=self.config.max_plan_steps,
                        lease_seconds=self.config.durable_task_lease_seconds,
                        max_task_seconds=self.config.durable_task_max_seconds,
                        max_workflow_quanta=(self.config.durable_workflow_max_quanta),
                        notification_outbox=self.notification_outbox_store,
                    )
                )
        if (
            self.durable_task_service is not None
            and self.durable_task_service.notification_outbox is None
        ):
            self.durable_task_service.notification_outbox = (
                self.notification_outbox_store
            )
        registry_get = getattr(self.registry, "get", None)
        if registry is not None and callable(registry_get):
            try:
                vision_tool = registry_get(LIVE_VIEW_INSPECT_TOOL_NAME)
            except KeyError:
                pass
            else:
                if getattr(vision_tool, "memory_store", None) is None:
                    vision_tool.memory_store = self.realtime_video_memory_store
                if getattr(vision_tool, "semantic_store_pool", None) is None:
                    vision_tool.semantic_store_pool = self.visual_semantic_store_pool
        self.run_history = run_history
        self.session_store = session_store or create_session_store(self.config)
        self.capability_grant_controller = CapabilityGrantController(
            session_store=self.session_store,
            skill_root=default_repo_root(),
            registered_tool_specs=self.registry.list_specs(),
        )
        self.event_sink = event_sink
        self.trace_store = trace_store or InMemoryTraceStore()
        self.visual_reminder_registry.set_trace_store(self.trace_store)
        self.chat_adapter = chat_adapter or create_chat_adapter(self.config)
        self.visual_context_token_counter = create_visual_context_token_counter(
            self.config
        )
        self.visual_context_compactor = create_visual_context_compactor(
            self.config,
            self.chat_adapter,
            token_counter=self.visual_context_token_counter,
        )
        self.visual_context_window_policy = ContextWindowPolicy(
            input_token_limit=self.config.visual_context_input_token_limit,
            trigger_ratio=self.config.visual_context_compaction_trigger_ratio,
            target_ratio=self.config.visual_context_compaction_target_ratio,
            hard_ratio=self.config.visual_context_compaction_hard_ratio,
            safety_margin_tokens=(
                self.config.visual_context_compaction_safety_margin_tokens
            ),
            summary_max_tokens=self.config.visual_context_summary_max_tokens,
        )
        self.visual_timeline_compactor = create_visual_timeline_compactor(
            self.config,
            self.chat_adapter,
            token_counter=self.visual_context_token_counter,
        )
        self.visual_timeline_context_service = (
            VisualTimelineContextService(
                compactor=self.visual_timeline_compactor,
                token_counter=self.visual_context_token_counter,
                window_policy=self.visual_context_window_policy,
                keep_recent_observations=(
                    self.config.visual_context_keep_recent_records
                ),
            )
            if self.visual_timeline_compactor is not None
            and self.visual_context_token_counter is not None
            else None
        )
        try:
            visual_memory_tool = self.registry.get(VISUAL_MEMORY_SEARCH_TOOL_NAME)
        except KeyError:
            pass
        else:
            configure_timeline_context = getattr(
                visual_memory_tool,
                "configure_timeline_context_service",
                None,
            )
            if callable(configure_timeline_context):
                configure_timeline_context(self.visual_timeline_context_service)
        self.context_token_counter = (
            context_token_counter
            if context_token_counter is not None
            else create_context_token_counter(self.config)
        )
        self.context_compactor = (
            context_compactor
            if context_compactor is not None
            else create_context_compactor(
                self.config,
                self.chat_adapter,
                token_counter=self.context_token_counter,
            )
        )
        if self.context_compactor is not None and self.context_token_counter is None:
            raise ValueError("context compaction requires a model tokenizer")
        self.context_window_policy = ContextWindowPolicy(
            input_token_limit=self.config.context_input_token_limit,
            trigger_ratio=self.config.context_compaction_trigger_ratio,
            target_ratio=self.config.context_compaction_target_ratio,
            hard_ratio=self.config.context_compaction_hard_ratio,
            safety_margin_tokens=self.config.context_compaction_safety_margin_tokens,
            summary_max_tokens=self.config.context_summary_max_tokens,
        )
        self.context_service = ContextService(
            compactor=self.context_compactor,
            token_counter=self.context_token_counter,
            window_policy=self.context_window_policy,
            current_location=self.config.current_location,
            supports_developer_role=bool(
                getattr(
                    getattr(self.chat_adapter, "capabilities", None),
                    "supports_developer_role",
                    False,
                )
            ),
            chat_max_tokens=self.config.chat_max_tokens,
            deep_research_chat_max_tokens=(self.config.deep_research_chat_max_tokens),
        )
        self.context_source_coordinator = (
            context_source_coordinator
            or ContextSourceCoordinator(
                [SoulContextSource(), UserProfileContextSource()]
            )
        )
        self.tool_executor = ToolExecutor(
            registry=self.registry,
            event_sink=self.event_sink,
            context_metadata={
                "durable_task_service": self.durable_task_service,
            },
            execution_backend=self.tool_execution_backend,
            operation_store=self.tool_operation_store,
        )
        self.assistant_graph_app = AssistantTurnGraphApp(checkpointer=checkpointer)

    def _create_session_embedding_coordinator(
        self,
        user_id: str,
        session_id: str,
    ) -> SessionEmbeddingCoordinator:
        _ = user_id
        coordinator = SessionEmbeddingCoordinator(
            session_id,
            self.embedding_provider,
            observer=self.embedding_observer,
        )
        coordinator.cross_modal_alignment_consumer = CrossModalAlignmentConsumer()
        coordinator.visual_attention_consumer = VisualAttentionConsumer()
        coordinator.register_consumer(
            coordinator.cross_modal_alignment_consumer,
            queue_size=128,
            overflow_policy="drop_oldest",
        )
        coordinator.register_consumer(
            coordinator.visual_attention_consumer,
            queue_size=64,
            overflow_policy="latest_wins",
        )
        return coordinator

    def initialize_session_memory(
        self,
        identity: RequestIdentity,
        *,
        reset: bool = False,
        session_config: Mapping[str, Any] | None = None,
    ) -> AgentState:
        """Recall and freeze long-term memory before any turn starts."""

        if not identity.session_id:
            raise ValueError("session_id is required to initialize session memory")
        identity = identity.model_copy(update={"agent_id": self.agent_id})
        request = UserRequest(
            user_id=identity.user_id,
            session_id=identity.session_id,
            text="",
            metadata=_trusted_memory_session_metadata(session_config),
        )
        state = AgentState.from_request(request, agent_id=identity.agent_id)
        state.session_memory_snapshot = (
            self.long_term_memory_service.initialize_session(
                identity=identity,
                state=state,
                trace_store=self.trace_store,
                reset=reset,
            )
        )
        return state

    def run_work_item(self, request):
        """Execute one bounded Workflow assignment through the existing assistant loop."""

        from assistant_agent.workflows.agent_runtime import (
            AgentWorkItemRequest,
            AgentWorkItemResult,
            parse_work_item_response,
            parse_workflow_plan_response,
            render_work_item_prompt,
        )

        assignment = (
            request
            if isinstance(request, AgentWorkItemRequest)
            else AgentWorkItemRequest.model_validate(request)
        )
        user_request = UserRequest(
            user_id=assignment.user_id,
            session_id=assignment.session_id,
            text=render_work_item_prompt(assignment),
            assistant_mode=assignment.assistant_mode,
            task_execution_mode="foreground",
            metadata={
                "_trusted_workflow_assignment": {
                    "workflow_id": assignment.workflow_id,
                    "work_item_id": assignment.work_item_id,
                    "attempt_id": assignment.attempt_id,
                },
                "_trusted_workflow_max_iterations": assignment.max_iterations,
                "_trusted_workflow_allowed_tools": list(assignment.allowed_tool_names),
                "tool_visibility": {
                    "allowed_tools": list(assignment.allowed_tool_names),
                    "profile": "workflow_work_item",
                },
            },
        )
        parsed_result: AgentWorkItemResult | None = None

        def parse_terminal_work_item_state(state: AgentState) -> AgentWorkItemResult:
            if state.status == "completed" and state.response is not None:
                status = "succeeded"
                summary = state.response.message
            elif state.status == "cancelled":
                status = "blocked"
                summary = "Work item execution was cancelled."
            else:
                status = "failed"
                summary = (
                    state.response.message
                    if state.response is not None
                    else "Work item execution failed."
                )
            model_calls_used = sum(
                1
                for step in state.request.metadata.get("assistant_loop_steps", [])
                if isinstance(step, dict) and step.get("output_type") is not None
            )
            artifact_refs = list(state.response.output_refs) if state.response else []
            loop_steps = [
                step
                for step in state.request.metadata.get("assistant_loop_steps", [])
                if isinstance(step, dict) and step.get("output_type") is not None
            ]
            terminal_notes = (
                loop_steps[-1].get("safety_notes", []) if loop_steps else []
            )
            terminal_failure_note = next(
                (
                    note
                    for note in terminal_notes
                    if note
                    in {
                        "provider_context_overflow",
                        "provider_context_overflow_retry_failed",
                        "provider_response_truncated",
                        "provider_error",
                        "provider_refusal",
                        "provider_timeout",
                        "provider_empty_response",
                        "empty_native_final_answer",
                    }
                ),
                None,
            )
            if status == "succeeded" and terminal_failure_note is not None:
                status = "failed"
            if status == "succeeded":
                if assignment.agent_role == "planner":
                    return parse_workflow_plan_response(
                        summary,
                        run_id=state.run_id,
                        trace_id=state.trace_id,
                        model_calls_used=model_calls_used,
                        tool_calls_used=len(state.tool_calls),
                    )
                required_verification_ids = [
                    item.constraint_id
                    for item in assignment.assigned_constraints
                    if item.verifier_work_item_id == assignment.work_item_id
                ]
                required_acceptance_ids = (
                    [
                        item.criterion_id
                        for item in assignment.acceptance_contract.criteria
                    ]
                    if isinstance(
                        assignment.acceptance_contract,
                        WorkflowStepAcceptanceContract,
                    )
                    else []
                )
                return parse_work_item_response(
                    summary,
                    run_id=state.run_id,
                    trace_id=state.trace_id,
                    artifact_refs=artifact_refs,
                    model_calls_used=model_calls_used,
                    tool_calls_used=len(state.tool_calls),
                    required_verification_ids=required_verification_ids,
                    required_acceptance_ids=required_acceptance_ids,
                )
            return AgentWorkItemResult(
                status=status,
                run_id=state.run_id,
                trace_id=state.trace_id,
                summary=summary,
                error_code=(
                    "provider_context_overflow"
                    if terminal_failure_note
                    in {
                        "provider_context_overflow",
                        "provider_context_overflow_retry_failed",
                    }
                    else terminal_failure_note
                ),
                artifact_refs=artifact_refs,
                model_calls_used=model_calls_used,
                tool_calls_used=len(state.tool_calls),
                agent_role=assignment.agent_role,
            )

        def admit_work_item_result_before_terminal(state: AgentState) -> None:
            nonlocal parsed_result
            parsed_result = parse_terminal_work_item_state(state)
            if (
                parsed_result.status == "succeeded"
                and assignment.agent_role == "planner"
                and parsed_result.plan_proposal is not None
                and self.workflow_service is not None
            ):
                workflow_bundle = self.workflow_service.store.load(
                    assignment.workflow_id
                )
                if workflow_bundle is not None:
                    try:
                        from assistant_agent.workflows.runtime import (
                            materialize_planner_revision,
                        )

                        materialize_planner_revision(
                            service=self.workflow_service,
                            bundle=workflow_bundle,
                            proposal=parsed_result.plan_proposal,
                            now=self.workflow_service.clock(),
                        )
                    except Exception:  # noqa: BLE001 - proposal is untrusted.
                        parsed_result = parsed_result.model_copy(
                            update={
                                "status": "failed",
                                "summary": (
                                    "Planner proposal failed Workflow admission."
                                ),
                                "error_code": "workflow_plan_rejected",
                                "plan_proposal": None,
                            }
                        )
            state.request.metadata["_trusted_workflow_work_item_outcome"] = {
                "status": parsed_result.status,
                "error_code": parsed_result.error_code,
            }
            if parsed_result.status == "succeeded" or state.status == "cancelled":
                return
            error_code = (
                parsed_result.error_code or f"workflow_work_item_{parsed_result.status}"
            )
            state.errors.append(
                AgentError(
                    message="Workflow work item result requires outer recovery.",
                    source="workflow_work_item_result",
                    details={"code": error_code},
                )
            )
            state.status = "failed"

        state = self.run_state(
            user_request,
            pre_terminal_state_hook=admit_work_item_result_before_terminal,
            export_trace_context=RuntimeExportTraceContext(
                export_trace_id=(
                    assignment.workflow_trace_id
                    or langfuse_trace_id(assignment.workflow_id)
                ),
                export_span_id=workflow_attempt_span_id(
                    assignment.workflow_id,
                    assignment.attempt_id,
                ),
                export_parent_span_id=workflow_root_span_id(
                    assignment.workflow_trace_id
                    or langfuse_trace_id(assignment.workflow_id)
                ),
                export_trace_name=f"{assignment.workflow_type}.workflow",
                export_observation_name=assignment.display_title,
                workflow_id=assignment.workflow_id,
                work_item_id=assignment.work_item_id,
                attempt_id=assignment.attempt_id,
                agent_role=assignment.agent_role,
            ),
        )
        return parsed_result or parse_terminal_work_item_state(state)

    def run_state(
        self,
        request: UserRequest,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
        trace_context: RuntimeTraceContext | None = None,
        export_trace_context: RuntimeExportTraceContext | None = None,
        pre_terminal_state_hook: Callable[[AgentState], None] | None = None,
        run_id: str | None = None,
    ) -> AgentState:
        """Run the graph and return the full state for compatibility callers.

        ``event_sink`` overrides the runtime-level sink for this run only. The
        runtime stays shareable across concurrent runs (e.g. one per WebSocket
        connection) without mutating ``self.event_sink``.
        """

        effective_run_id = run_id or new_run_id()
        identity = RequestIdentity.for_user(
            user_id=request.user_id,
            agent_id=self.agent_id,
            session_id=request.session_id,
        )
        try:
            return self._run_state(
                request,
                event_sink=event_sink,
                cancel_token=cancel_token,
                trace_context=trace_context,
                export_trace_context=export_trace_context,
                pre_terminal_state_hook=pre_terminal_state_hook,
                run_id=effective_run_id,
            )
        finally:
            self.long_term_memory_service.release_run_context(
                identity=identity,
                run_id=effective_run_id,
            )

    async def arun_state(
        self,
        request: UserRequest,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
        trace_context: RuntimeTraceContext | None = None,
        export_trace_context: RuntimeExportTraceContext | None = None,
        pre_terminal_state_hook: Callable[[AgentState], None] | None = None,
        run_id: str | None = None,
        interrupt_request: AssistantInterruptRequest | None = None,
    ) -> AgentState:
        """Run the graph through LangGraph's native asynchronous execution API."""

        effective_run_id = run_id or new_run_id()
        identity = RequestIdentity.for_user(
            user_id=request.user_id,
            agent_id=self.agent_id,
            session_id=request.session_id,
        )
        try:
            prepared = self._prepare_graph_run(
                request,
                event_sink=event_sink,
                cancel_token=cancel_token,
                trace_context=trace_context,
                export_trace_context=export_trace_context,
                pre_terminal_state_hook=pre_terminal_state_hook,
                run_id=effective_run_id,
                interrupt_request=interrupt_request,
            )
            state = await self._execute_graph_async(prepared)
            if state.status == "waiting_user":
                return state
            return self._finalize_graph_run(prepared, state)
        finally:
            self.long_term_memory_service.release_run_context(
                identity=identity,
                run_id=effective_run_id,
            )

    def _run_state(
        self,
        request: UserRequest,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
        trace_context: RuntimeTraceContext | None = None,
        export_trace_context: RuntimeExportTraceContext | None = None,
        pre_terminal_state_hook: Callable[[AgentState], None] | None = None,
        run_id: str | None = None,
    ) -> AgentState:
        """Execute one run while the Host retains its frozen Memory context."""

        prepared = self._prepare_graph_run(
            request,
            event_sink=event_sink,
            cancel_token=cancel_token,
            trace_context=trace_context,
            export_trace_context=export_trace_context,
            pre_terminal_state_hook=pre_terminal_state_hook,
            run_id=run_id,
        )
        state = self._execute_graph_sync(prepared)
        return self._finalize_graph_run(prepared, state)

    def _prepare_graph_run(
        self,
        request: UserRequest,
        *,
        event_sink: EventSink | None,
        cancel_token: Any | None,
        trace_context: RuntimeTraceContext | None,
        export_trace_context: RuntimeExportTraceContext | None,
        pre_terminal_state_hook: Callable[[AgentState], None] | None,
        run_id: str,
        interrupt_request: AssistantInterruptRequest | None = None,
    ) -> _PreparedGraphRun:
        """Prepare the shared state and runtime-only dependencies for one run."""

        request = normalize_task_execution_mode(
            request,
            durable_tasks_enabled=self.config.durable_tasks_enabled,
        )
        workflow_work_item = _is_workflow_work_item_request(request)
        deep_research_start = (
            request.assistant_mode == "deep_research" and not workflow_work_item
        )
        if not workflow_work_item and not deep_research_start:
            self._refresh_visual_memory_capability(request)
            self._refresh_visual_reminder_capability(request)
            self._attach_proactive_session_context(request)
        base_event_sink = event_sink or self.event_sink
        run_event_sink = (
            _ResponseDeltaTrackingEventSink(base_event_sink)
            if base_event_sink is not None
            else None
        )
        graph_event_sink: EventSink | None = run_event_sink
        if run_event_sink is not None and interrupt_request is not None:
            graph_event_sink = _InterruptResponseDeltaBarrier(run_event_sink)
        # A per-run ToolExecutor binds the run's sink so tool events and agent
        # trace events emitted via graph_state["tool_executor"] reach it.
        tool_executor = ToolExecutor(
            registry=self.registry,
            event_sink=graph_event_sink,
            context_metadata={
                "durable_task_service": self.durable_task_service,
                "workflow_service": self.workflow_service,
            },
            cancel_token=cancel_token,
            execution_backend=self.tool_execution_backend,
            operation_store=self.tool_operation_store,
        )
        state = AgentState.from_request(
            request,
            run_id=run_id,
            trace_id=trace_context.trace_id if trace_context is not None else None,
            agent_id=self.agent_id,
        )
        try:
            self.capability_grant_controller.prepare_run(
                state,
                self.registry.list_specs(),
            )
        except Exception as exc:
            state.errors.append(
                AgentError(
                    message="Capability grant restoration failed.",
                    source="capability_grant_controller",
                    details={
                        "code": "capability_grant_restore_failed",
                        "error_type": type(exc).__name__,
                    },
                )
            )
        if not workflow_work_item and not deep_research_start:
            self._embed_stable_request_text(state, request)
        state.context_source_result = self.context_source_coordinator.load_once(
            ContextSourceRequest(
                user_id=state.user_id,
                source_root=Path(self.config.editable_context_root),
                local_owner_user_id=self.config.editable_context_user_id,
                provider_mode=self.config.provider_mode,
                editable_context_enabled=self.config.editable_context_enabled,
                section_char_budgets={
                    SOUL_SOURCE_ID: SOUL_COMPILED_MAX_CHARS,
                    USER_PROFILE_SOURCE_ID: USER_PROFILE_COMPILED_MAX_CHARS,
                },
                enabled_source_ids=(
                    {USER_PROFILE_SOURCE_ID, SOUL_SOURCE_ID}
                    if self.config.editable_context_user_id
                    else {USER_PROFILE_SOURCE_ID}
                ),
            )
        )
        run_started_at = perf_counter()
        runtime_event_publisher = RuntimeEventPublisher(
            event_sink=run_event_sink,
            trace_store=self.trace_store,
        )
        run_started_fact = RunStartedFact(
            state=state,
            parent_span_id=(
                trace_context.parent_span_id if trace_context is not None else None
            ),
            execution_engine=(
                "durable_plan_execute_start"
                if deep_research_start
                else "langgraph_assistant_loop"
            ),
            export_trace_context=export_trace_context,
            experiment_trace_link=(
                trace_context.experiment_link if trace_context is not None else None
            ),
        )
        runtime_event_publisher.deliver_run_started(run_started_fact)
        if self.run_history is not None:
            self.run_history.record_start(state.run_id, state.user_id, state.session_id)
        conversation_prepare_latency_ms = request.metadata.get(
            "conversation_prepare_latency_ms"
        )
        if (
            isinstance(conversation_prepare_latency_ms, int)
            and not isinstance(conversation_prepare_latency_ms, bool)
            and conversation_prepare_latency_ms >= 0
        ):
            self._append_observability_event(
                state,
                canonical_event="conversation.prepare.finished",
                status="succeeded",
                latency_ms=conversation_prepare_latency_ms,
                attributes={
                    "conversation_turn_index": request.metadata.get(
                        "conversation_turn_index"
                    ),
                },
            )
        runtime_event_publisher.record_run_started(run_started_fact)
        if not workflow_work_item and not deep_research_start:
            self._prepare_run_memory_context(
                state,
                cancel_token=cancel_token,
            )
        runtime_context = GraphRuntimeContext(
            tool_executor=tool_executor,
            chat_adapter=self.chat_adapter,
            chat_turn=self._run_native_chat_turn,
            context_service=self.context_service,
            context_projector=self._refresh_realtime_video_context,
            tool_result_handler=self.capability_grant_controller.handle_tool_result,
            trace_store=self.trace_store,
            event_sink=graph_event_sink,
            cancel_token=cancel_token,
            agent_state=state,
        )
        legacy_initial_state = {
            "request": request,
            "state": state,
            "outputs_by_step": {},
            "current_step_index": 0,
            "run_phase": RunPhase.ACT,
            "trace_id": state.trace_id,
            "max_tool_iterations": _workflow_iteration_limit(
                request,
                configured=self.config.max_tool_iterations,
            ),
            "max_control_tool_iterations": self.config.max_control_tool_iterations,
            "tool_calls_used": 0,
            "action_tool_calls_used": 0,
            "control_tool_calls_used": 0,
            "max_plan_steps": self.config.max_plan_steps,
            "assistant_output": None,
            "pending_tool_calls": [],
            "assistant_iterations": 0,
            "tool_observations": [],
            "last_llm_span_id": "",
            "last_llm_attempt_kind": "",
            "response_stream_current_call_emitted": False,
            "response_stream_ends_with_newline": False,
            "response_stream_separator_pending": False,
        }
        initial_state = assistant_turn_state_from_loop_state(legacy_initial_state)
        if interrupt_request is not None:
            initial_state["pending_interrupt"] = interrupt_request.model_dump(
                mode="json"
            )
            initial_state = validate_assistant_turn_state(initial_state)
        identity = GraphExecutionIdentity.for_assistant_turn(
            agent_id=self.agent_id,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=state.run_id,
        )
        return _PreparedGraphRun(
            request=request,
            state=state,
            workflow_work_item=workflow_work_item,
            deep_research_start=deep_research_start,
            run_event_sink=run_event_sink,
            runtime_event_publisher=runtime_event_publisher,
            run_started_at=run_started_at,
            runtime_context=runtime_context,
            initial_state=initial_state,
            identity=identity,
            cancel_token=cancel_token,
            pre_terminal_state_hook=pre_terminal_state_hook,
        )

    def _begin_graph_execution(self, prepared: _PreparedGraphRun) -> bool:
        """Apply shared pre-graph cancellation and compatibility branches."""

        state = prepared.state
        try:
            raise_if_cancelled(
                prepared.cancel_token,
                phase="pre_graph",
                state=state,
            )
        except AgentRunCancelled as exc:
            state.cancel(exc.message, source=exc.source, details=exc.details)
            return False
        if prepared.deep_research_start:
            _start_deep_research_workflow(
                state,
                workflow_service=self.workflow_service,
            )
            return False
        if (
            prepared.request.task_execution_mode == "durable"
            and not self.config.durable_tasks_enabled
        ):
            _set_durable_tasks_disabled_response(state)
            return False
        self._emit_graph_execution_event(prepared, event_type="graph_node_started")
        return True

    def _complete_graph_execution(
        self,
        prepared: _PreparedGraphRun,
        final_state: Mapping[str, Any],
    ) -> AgentState:
        persisted = validate_assistant_turn_state(final_state)
        state = apply_assistant_turn_state_to_agent_state(
            persisted,
            runtime_state=prepared.state,
        )
        try:
            raise_if_cancelled(
                prepared.cancel_token,
                phase="post_graph",
                state=state,
            )
        except AgentRunCancelled as exc:
            if isinstance(exc.state, AgentState):
                state = exc.state
            state.cancel(exc.message, source=exc.source, details=exc.details)
        return state

    def _emit_graph_execution_event(
        self,
        prepared: _PreparedGraphRun,
        *,
        event_type: Literal["graph_node_started", "graph_node_finished"],
    ) -> None:
        self._emit(
            AgentEvent(
                type=event_type,
                session_id=prepared.state.session_id,
                run_id=prepared.state.run_id,
                node_name="agent_graph",
            ),
            prepared.run_event_sink,
        )

    def _execute_graph_sync(self, prepared: _PreparedGraphRun) -> AgentState:
        if not self._begin_graph_execution(prepared):
            return prepared.state
        try:
            final_state = self.assistant_graph_app.invoke(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
            return self._complete_graph_execution(prepared, final_state)
        except AgentRunCancelled as exc:
            state = exc.state if isinstance(exc.state, AgentState) else prepared.state
            state.cancel(exc.message, source=exc.source, details=exc.details)
            return state
        finally:
            self._emit_graph_execution_event(
                prepared,
                event_type="graph_node_finished",
            )

    async def _execute_graph_async(self, prepared: _PreparedGraphRun) -> AgentState:
        if not self._begin_graph_execution(prepared):
            return prepared.state
        try:
            result = await self.assistant_graph_app.arun(
                prepared.initial_state,
                identity=prepared.identity,
                context=prepared.runtime_context,
            )
            if result.status == "interrupted":
                state = self._complete_graph_execution(prepared, result.final_state)
                state.status = "waiting_user"
                state.response = None
                return state
            return self._complete_graph_execution(prepared, result.final_state)
        except AgentRunCancelled as exc:
            state = exc.state if isinstance(exc.state, AgentState) else prepared.state
            state.cancel(exc.message, source=exc.source, details=exc.details)
            return state
        finally:
            self._emit_graph_execution_event(
                prepared,
                event_type="graph_node_finished",
            )

    def _finalize_graph_run(
        self,
        prepared: _PreparedGraphRun,
        state: AgentState,
    ) -> AgentState:
        """Commit the shared terminal lifecycle after sync or async execution."""

        request = prepared.request
        run_event_sink = prepared.run_event_sink
        run_started_at = prepared.run_started_at
        runtime_event_publisher = prepared.runtime_event_publisher
        if prepared.pre_terminal_state_hook is not None:
            try:
                prepared.pre_terminal_state_hook(state)
            except Exception as exc:  # noqa: BLE001 - trusted hook fails closed.
                state.errors.append(
                    AgentError(
                        message="Runtime terminal state validation failed.",
                        source="pre_terminal_state_hook",
                        details={
                            "code": "terminal_state_validation_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                )
                state.status = "failed"
        if state.response is not None:
            state.response = with_generated_artifact_delivery(
                state.response,
                base_url=self.config.artifact_base_url,
            )
        if self.run_history is not None:
            postprocess_started_at = perf_counter()
            terminal_status = _terminal_history_status(state.status)
            self.run_history.record_end(
                state.run_id,
                state.user_id,
                state.session_id,
                terminal_status,
                None,
                list(dict.fromkeys(call.tool_name for call in state.tool_calls)),
                int((perf_counter() - run_started_at) * 1000),
                error=state.errors[-1].message if state.errors else None,
            )
        else:
            postprocess_started_at = perf_counter()
            terminal_status = _terminal_history_status(state.status)
        tool_lifecycle_cleanup_issues = self.registry.notify_run_terminal(
            state.run_id,
            terminal_status,
        )
        self.session_store.touch_run(
            user_id=state.user_id,
            session_id=state.session_id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            message_preview=request.text or "",
            status=terminal_status,
        )
        self._append_observability_event(
            state,
            canonical_event="runtime.postprocess.finished",
            status="succeeded",
            latency_ms=int((perf_counter() - postprocess_started_at) * 1000),
            attributes={
                "terminal_status": terminal_status,
                "run_history_present": self.run_history is not None,
                "session_store_updated": True,
                "tool_lifecycle_cleanup_issue_count": len(
                    tool_lifecycle_cleanup_issues
                ),
            },
        )
        terminal_fact = RunTerminalFact(
            state=state,
            terminal_status=terminal_status,
            latency_ms=int((perf_counter() - run_started_at) * 1000),
        )
        runtime_event_publisher.record_run_terminal(terminal_fact)
        _record_local_trace_conversation(state)
        _append_trace_content_event(self.trace_store, state)
        append_runtime_turn_summary(self.trace_store, state=state)
        if terminal_status == "completed":
            response_text = state.response.message if state.response else ""
            if (
                response_text
                and run_event_sink is not None
                and not run_event_sink.response_delta_emitted
            ):
                self._emit(
                    AgentEvent(
                        type="response_delta",
                        session_id=state.session_id,
                        run_id=state.run_id,
                        text=response_text,
                        payload={
                            "source": "runtime_final_response",
                            "token_streaming": False,
                            "chunking_strategy": "final_text_fallback",
                        },
                    ),
                    run_event_sink,
                )
        runtime_event_publisher.deliver_run_terminal(terminal_fact)
        if terminal_status == "completed" and not prepared.workflow_work_item:
            _record_local_delivered_response(state)
            self.long_term_memory_service.enqueue_completed_turn(
                trace_store=self.trace_store,
                state=state,
            )
        return state

    def _embed_stable_request_text(
        self,
        state: AgentState,
        request: UserRequest,
    ) -> None:
        observation = _stable_text_observation(
            state.session_id,
            state.run_id,
            request.text,
        )
        if observation is None:
            return
        coordinator = self.embedding_coordinator_store.resolve(
            state.user_id,
            state.session_id,
        )
        if coordinator.has_consumer_for("text"):
            coordinator.embed_text(observation)

    def _refresh_visual_memory_capability(self, request: UserRequest) -> None:
        """Overwrite caller metadata with runtime-owned visual-history facts."""

        request.metadata.pop("_trusted_visual_memory_available", None)
        request.metadata.pop("_trusted_visual_memory_as_of_sequence", None)
        request.metadata.pop("_trusted_visual_memory_as_of_ms", None)
        semantic_store = self.visual_semantic_store_pool.peek(
            request.user_id,
            request.session_id,
        )
        if semantic_store is not None and semantic_store.has_visual_history():
            request.metadata["_trusted_visual_memory_available"] = True
        if is_trusted_agent_service_request(request):
            target_sequence = request.metadata.get("realtime_video_target_sequence")
            if (
                isinstance(target_sequence, int)
                and not isinstance(target_sequence, bool)
                and target_sequence >= 0
            ):
                request.metadata["_trusted_visual_memory_as_of_sequence"] = (
                    target_sequence
                )

    def _refresh_visual_reminder_capability(self, request: UserRequest) -> None:
        """Overwrite caller metadata with a live trusted VIDEO connection fact."""

        request.metadata.pop("_trusted_visual_reminder_available", None)
        agent_service = request.metadata.get("agent_service")
        call_type = (
            agent_service.get("call_type") if isinstance(agent_service, dict) else None
        )
        if not is_trusted_agent_service_request(request) or call_type != "VIDEO":
            return
        manager = self.visual_reminder_registry.peek(
            request.user_id,
            request.session_id,
        )
        if manager is not None:
            request.metadata["_trusted_visual_reminder_available"] = True

    def _attach_proactive_session_context(self, request: UserRequest) -> None:
        """Overwrite caller data with bounded Runtime-owned delivery evidence."""

        request.metadata.pop("_trusted_proactive_session_events", None)
        events = self.visual_reminder_registry.recent_session_events(
            request.user_id,
            request.session_id,
        )
        if events:
            request.metadata["_trusted_proactive_session_events"] = [
                event.model_dump(mode="json") for event in events
            ]

    def drain_memory_ingestions(self, *, timeout: float | None = None) -> bool:
        """Wait for accepted memory ingestions."""

        return self.long_term_memory_service.drain(timeout=timeout)

    def _attach_session_memory_snapshot(self, state: AgentState) -> None:
        """Attach the frozen snapshot without performing recall."""

        self.long_term_memory_service.attach_session_snapshot(state)

    def _prepare_run_memory_context(
        self,
        state: AgentState,
        *,
        cancel_token: Any | None,
    ) -> None:
        """Prepare and freeze the active Plugin contribution once per run."""

        self.long_term_memory_service.prepare_context(
            state=state,
            trace_store=self.trace_store,
            cancel_token=cancel_token,
        )

    def close(self) -> bool:
        """Drain and close runtime-owned background lifecycle services."""

        self.embedding_coordinator_store.close()
        self.visual_semantic_store_pool.close()
        self.visual_memory_text_index.close()
        return self.long_term_memory_service.close(
            timeout=self.config.memory_ingestion_shutdown_timeout_seconds,
        )

    def run_task_quantum(
        self,
        request: UserRequest,
        *,
        binding: TrustedTaskBinding,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
    ) -> "TaskQuantumResult":
        """Run at most one governed durable-task action and yield a checkpoint."""

        from assistant_agent.automation.durable_tasks.worker import TaskQuantumResult

        request = request.model_copy(
            update={"task_execution_mode": "durable"}, deep=True
        )
        request.metadata["durable_task_binding"] = binding.model_dump(mode="json")
        snapshot = DurableTaskSnapshot.model_validate(
            request.metadata.get("durable_task_snapshot")
        )
        state = AgentState.from_request(request, agent_id=self.agent_id)
        self._attach_session_memory_snapshot(state)
        state.request.metadata["durable_task_quantum"] = True
        tool_executor = ToolExecutor(
            registry=self.registry,
            event_sink=event_sink,
            context_metadata={
                "durable_task_service": self.durable_task_service,
                "durable_task_binding": binding,
            },
            cancel_token=cancel_token,
            execution_backend=self.tool_execution_backend,
            operation_store=self.tool_operation_store,
        )
        try:
            raise_if_cancelled(cancel_token, phase="durable_quantum_start", state=state)
            chat_request = self._durable_quantum_chat_request(request, state=state)
            if chat_request is None:
                return TaskQuantumResult(
                    TaskCheckpoint(kind="failed", error_code="tool_schema_unavailable"),
                    state,
                )
            result = self._run_native_chat_turn(chat_request)
            if not result.success:
                message = (
                    result.errors[0].message
                    if result.errors
                    else "Provider call failed."
                )
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind="failed",
                        error_code="durable_provider_failed",
                        error_message=message,
                    ),
                    state,
                )
            if not result.tool_calls:
                if not binding.ready_step_ids:
                    state.set_response(
                        AgentResponse(message=result.response_text or "任务完成。")
                    )
                    return TaskQuantumResult(
                        TaskCheckpoint(
                            kind="completed",
                            summary=result.response_text or "Task completed.",
                        ),
                        state,
                    )
                step_id = binding.ready_step_ids[0]
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind="tool_failed",
                        step_id=step_id,
                        error_code="durable_step_required",
                        error_message="Required durable steps remain incomplete.",
                    ),
                    state,
                )
            if len(result.tool_calls) != 1:
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind="failed",
                        error_code="durable_quantum_tool_limit",
                        error_message="A durable quantum accepts exactly one tool call.",
                    ),
                    state,
                )
            call = result.tool_calls[0]
            decision = native_tool_call_to_assistant_decision(call)
            decision.step_id = _durable_step_id_for_call(snapshot, binding, call.name)
            validation = ActionValidator().validate(
                decision=decision,
                registry=self.registry,
                request=request,
                state=state,
            )
            if not validation.accepted:
                checkpoint_kind = (
                    "waiting_input"
                    if validation.code
                    in {"invalid_tool_input", "missing_required_input"}
                    else "tool_failed"
                    if decision.step_id
                    else "failed"
                )
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind=checkpoint_kind,
                        step_id=decision.step_id,
                        error_code=validation.code,
                        error_message=validation.message,
                    ),
                    state,
                )
            active_binding = binding
            if self.durable_task_service is None or decision.step_id is None:
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind="failed",
                        error_code="durable_task_service_unavailable",
                    ),
                    state,
                )
            active_binding_holder = {"value": binding}

            def begin_external_attempt() -> None:
                started_binding = self.durable_task_service.begin_attempt(
                    binding=binding,
                    step_id=decision.step_id or "",
                    tool_name=decision.tool_name or "",
                    tool_input_digest=_durable_tool_input_digest(
                        decision.tool_input or {}
                    ),
                )
                active_binding_holder["value"] = started_binding
                request.metadata["durable_task_binding"] = started_binding.model_dump(
                    mode="json"
                )
                tool_executor.context_metadata["durable_task_binding"] = started_binding

            tool_executor.context_metadata["_before_tool_execution"] = (
                begin_external_attempt
            )
            tool_result = tool_executor.run_tool(
                state,
                decision.step_id,
                decision.tool_name or "",
                decision.tool_input or {},
                trace_store=self.trace_store,
                trace_id=state.trace_id,
                node_name="durable_task_quantum",
            )
            active_binding = active_binding_holder["value"]
            if (tool_result.data or {}).get("side_effect_state") == "unknown":
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind="outcome_unknown",
                        step_id=decision.step_id,
                        summary=_durable_tool_result_summary(tool_result),
                        error_code="mutating_outcome_unknown",
                        error_message=tool_result.error,
                    ),
                    state,
                    active_binding,
                )
            if tool_result.success:
                return TaskQuantumResult(
                    TaskCheckpoint(
                        kind="tool_succeeded",
                        step_id=decision.step_id,
                        output_ref=tool_result.output_ref,
                        summary=_durable_tool_result_summary(tool_result),
                    ),
                    state,
                    active_binding,
                )
            return TaskQuantumResult(
                TaskCheckpoint(
                    kind="tool_failed",
                    step_id=decision.step_id,
                    error_code="durable_tool_failed",
                    error_message=tool_result.error or "Tool execution failed.",
                ),
                state,
                active_binding,
            )
        except AgentRunCancelled as exc:
            state.cancel(exc.message, source=exc.source, details=exc.details)
            return TaskQuantumResult(
                TaskCheckpoint(kind="cancelled", summary=exc.message),
                state,
            )

    def _run_native_chat_turn(self, chat_request: ChatRequest) -> ChatResult:
        if self.config.native_provider_streaming and supports_async_streaming_chat(
            self.chat_adapter
        ):
            return ProviderStreamingTurnRunner().run_turn(
                self.chat_adapter, chat_request
            )
        return self.chat_adapter.chat(chat_request)

    def _durable_quantum_chat_request(
        self,
        request: UserRequest,
        *,
        state: AgentState,
    ) -> ChatRequest | None:
        """Build the single provider turn used by one durable-task quantum."""

        self._refresh_realtime_video_context(request)
        tool_specs = _durable_quantum_tool_specs(self.registry, state)
        if tool_specs is None:
            return None
        context_pack = build_traced_assistant_context_pack(
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="durable_task_quantum",
            state=state,
            request=request,
            observations=[],
            tool_specs=tool_specs,
            iteration=0,
            max_iterations=1,
            context_compactor=None,
            supports_developer_role=self.context_service.supports_developer_role,
        )
        compilation = PromptCompiler().compile(
            PromptCompileRequest(
                user_id=state.user_id,
                session_id=state.session_id,
                mode=PromptCompileMode.NATIVE_TOOL,
                user_query_fallback="durable task quantum",
                context_pack=context_pack,
                observations=(),
                native_calls=(),
                tool_call_id_prefix="durable_task_call_",
                current_location=self.config.current_location,
                supports_developer_role=self.context_service.supports_developer_role,
            )
        )
        return compilation.chat_request

    def _refresh_realtime_video_context(self, request: UserRequest) -> None:
        """Refresh the passive rolling snapshot immediately before context build."""

        if not is_trusted_agent_service_request(request) or not request.video_ids:
            request.metadata.pop("realtime_video_context", None)
            request.metadata.pop("realtime_video_context_trusted", None)
            return
        video_id = request.video_ids[-1]
        snapshot = self.realtime_video_memory_store.snapshot(video_id)
        target_sequence = request.metadata.get("realtime_video_target_sequence")
        if (
            isinstance(target_sequence, bool)
            or not isinstance(target_sequence, int)
            or target_sequence < 0
        ):
            target_sequence = None
        context = project_realtime_video_context(
            snapshot,
            now_ms=int(time() * 1000),
            target_sequence=target_sequence,
        )
        request.metadata["realtime_video_context"] = context.model_dump(mode="json")
        request.metadata["realtime_video_context_trusted"] = True

    def run_stream(
        self,
        request: UserRequest,
        *,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
    ) -> AgentRunStream[AgentState]:
        """Run the graph in a worker thread and expose AgentEvent records asynchronously."""

        loop = asyncio.get_running_loop()
        stream: AgentRunStream[AgentState] = AgentRunStream(loop=loop)
        inner = event_sink if event_sink is not None else self.event_sink
        stream_sink = AsyncQueueEventSink(loop=loop, stream=stream, inner=inner)

        async def _run() -> None:
            try:
                state = await asyncio.to_thread(
                    self.run_state,
                    request,
                    event_sink=stream_sink,
                    cancel_token=cancel_token,
                )
            except BaseException as exc:
                stream.set_exception(exc)
            else:
                stream.set_result(state)

        asyncio.create_task(_run())
        return stream

    def run(
        self,
        request: UserRequest,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
    ) -> AgentResponse:
        """Run the graph and return the final AgentResponse."""

        state = self.run_state(
            request, event_sink=event_sink, cancel_token=cancel_token
        )
        if state.response is not None:
            return state.response
        if state.status == "cancelled":
            return AgentResponse(
                message="请求已取消。",
                data={
                    "status": state.status,
                    "errors": [error.model_dump(mode="json") for error in state.errors],
                },
            )
        return AgentResponse(
            message="请求处理失败。",
            data={
                "status": state.status,
                "errors": [error.model_dump(mode="json") for error in state.errors],
            },
        )

    def _emit(self, event: AgentEvent, event_sink: EventSink | None = None) -> None:
        sink = event_sink or self.event_sink
        if sink is not None:
            sink.emit(event)

    def _append_observability_event(
        self,
        state: AgentState,
        *,
        canonical_event: str,
        node_name: str = "runtime",
        status: str | None = None,
        tool_name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: int | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        append_observability_event(
            self.trace_store,
            trace_id=state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event=canonical_event,
            node_name=node_name,
            status=status,
            tool_name=tool_name,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            parent_span_id=parent_span_id,
            attributes=attributes,
            output_summary=output_summary,
            error=error,
        )


def _terminal_history_status(
    status: str,
) -> Literal["completed", "failed", "cancelled"]:
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "completed":
        return "completed"
    raise ValueError("run status is not terminal")


class _ResponseDeltaTrackingEventSink:
    """Forward events while tracking whether user-visible response chunks exist."""

    def __init__(self, inner: EventSink) -> None:
        self.inner = inner
        self.response_delta_emitted = False

    def emit(self, event: AgentEvent) -> None:
        if event.type == "response_delta":
            self.response_delta_emitted = True
        self.inner.emit(event)


class _InterruptResponseDeltaBarrier:
    """Keep provisional text private until a trusted interrupt is resolved."""

    def __init__(self, inner: _ResponseDeltaTrackingEventSink) -> None:
        self.inner = inner
        self.deferred_response_deltas: list[AgentEvent] = []

    @property
    def response_delta_emitted(self) -> bool:
        return self.inner.response_delta_emitted

    def emit(self, event: AgentEvent) -> None:
        if event.type == "response_delta":
            self.deferred_response_deltas.append(event)
            return
        self.inner.emit(event)


def _metadata_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _record_local_trace_conversation(state: AgentState) -> None:
    from assistant_agent.observability.trace_content_policy import (
        local_trace_content_enabled,
    )

    if not local_trace_content_enabled():
        return
    user_text = (state.request.text or "").strip()
    assistant_text = (
        state.response.message if state.response is not None else ""
    ).strip()
    if not assistant_text and state.errors:
        assistant_text = state.errors[-1].message.strip()
    if not user_text or not assistant_text:
        return
    from assistant_agent.observability.trace_conversation import (
        get_default_trace_conversation_store,
    )

    get_default_trace_conversation_store().append(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        user_text=user_text,
        assistant_text=assistant_text,
    )


def _record_local_delivered_response(state: AgentState) -> None:
    from assistant_agent.observability.trace_content_policy import (
        local_trace_content_enabled,
    )

    if not local_trace_content_enabled() or state.response is None:
        return
    delivered_text = state.response.message.strip()
    if not delivered_text:
        return
    from assistant_agent.observability.trace_conversation import (
        get_default_trace_conversation_store,
    )

    get_default_trace_conversation_store().append_delivered(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        delivered_text=delivered_text,
    )


def _append_trace_content_event(
    trace_store: TraceStore | None, state: AgentState
) -> None:
    """Persist complete run evidence for later trace-driven evaluation."""

    from assistant_agent.observability.trace_content_policy import (
        local_trace_content_enabled,
    )

    if trace_store is None or not local_trace_content_enabled():
        return
    from assistant_agent.observability.trace_conversation import (
        get_default_trace_conversation_store,
    )

    conversation = get_default_trace_conversation_store().get(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        limit=1_000_000,
        include_llm_inputs=True,
        include_llm_outputs=True,
        include_tool_observations=True,
    )
    conversation_payload = (
        conversation.model_dump(mode="json") if conversation is not None else {}
    )
    append_observability_event(
        trace_store,
        trace_id=state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="trace.content",
        observation_type="event",
        observation_name="trace.content",
        node_name="runtime",
        status=state.status,
        input_summary={
            "request": state.request.model_dump(mode="json"),
            "llm_inputs": conversation_payload.get("llm_inputs", []),
        },
        output_summary={
            "response": (
                state.response.model_dump(mode="json")
                if state.response is not None
                else None
            ),
            "conversation": {
                key: value
                for key, value in conversation_payload.items()
                if key
                in {
                    "user",
                    "assistant",
                    "delivered",
                    "llm_outputs",
                    "tool_observations",
                }
            },
        },
        attributes={"content_capture": "full"},
    )


def _durable_quantum_tool_specs(
    registry: Any, state: AgentState
) -> list[ToolSpec] | None:
    try:
        if hasattr(registry, "list_specs"):
            specs = registry.list_specs()
        else:
            specs = registry.describe_tools()
        return [
            spec if isinstance(spec, ToolSpec) else ToolSpec.model_validate(spec)
            for spec in specs
        ]
    except Exception as exc:
        _set_durable_tool_description_failure_response(state, exc)
        return None


def _workflow_iteration_limit(request: UserRequest, *, configured: int) -> int:
    """Honor only runtime-owned Workflow budgets and never expand the global limit."""

    if not isinstance(request.metadata.get("_trusted_workflow_assignment"), dict):
        return configured
    requested = request.metadata.get("_trusted_workflow_max_iterations")
    if not isinstance(requested, int):
        return configured
    return max(1, min(requested, configured))


def _is_workflow_work_item_request(request: UserRequest) -> bool:
    return isinstance(request.metadata.get("_trusted_workflow_assignment"), dict)


def _set_durable_tool_description_failure_response(
    state: AgentState, exc: Exception
) -> None:
    message = f"工具描述读取失败：{exc}"
    state.request.metadata["tool_description_error"] = {
        "code": "tool_description_unavailable",
        "message": str(exc),
    }
    error = AgentError(
        message=message,
        source="durable_task_quantum",
        details={
            "code": "tool_description_unavailable",
            "recovery_action": "stop_with_error",
        },
    )
    state.errors.append(error)
    state.response = AgentResponse(
        message="工具描述不可用，无法安全执行 agent runtime。",
        data={
            "durable_task_quantum": True,
            "errors": [{"code": error.details["code"], "message": message}],
        },
    )
    state.status = "failed"


def _durable_step_id_for_call(
    snapshot: DurableTaskSnapshot,
    binding: TrustedTaskBinding,
    tool_name: str,
) -> str | None:
    matching = [
        step.step_id
        for step in snapshot.plan.steps
        if step.step_id in binding.ready_step_ids and step.tool_name == tool_name
    ]
    if len(matching) == 1:
        return matching[0]
    return binding.ready_step_ids[0] if len(binding.ready_step_ids) == 1 else None


def _durable_tool_input_digest(tool_input: dict[str, Any]) -> str:
    encoded = json.dumps(
        tool_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _durable_tool_result_summary(result: ToolResult) -> str:
    data = result.data or {}
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:1000]
    if result.voice_summary:
        return result.voice_summary[:1000]
    return f"{result.tool_name} completed successfully."


def _set_durable_tasks_disabled_response(state: AgentState) -> None:
    code = "durable_tasks_disabled"
    message = "Durable task execution is disabled for this runtime."
    state.errors.append(
        AgentError(message=message, source="runtime", details={"code": code})
    )
    state.response = AgentResponse(
        message="当前运行时未启用持久化任务执行。",
        data={"errors": [{"code": code, "message": message}]},
    )
    state.status = "failed"


def _start_deep_research_workflow(
    state: AgentState,
    *,
    workflow_service: WorkflowService | None,
) -> None:
    """Start one durable Plan-and-Execute execution without an ingress ReAct."""

    objective = (state.request.text or "").strip()
    if workflow_service is None or not objective:
        code = (
            "durable_workflows_disabled"
            if workflow_service is None
            else "deep_research_objective_required"
        )
        state.errors.append(
            AgentError(
                message="Deep Research could not start.",
                source="deep_research_start",
                details={"code": code},
            )
        )
        state.response = AgentResponse(
            message="当前无法启动深度研究工作流。",
            data={"errors": [{"code": code}]},
        )
        state.status = "failed"
        return
    try:
        bundle = workflow_service.submit(
            identity=RequestIdentity.for_user(
                user_id=state.user_id,
                agent_id=state.agent_id,
                session_id=state.session_id,
            ),
            ingress_run_id=state.run_id,
            ingress_trace_id=state.trace_id,
            ingress_parent_span_id=None,
            submission=WorkflowSubmission(
                workflow_type="deep_research",
                objective=objective,
                deliverables=["research_report"],
                constraints=["最终报告必须引用至少 15 个可信且多样的来源。"],
                inputs={
                    "research_questions": [objective],
                    "source_target": 15,
                },
                durability_reasons=["assistant_mode_deep_research"],
                idempotency_key=f"deep_research:{state.run_id}",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - boundary returns a safe failure.
        state.errors.append(
            AgentError(
                message="Deep Research workflow submission failed.",
                source="deep_research_start",
                details={
                    "code": "workflow_submission_failed",
                    "error_type": type(exc).__name__,
                },
            )
        )
        state.response = AgentResponse(
            message="深度研究工作流启动失败。",
            data={"errors": [{"code": "workflow_submission_failed"}]},
        )
        state.status = "failed"
        return
    workflow = bundle.workflow
    state.set_response(
        AgentResponse(
            message="",
            data={
                "workflow": {
                    "workflow_id": workflow.workflow_id,
                    "workflow_type": workflow.workflow_type,
                    "status": workflow.status,
                    "phase": workflow.phase,
                }
            },
            output_refs=[f"workflow://{workflow.workflow_id}"],
        )
    )
