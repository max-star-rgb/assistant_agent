"""Default LangGraph runtime for agent execution."""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.cancellation import AgentRunCancelled, raise_if_cancelled
from assistant_agent.agent.conditional_graph import build_conditional_agent_graph
from assistant_agent.agent.assistant_loop_graph import build_assistant_loop_graph
from assistant_agent.agent.graph_runtime import GraphRuntimeContext
from assistant_agent.agent.intent import IntentDetector
from assistant_agent.agent.llm_event_mapping import stream_delta_to_agent_event
from assistant_agent.agent.router import ToolRouter
from assistant_agent.agent.state import AgentError, AgentState
from assistant_agent.agent.event_stream import AgentRunStream, AsyncQueueEventSink
from assistant_agent.agent.system_prompt_policy import (
    SystemPromptOptions,
    SystemPromptProfile,
)
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.agent.memory_tool_selection import (
    build_memory_tool_selection_audit,
    record_memory_tool_selection_audit,
)
from assistant_agent.agent.provider_streaming import ProviderStreamingTurnRunner, supports_async_streaming_chat
from assistant_agent.agent.tool_scheduler import (
    ToolExecutionGroup,
    build_scheduled_tool_call,
    plan_tool_schedule,
)
from assistant_agent.memory.factory import create_memory_store
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision, native_tool_call_to_assistant_decision
from assistant_agent.schemas.api import api_error_from_agent_error
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result, rejected_observation
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.event_sink import EventSink
from assistant_agent.services.chat_adapter import ChatAdapter, ChatRequest, ChatResult, create_chat_adapter
from assistant_agent.services.checkpointer import create_checkpointer
from assistant_agent.services.context.observability import build_traced_assistant_context_pack
from assistant_agent.services.context.compactor import ContextCompactor, create_context_compactor
from assistant_agent.services.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompileResult,
    PromptCompiler,
)
from assistant_agent.services.context.report import build_context_report
from assistant_agent.services.memory_observability import load_memory_with_trace, save_memory_with_trace
from assistant_agent.services.memory_core_status import build_memory_core_status, update_memory_core_status_errors
from assistant_agent.services.response_observability import append_response_final_event
from assistant_agent.services.run_history import RunHistoryStore
from assistant_agent.services.session_store import SessionStore, create_session_store
from assistant_agent.services.tool_history import ToolHistoryStore
from assistant_agent.services.tool_policy import max_result_chars_for_registered_tool
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceStore, append_observability_event
from assistant_agent.services.video_context import InMemoryVideoContextStore, VideoContextStore
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.tools.registry import ToolRegistry, create_default_registry, tool_execution_policy


PROGRESS_MESSAGES = {
    "product_search": "我查一下。",
    "price_compare": "我比一下价格。",
    "vision_understanding": "我看一下。",
    "video_understanding": "我分析一下。",
    "web_search": "我联网查一下。",
    "image_generation": "我开始生成，可能需要一点时间。",
}


def progress_message_for_tool(tool_name: str, *, tool_spec: ToolSpec | None = None) -> str:
    """Return the deterministic user-visible wait message for a tool."""

    policy_message = None
    if tool_spec is not None:
        policy_message = tool_spec.execution.progress_message
    if policy_message is None:
        policy_message = tool_execution_policy(tool_name).progress_message
    if policy_message:
        return policy_message
    return PROGRESS_MESSAGES.get(tool_name, "我处理一下。")


@dataclass(frozen=True)
class _ParallelToolRunResult:
    call_index: int
    state: AgentState
    tool_result: ToolResult
    events: list[AgentEvent]


class _BufferedEventSink:
    """Collect tool events emitted by an isolated parallel branch."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


class AgentGraphRuntime:
    """Run agent requests through the compiled LangGraph workflow."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        memory_store: Any | None = None,
        config: ProviderConfig | None = None,
        intent_detector: IntentDetector | None = None,
        router: ToolRouter | None = None,
        run_history: RunHistoryStore | None = None,
        session_store: SessionStore | None = None,
        tool_history: ToolHistoryStore | None = None,
        event_sink: EventSink | None = None,
        trace_store: TraceStore | None = None,
        chat_adapter: ChatAdapter | None = None,
        context_compactor: ContextCompactor | None = None,
        video_context_store: VideoContextStore | None = None,
        realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.config = config or ProviderConfig.from_env()
        self.video_context_store = video_context_store or InMemoryVideoContextStore()
        self.realtime_video_memory_store = realtime_video_memory_store or RealtimeVideoMemoryStore()
        self.memory_store = memory_store or create_memory_store(self.config)
        self.memory_manager = MemoryManager(self.memory_store)
        self.registry = registry or create_default_registry(
            self.config,
            video_context_store=self.video_context_store,
            realtime_video_memory_store=self.realtime_video_memory_store,
        )
        registry_get = getattr(self.registry, "get", None)
        if registry is not None and callable(registry_get):
            try:
                video_tool = registry_get("video_understanding")
            except KeyError:
                pass
            else:
                if getattr(video_tool, "memory_store", None) is None:
                    video_tool.memory_store = self.realtime_video_memory_store
        self.intent_detector = intent_detector or IntentDetector()
        self.router = router or ToolRouter()
        self.run_history = run_history
        self.session_store = session_store or create_session_store(self.config)
        self.tool_history = tool_history
        self.event_sink = event_sink
        self.trace_store = trace_store or InMemoryTraceStore()
        self.chat_adapter = chat_adapter or create_chat_adapter(self.config)
        self.context_compactor = context_compactor or create_context_compactor(self.config, self.chat_adapter)
        self.checkpointer = checkpointer if checkpointer is not None else create_checkpointer(self.config)
        self.tool_executor = ToolExecutor(
            registry=self.registry,
            tool_history=self.tool_history,
            event_sink=self.event_sink,
            context_metadata={"memory_manager": self.memory_manager},
        )
        self._conditional_graph = build_conditional_agent_graph()
        self._react_graph = build_assistant_loop_graph()
        self._graph = self._react_graph if self.config.agent_graph_mode == "assistant_loop" else self._conditional_graph

    def run_state(
        self,
        request: UserRequest,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
    ) -> AgentState:
        """Run the graph and return the full state for compatibility callers.

        ``event_sink`` overrides the runtime-level sink for this run only. The
        runtime stays shareable across concurrent runs (e.g. one per WebSocket
        connection) without mutating ``self.event_sink``.
        """

        base_event_sink = event_sink or self.event_sink
        run_event_sink = (
            _ResponseDeltaTrackingEventSink(base_event_sink)
            if base_event_sink is not None
            else None
        )
        # A per-run ToolExecutor binds the run's sink so tool events and agent
        # trace events emitted via graph_state["tool_executor"] reach it.
        tool_executor = ToolExecutor(
            registry=self.registry,
            tool_history=self.tool_history,
            event_sink=run_event_sink,
            context_metadata={"memory_manager": self.memory_manager},
            cancel_token=cancel_token,
        )
        state = AgentState.from_request(request)
        run_started_at = perf_counter()
        self._emit(
            AgentEvent(
                type="task_started",
                session_id=state.session_id,
                run_id=state.run_id,
                payload={"user_id": state.user_id},
            ),
            run_event_sink,
        )
        if self.run_history is not None:
            self.run_history.record_start(state.run_id, state.user_id, state.session_id)
        self._append_observability_event(
            state,
            canonical_event="run.started",
            status="started",
            attributes={
                "execution_strategy": state.execution_strategy,
                "native_runtime": self._should_use_native_runtime(),
            },
        )
        request.metadata["memory_core_status"] = build_memory_core_status(
            config=self.config,
            memory_store=self.memory_store,
        ).model_dump(mode="json")

        runtime_context = GraphRuntimeContext(
            intent_detector=self.intent_detector,
            router=self.router,
            tool_executor=tool_executor,
            chat_adapter=self.chat_adapter,
            context_compactor=self.context_compactor,
            memory_manager=self.memory_manager,
            trace_store=self.trace_store,
            event_sink=run_event_sink,
            cancel_token=cancel_token,
        )
        initial_state = {
            "request": request,
            "state": state,
            "outputs_by_step": {},
            "current_step_index": 0,
            "trace_id": state.trace_id,
            "max_tool_iterations": self.config.max_tool_iterations,
            "max_plan_steps": self.config.max_plan_steps,
            "max_plan_revisions": self.config.max_plan_revisions,
        }
        try:
            raise_if_cancelled(cancel_token, phase="pre_graph", state=state)
        except AgentRunCancelled as exc:
            state.cancel(exc.message, source=exc.source, details=exc.details)
        else:
            if self._should_use_native_runtime():
                native_started_at = perf_counter()
                try:
                    state = self._run_native_runtime(
                        request,
                        state=state,
                        tool_executor=tool_executor,
                        event_sink=run_event_sink,
                    )
                    raise_if_cancelled(cancel_token, phase="post_native_runtime", state=state)
                    save_memory_with_trace(
                        manager=self.memory_manager,
                        trace_store=self.trace_store,
                        trace_id=state.trace_id,
                        node_name="native_runtime",
                        state=state,
                        skipped_reason="native_runtime_memory_writes_are_llm_tool_calls",
                    )
                except AgentRunCancelled as exc:
                    if isinstance(exc.state, AgentState):
                        state = exc.state
                    state.cancel(exc.message, source=exc.source, details=exc.details)
                finally:
                    self._append_observability_event(
                        state,
                        canonical_event="native_runtime.finished",
                        node_name="native_runtime",
                        status=_phase_status(state.status),
                        latency_ms=int((perf_counter() - native_started_at) * 1000),
                        attributes={
                            "tool_count": len(state.tool_calls),
                            "error_count": len(state.errors),
                            "response_present": state.response is not None,
                        },
                    )
            else:
                self._emit(
                    AgentEvent(
                        type="graph_node_started",
                        session_id=state.session_id,
                        run_id=state.run_id,
                        node_name="agent_graph",
                    ),
                    run_event_sink,
                )
                try:
                    final_state = self._select_graph(request, runtime_context=runtime_context).invoke(
                        initial_state,
                        config=self._langgraph_config(request, state),
                    )
                    state = final_state["state"]
                    raise_if_cancelled(cancel_token, phase="post_graph", state=state)
                except AgentRunCancelled as exc:
                    if isinstance(exc.state, AgentState):
                        state = exc.state
                    state.cancel(exc.message, source=exc.source, details=exc.details)
                finally:
                    self._emit(
                        AgentEvent(
                            type="graph_node_finished",
                            session_id=state.session_id,
                            run_id=state.run_id,
                            node_name="agent_graph",
                        ),
                        run_event_sink,
                    )
        if self.run_history is not None:
            postprocess_started_at = perf_counter()
            terminal_status = _terminal_history_status(state.status)
            self.run_history.record_end(
                state.run_id,
                state.user_id,
                state.session_id,
                terminal_status,
                state.intent.intent if state.intent else None,
                [tool.tool_name for tool in state.selected_tools],
                int((perf_counter() - run_started_at) * 1000),
                error=state.errors[-1].message if state.errors else None,
            )
        else:
            postprocess_started_at = perf_counter()
            terminal_status = _terminal_history_status(state.status)
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
            },
        )
        terminal_event = {
            "completed": "run.completed",
            "failed": "run.failed",
            "cancelled": "run.cancelled",
        }[terminal_status]
        self._append_observability_event(
            state,
            canonical_event=terminal_event,
            status=terminal_status,
            latency_ms=int((perf_counter() - run_started_at) * 1000),
            attributes={
                "tool_count": len(state.tool_calls),
                "error_count": len(state.errors),
                "response_present": state.response is not None,
            },
            error=_latest_state_error(state),
        )
        if state.status == "failed":
            self._emit(
                AgentEvent(
                    type="task_failed",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    error=(
                        api_error_from_agent_error(state.errors[-1]).model_dump(mode="json")
                        if state.errors
                        else {"code": "TASK_FAILED", "message": "Agent run failed.", "detail": {}, "recoverable": False}
                    ),
                ),
                run_event_sink,
            )
        elif state.status == "cancelled":
            self._emit(
                AgentEvent(
                    type="task_cancelled",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    error=(
                        api_error_from_agent_error(state.errors[-1]).model_dump(mode="json")
                        if state.errors
                        else {
                            "code": "AGENT_RUN_CANCELLED",
                            "message": "Agent run cancelled.",
                            "detail": {},
                            "recoverable": False,
                        }
                    ),
                ),
                run_event_sink,
            )
        else:
            response_text = state.response.message if state.response else ""
            if response_text and run_event_sink is not None and not run_event_sink.response_delta_emitted:
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
            self._emit(
                AgentEvent(
                    type="final_response",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    text=response_text,
                ),
                run_event_sink,
            )
        _update_memory_core_status_from_recall(state.request.metadata)
        return state

    def _should_use_native_runtime(self) -> bool:
        """Use provider-native content/tool_calls for every non-mock runtime run."""

        return not _is_mock_chat_adapter(self.chat_adapter)

    def _run_native_chat_turn(self, chat_request: ChatRequest) -> ChatResult:
        if self.config.native_provider_streaming and supports_async_streaming_chat(self.chat_adapter):
            return ProviderStreamingTurnRunner().run_turn(self.chat_adapter, chat_request)
        return self.chat_adapter.chat(chat_request)

    def _plan_native_tool_schedule(
        self,
        tool_calls: list[Any],
        *,
        request: UserRequest,
        state: AgentState,
        remaining_tool_budget: int,
    ):
        scheduled_calls = []
        specs_by_name = {spec.name: spec for spec in self.registry.list_specs()}
        for call_index, call in enumerate(tool_calls):
            decision = native_tool_call_to_assistant_decision(call)
            validation = ActionValidator().validate(
                decision=decision,
                registry=self.registry,
                request=request,
                state=state,
            )
            scheduled_calls.append(
                build_scheduled_tool_call(
                    call_index=call_index,
                    decision=decision,
                    validation=validation,
                    native_call_id=call.id,
                    tool_spec=specs_by_name.get(decision.tool_name or ""),
                )
            )
            if not validation.accepted:
                break

        schedule = plan_tool_schedule(
            scheduled_calls,
            remaining_tool_budget=remaining_tool_budget,
            provider_budget_parallel_safe=_native_provider_budget_parallel_safe(state, len(scheduled_calls)),
        )
        schedule_metadata = schedule.to_metadata()
        state.request.metadata.setdefault("native_tool_schedules", []).append(schedule_metadata)
        state.request.metadata["last_native_tool_schedule"] = schedule_metadata
        return schedule

    def _run_native_parallel_tool_group(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        tool_executor: ToolExecutor,
        event_sink: EventSink | None,
        result: ChatResult,
        group: ToolExecutionGroup,
        observations: list[dict[str, Any]],
        native_calls: list[dict[str, Any]],
        iteration: int,
        max_iterations: int,
    ) -> AgentState | None:
        observation_start_index = len(observations)
        step_ids: dict[int, str] = {}
        for group_index, scheduled_call in enumerate(group.calls):
            call = result.tool_calls[scheduled_call.call_index]
            call_payload = call.model_dump(mode="json")
            native_calls.append(call_payload)
            state.request.metadata.setdefault("native_tool_calls", []).append(call_payload)
            _record_native_tool_call_preamble(
                state,
                tool_name=call.name,
                content=result.response_text,
            )
            decision = scheduled_call.decision
            _record_native_decision_metadata(
                state,
                request=request,
                decision=decision,
                iteration=iteration,
                max_iterations=max_iterations,
                safety_notes=["native_tool_call"],
            )
            self._append_observability_event(
                state,
                canonical_event="react.decision",
                node_name="native_runtime",
                status=decision.type,
                tool_name=decision.tool_name,
                attributes={
                    "iteration": iteration + 1,
                    "batch_index": scheduled_call.call_index + 1,
                    "batch_size": len(result.tool_calls),
                    "decision_type": decision.type,
                    "reason": decision.reason,
                    "safety_notes": decision.safety_notes,
                    "tool_schedule_mode": group.mode,
                    "tool_schedule_reason": group.reason,
                },
            )
            validation = scheduled_call.validation
            state.request.metadata["last_action_validator"] = validation.model_dump(mode="json")
            self._append_observability_event(
                state,
                canonical_event="action.validation.finished",
                node_name="native_runtime",
                status="accepted",
                tool_name=decision.tool_name,
                attributes={
                    **validation.model_dump(mode="json"),
                    "batch_index": scheduled_call.call_index + 1,
                    "batch_size": len(result.tool_calls),
                    "tool_schedule_mode": group.mode,
                    "tool_schedule_reason": group.reason,
                },
            )
            _emit_native_tool_progress_message(
                state,
                tool_name=decision.tool_name or call.name,
                event_sink=event_sink,
                tool_spec=scheduled_call.tool_spec,
            )
            step_ids[scheduled_call.call_index] = (
                decision.step_id or f"native_runtime_{observation_start_index + group_index + 1}"
            )

        base_tool_call_count = len(state.tool_calls)
        base_tool_result_count = len(state.tool_results)
        base_error_count = len(state.errors)
        base_provider_call_count = len(state.provider_budget.call_records)
        tool_results: dict[int, _ParallelToolRunResult] = {}
        with ThreadPoolExecutor(
            max_workers=len(group.calls),
            thread_name_prefix="native_tool_scheduler",
        ) as executor:
            future_to_call = {
                executor.submit(
                    _run_native_parallel_tool_call,
                    tool_executor,
                    state.model_copy(deep=True),
                    scheduled_call,
                    step_ids[scheduled_call.call_index],
                    trace_store=self.trace_store,
                    trace_id=state.trace_id,
                ): scheduled_call
                for scheduled_call in group.calls
            }
            for future in as_completed(future_to_call):
                scheduled_call = future_to_call[future]
                tool_results[scheduled_call.call_index] = future.result()

        _merge_native_parallel_state_records(
            state,
            base_tool_call_count=base_tool_call_count,
            base_tool_result_count=base_tool_result_count,
            base_error_count=base_error_count,
            base_provider_call_count=base_provider_call_count,
            ordered_results=[tool_results[scheduled_call.call_index] for scheduled_call in group.calls],
        )
        _replay_native_parallel_events(
            [tool_results[scheduled_call.call_index] for scheduled_call in group.calls],
            event_sink=event_sink,
        )

        for scheduled_call in group.calls:
            tool_result = tool_results[scheduled_call.call_index].tool_result
            observation = observation_from_tool_result(
                tool_result,
                request_text=request.text,
                prior_observations=observations,
                max_result_chars=max_result_chars_for_registered_tool(
                    self.registry,
                    tool_result.tool_name,
                ),
            ).model_dump(mode="json")
            observations.append(observation)
            state.request.metadata["native_runtime_observations"] = observations
            _record_native_observation_metadata(state, observation)
            _append_native_tool_observation_event(self, state, observation)

        if state.status == "failed":
            return state
        if len(observations) >= max_iterations:
            if self._request_native_final_answer_after_tool_limit(
                request,
                state=state,
                observations=observations,
                native_calls=native_calls,
                event_sink=event_sink,
                iteration=iteration,
                max_iterations=max_iterations,
            ):
                return state
            self._set_native_runtime_max_iteration_response(
                state,
                observations=observations,
                max_iterations=max_iterations,
            )
            return state
        return None

    def _run_native_runtime(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        tool_executor: ToolExecutor,
        event_sink: EventSink | None,
    ) -> AgentState:
        """Run provider-native content/tool_calls before entering the graph."""

        if not _chat_adapter_supports_native_tools(self.chat_adapter):
            _set_native_tooling_unsupported_response(state, self.chat_adapter)
            return state

        state.request.metadata["native_runtime"] = True
        state.request.metadata.setdefault("assistant_loop_steps", [])
        state.request.metadata["auto_task_summary_memory"] = {
            "skipped": True,
            "reason": "native_runtime_memory_writes_are_llm_tool_calls",
        }
        load_memory_with_trace(
            manager=self.memory_manager,
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="native_runtime",
            state=state,
            request=request,
        )

        observations: list[dict[str, Any]] = []
        native_calls: list[dict[str, Any]] = []
        max_iterations = max(1, self.config.max_tool_iterations)
        for iteration in range(max_iterations):
            stream_buffer = (
                _NativeRuntimeResponseBuffer(state, event_sink)
                if event_sink is not None
                else None
            )
            stream_callback = (
                stream_buffer.emit_delta
                if stream_buffer is not None
                else _native_runtime_stream_callback(state, event_sink)
            )
            chat_request = self._native_runtime_chat_request(
                request,
                state=state,
                observations=observations,
                native_calls=native_calls,
                stream_callback=stream_callback,
                iteration=iteration,
                max_iterations=max_iterations,
            )
            if chat_request is None:
                return state
            chat_started_at = perf_counter()
            result = self._run_native_chat_turn(chat_request)
            chat_wall_latency_ms = int((perf_counter() - chat_started_at) * 1000)
            self._record_native_runtime_chat_call(state, request, result)
            self._append_observability_event(
                state,
                canonical_event="llm.chat.finished",
                node_name="native_runtime",
                status="succeeded" if result.success else "failed",
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                attributes={
                    "iteration": iteration + 1,
                    "message_kind": result.message_kind,
                    "finish_reason": result.finish_reason,
                    "tool_call_count": len(result.tool_calls),
                    "provider_latency_ms": result.latency_ms,
                    "wall_latency_ms": chat_wall_latency_ms,
                    "usage": result.usage,
                },
                error=_chat_result_error(result),
            )
            if result.success and result.tool_calls:
                if stream_buffer is not None:
                    stream_buffer.discard()
                schedule = self._plan_native_tool_schedule(
                    result.tool_calls,
                    request=request,
                    state=state,
                    remaining_tool_budget=max_iterations - len(observations),
                )
                if _native_schedule_is_parallel(schedule):
                    parallel_result = self._run_native_parallel_tool_group(
                        request,
                        state=state,
                        tool_executor=tool_executor,
                        event_sink=event_sink,
                        result=result,
                        group=schedule.groups[0],
                        observations=observations,
                        native_calls=native_calls,
                        iteration=iteration,
                        max_iterations=max_iterations,
                    )
                    if parallel_result is not None:
                        return parallel_result
                    continue
                scheduled_by_index = {
                    scheduled_call.call_index: scheduled_call
                    for group in schedule.groups
                    for scheduled_call in group.calls
                }
                for call_index, call in enumerate(result.tool_calls):
                    if len(observations) >= max_iterations:
                        self._record_native_tool_calls_skipped_for_budget(
                            state,
                            skipped_count=len(result.tool_calls) - call_index,
                        )
                        if self._request_native_final_answer_after_tool_limit(
                            request,
                            state=state,
                            observations=observations,
                            native_calls=native_calls,
                            event_sink=event_sink,
                            iteration=iteration,
                            max_iterations=max_iterations,
                        ):
                            return state
                        self._set_native_runtime_max_iteration_response(
                            state,
                            observations=observations,
                            max_iterations=max_iterations,
                        )
                        return state

                    call_payload = call.model_dump(mode="json")
                    native_calls.append(call_payload)
                    state.request.metadata.setdefault("native_tool_calls", []).append(call_payload)
                    _record_native_tool_call_preamble(state, tool_name=call.name, content=result.response_text)
                    decision = native_tool_call_to_assistant_decision(call)
                    _record_native_decision_metadata(
                        state,
                        request=request,
                        decision=decision,
                        iteration=iteration,
                        max_iterations=max_iterations,
                        safety_notes=["native_tool_call"],
                    )
                    self._append_observability_event(
                        state,
                        canonical_event="react.decision",
                        node_name="native_runtime",
                        status=decision.type,
                        tool_name=decision.tool_name,
                        attributes={
                            "iteration": iteration + 1,
                            "batch_index": call_index + 1,
                            "batch_size": len(result.tool_calls),
                            "decision_type": decision.type,
                            "reason": decision.reason,
                            "safety_notes": decision.safety_notes,
                        },
                    )
                    validation = ActionValidator().validate(
                        decision=decision,
                        registry=self.registry,
                        request=request,
                        state=state,
                    )
                    state.request.metadata["last_action_validator"] = validation.model_dump(mode="json")
                    self._append_observability_event(
                        state,
                        canonical_event="action.validation.finished",
                        node_name="native_runtime",
                        status="accepted" if validation.accepted else "rejected",
                        tool_name=decision.tool_name,
                        attributes={
                            **validation.model_dump(mode="json"),
                            "batch_index": call_index + 1,
                            "batch_size": len(result.tool_calls),
                        },
                        error={"code": validation.code, "message": validation.message} if not validation.accepted else None,
                    )
                    if not validation.accepted:
                        observation = rejected_observation(
                            tool_name=decision.tool_name or "unknown",
                            error_code=validation.code,
                            error_message=validation.message,
                        ).model_dump(mode="json")
                        observations.append(observation)
                        state.request.metadata["native_runtime_observations"] = observations
                        _record_native_observation_metadata(state, observation)
                        _append_native_tool_observation_event(self, state, observation)
                        _set_native_validation_rejection_response(state, validation.model_dump(mode="json"))
                        return state

                    _emit_native_tool_progress_message(
                        state,
                        tool_name=decision.tool_name or call.name,
                        event_sink=event_sink,
                        tool_spec=(
                            scheduled_by_index[call_index].tool_spec
                            if call_index in scheduled_by_index
                            else None
                        ),
                    )
                    tool_result = tool_executor.run_tool(
                        state,
                        decision.step_id or f"native_runtime_{len(observations) + 1}",
                        decision.tool_name or "",
                        decision.tool_input or {},
                        trace_store=self.trace_store,
                        trace_id=state.trace_id,
                        node_name="native_runtime",
                    )
                    observation = observation_from_tool_result(
                        tool_result,
                        request_text=request.text,
                        prior_observations=observations,
                        max_result_chars=max_result_chars_for_registered_tool(
                            self.registry,
                            tool_result.tool_name,
                        ),
                    ).model_dump(mode="json")
                    observations.append(observation)
                    state.request.metadata["native_runtime_observations"] = observations
                    _record_native_observation_metadata(state, observation)
                    _append_native_tool_observation_event(self, state, observation)
                    if state.status == "failed":
                        return state
                    if len(observations) >= max_iterations:
                        remaining_calls = len(result.tool_calls) - call_index - 1
                        if remaining_calls:
                            self._record_native_tool_calls_skipped_for_budget(
                                state,
                                skipped_count=remaining_calls,
                            )
                        if self._request_native_final_answer_after_tool_limit(
                            request,
                            state=state,
                            observations=observations,
                            native_calls=native_calls,
                            event_sink=event_sink,
                            iteration=iteration,
                            max_iterations=max_iterations,
                        ):
                            return state
                        self._set_native_runtime_max_iteration_response(
                            state,
                            observations=observations,
                            max_iterations=max_iterations,
                        )
                        return state
                continue

            if stream_buffer is not None:
                stream_buffer.flush()
            self._set_native_runtime_response(state, result, observations)
            return state

        self._set_native_runtime_max_iteration_response(
            state,
            observations=observations,
            max_iterations=max_iterations,
        )
        return state

    def _native_runtime_chat_request(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        observations: list[dict[str, Any]],
        native_calls: list[dict[str, Any]],
        stream_callback: Any | None,
        iteration: int,
        max_iterations: int,
    ) -> ChatRequest | None:
        profile = _system_prompt_profile_from_request(request)
        tool_specs = [] if profile == SystemPromptProfile.FINAL_ONLY else _native_runtime_tool_specs(self.registry, state)
        if tool_specs is None:
            return None
        context_pack = build_traced_assistant_context_pack(
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="native_runtime",
            state=state,
            request=request,
            observations=observations,
            tool_specs=tool_specs,
            iteration=iteration,
            max_iterations=max_iterations,
            context_compactor=None,
        )
        mode = (
            PromptCompileMode.NATIVE_FINAL_ONLY
            if profile == SystemPromptProfile.FINAL_ONLY
            else PromptCompileMode.NATIVE_TOOL
        )
        compilation = PromptCompiler().compile(
            PromptCompileRequest(
                user_id=state.user_id,
                session_id=state.session_id,
                mode=mode,
                user_query_fallback="native runtime assistant turn",
                profile=profile,
                options=_system_prompt_options_from_request(request),
                context_pack=context_pack,
                observations=tuple(observations),
                native_calls=tuple(native_calls),
                tool_call_id_prefix="native_runtime_call_",
                stream_callback=stream_callback,
            )
        )
        self._record_native_runtime_context_report(
            state,
            context_pack=context_pack,
            compilation=compilation,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        return compilation.chat_request

    def _request_native_final_answer_after_tool_limit(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        observations: list[dict[str, Any]],
        native_calls: list[dict[str, Any]],
        event_sink: EventSink | None,
        iteration: int,
        max_iterations: int,
    ) -> bool:
        stream_callback = _native_runtime_stream_callback(state, event_sink)
        chat_request = self._native_runtime_final_only_chat_request(
            request,
            state=state,
            observations=observations,
            native_calls=native_calls,
            stream_callback=stream_callback,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        chat_started_at = perf_counter()
        result = self._run_native_chat_turn(chat_request)
        chat_wall_latency_ms = int((perf_counter() - chat_started_at) * 1000)
        self._record_native_runtime_chat_call(
            state,
            request,
            result,
            capability="direct_chat",
        )
        self._append_observability_event(
            state,
            canonical_event="llm.chat.finished",
            node_name="native_runtime",
            status="succeeded" if result.success else "failed",
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            attributes={
                "iteration": iteration + 2,
                "max_iterations": max_iterations,
                "final_only_handoff": True,
                "message_kind": result.message_kind,
                "finish_reason": result.finish_reason,
                "tool_call_count": len(result.tool_calls),
                "provider_latency_ms": result.latency_ms,
                "wall_latency_ms": chat_wall_latency_ms,
                "usage": result.usage,
            },
            error=_chat_result_error(result),
        )
        if result.success and not result.tool_calls:
            self._set_native_runtime_response(state, result, observations)
            if state.response is not None:
                state.response.data["final_only_handoff"] = True
            return True

        metadata = state.request.metadata
        if result.tool_calls:
            metadata["native_runtime_final_only_returned_tool_call"] = True
            metadata["native_runtime_final_only_handoff_failed"] = False
        else:
            metadata["native_runtime_final_only_handoff_failed"] = True
        if result.errors:
            metadata["native_runtime_final_only_error_code"] = result.errors[0].code
        return False

    def _native_runtime_final_only_chat_request(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        observations: list[dict[str, Any]],
        native_calls: list[dict[str, Any]],
        stream_callback: Any | None,
        iteration: int,
        max_iterations: int,
    ) -> ChatRequest:
        context_pack = build_traced_assistant_context_pack(
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="native_runtime",
            state=state,
            request=request,
            observations=observations,
            tool_specs=[],
            iteration=iteration + 1,
            max_iterations=max_iterations,
            context_compactor=None,
        )
        compilation = PromptCompiler().compile(
            PromptCompileRequest(
                user_id=state.user_id,
                session_id=state.session_id,
                mode=PromptCompileMode.NATIVE_FINAL_ONLY,
                user_query_fallback="native runtime final answer",
                profile=SystemPromptProfile.FINAL_ONLY,
                options=_system_prompt_options_from_request(request),
                context_pack=context_pack,
                observations=tuple(observations),
                native_calls=tuple(native_calls),
                tool_call_id_prefix="native_runtime_call_",
                stream_callback=stream_callback,
            )
        )
        self._record_native_runtime_context_report(
            state,
            context_pack=context_pack,
            compilation=compilation,
            iteration=iteration + 1,
            max_iterations=max_iterations,
        )
        return compilation.chat_request

    def _record_native_runtime_context_report(
        self,
        state: AgentState,
        *,
        context_pack: Any,
        compilation: PromptCompileResult,
        iteration: int,
        max_iterations: int,
    ) -> None:
        report = build_context_report(
            context_pack,
            system_prompt=compilation.system_instruction,
            selected_tool_specs=list(compilation.selected_tool_specs),
        ).model_dump(mode="json")
        state.request.metadata["last_context_report_v1"] = report
        self._append_observability_event(
            state,
            canonical_event="context.report",
            node_name="native_runtime",
            status="succeeded",
            attributes={
                "iteration": iteration + 1,
                "max_iterations": max_iterations,
                "selected_tool_count": len(compilation.selected_tool_specs),
                "compression_stage": report.get("compression_stage"),
            },
            output_summary={"context_report_v1": report},
        )

    def _record_native_runtime_chat_call(
        self,
        state: AgentState,
        request: UserRequest,
        result: Any,
        *,
        capability: str | None = None,
    ) -> None:
        state.provider_budget.record_call(
            run_id=state.run_id,
            capability=capability or ("direct_chat" if not result.tool_calls else "assistant_native_tool_call"),
            provider=result.provider,
            model=result.model,
            input_size_bytes=len((request.text or "").encode("utf-8")),
            latency_ms=result.latency_ms,
            status="succeeded" if result.success else "failed",
        )

    def _set_native_runtime_response(
        self,
        state: AgentState,
        result: Any,
        observations: list[dict[str, Any]],
    ) -> None:
        errors = [error.model_dump(mode="json") for error in result.errors]
        if not result.success:
            first_error = result.errors[0] if result.errors else None
            message = (
                f"处理失败：{first_error.code}: {first_error.message}"
                if first_error is not None
                else "处理失败：provider returned an error."
            )
            details: dict[str, Any] = {"errors": errors}
            if first_error is not None:
                details["code"] = first_error.code
                details["retryable"] = first_error.recoverable
            state.errors.append(
                AgentError(
                    message=message,
                    source="native_runtime",
                    details=details,
                )
            )
            state.response = AgentResponse(
                message=message,
                data={
                    "native_runtime": True,
                    "provider": result.provider,
                    "model": result.model,
                    "errors": errors,
                    "provider_budget": state.provider_budget.summary(),
                },
            )
            state.status = "failed"
            append_response_final_event(
                trace_store=self.trace_store,
                trace_id=state.trace_id,
                node_name="native_runtime",
                state=state,
                source="native_runtime",
            )
            return
        else:
            message = result.refusal or result.response_text or "已处理请求。"
        decision = AssistantDecision(
            type="final_answer",
            message=message,
            reason=_native_finish_reason(result, fallback="Provider finished without requesting a tool."),
            safety_notes=["provider_refusal"] if result.refusal else [],
        )
        _record_native_decision_metadata(
            state,
            request=state.request,
            decision=decision,
            iteration=len(observations),
            max_iterations=max(1, self.config.max_tool_iterations),
            safety_notes=decision.safety_notes,
        )
        self._append_observability_event(
            state,
            canonical_event="react.decision",
            node_name="native_runtime",
            status=decision.type,
            attributes={
                "iteration": len(observations) + 1,
                "decision_type": decision.type,
                "reason": decision.reason,
                "message_present": bool(decision.message),
                "safety_notes": decision.safety_notes,
            },
        )
        state.set_response(
            AgentResponse(
                message=message,
                data={
                    "native_runtime": True,
                    "reason": decision.reason,
                    "provider": result.provider,
                    "model": result.model,
                    "usage": result.usage,
                    "finish_reason": result.finish_reason,
                    "message_kind": result.message_kind,
                    "tool_count": len(state.tool_calls),
                    "tool_observations": len(observations),
                    "errors": errors,
                    "provider_budget": state.provider_budget.summary(),
                },
                output_refs=[result.output_ref] if result.output_ref else [],
            )
        )
        append_response_final_event(
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="native_runtime",
            state=state,
            source="native_runtime",
        )

    def _set_native_runtime_max_iteration_response(
        self,
        state: AgentState,
        *,
        observations: list[dict[str, Any]],
        max_iterations: int,
    ) -> None:
        metadata = state.request.metadata
        state.set_response(
            AgentResponse(
                message=f"已达到最大工具调用次数 ({max_iterations})，这是我能提供的最好回答。",
                data={
                    "native_runtime": True,
                    "tool_count": len(state.tool_calls),
                    "tool_observations": len(observations),
                    "final_only_handoff_failed": bool(metadata.get("native_runtime_final_only_handoff_failed")),
                    "final_only_returned_tool_call": bool(metadata.get("native_runtime_final_only_returned_tool_call")),
                    "final_only_error_code": metadata.get("native_runtime_final_only_error_code"),
                    "provider_budget": state.provider_budget.summary(),
                },
            )
        )

    def _record_native_tool_calls_skipped_for_budget(
        self,
        state: AgentState,
        *,
        skipped_count: int,
    ) -> None:
        if skipped_count <= 0:
            return
        metadata = state.request.metadata
        current = metadata.get("native_runtime_tool_calls_skipped_for_budget")
        previous = current if isinstance(current, int) and current >= 0 else 0
        metadata["native_runtime_tool_calls_skipped_for_budget"] = previous + skipped_count
        metadata["native_runtime_tool_call_budget_exhausted"] = True

    def _select_graph(
        self,
        request: UserRequest,
        *,
        runtime_context: GraphRuntimeContext | None = None,
    ) -> Any:
        if self.config.agent_graph_mode != "assistant_loop":
            if runtime_context is not None:
                return build_conditional_agent_graph(
                    checkpointer=self.checkpointer,
                    runtime_context=runtime_context,
                )
            return self._conditional_graph
        if runtime_context is not None:
            return build_assistant_loop_graph(
                checkpointer=self.checkpointer,
                runtime_context=runtime_context,
            )
        return self._react_graph

    def _langgraph_config(self, request: UserRequest, state: AgentState) -> dict[str, dict[str, str]]:
        return {
            "configurable": {
                "thread_id": state.run_id,
                "session_id": request.session_id,
                "user_id": request.user_id,
                "run_id": state.run_id,
            }
        }

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

        state = self.run_state(request, event_sink=event_sink, cancel_token=cancel_token)
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
                "intent": state.intent.intent if state.intent else None,
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
            attributes=attributes,
            output_summary=output_summary,
            error=error,
        )


def _terminal_history_status(status: str) -> Literal["completed", "failed", "cancelled"]:
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "completed"


def _phase_status(status: str) -> str:
    if status in {"failed", "cancelled"}:
        return status
    return "succeeded"


def _latest_state_error(state: AgentState) -> dict[str, Any] | None:
    if not state.errors:
        return None
    error = state.errors[-1]
    return {
        "code": error.details.get("code", "unknown_error"),
        "message": error.message,
        "source": error.source,
        "recovery_action": error.details.get("recovery_action"),
    }


def _update_memory_core_status_from_recall(metadata: dict[str, Any]) -> None:
    core_status = metadata.get("memory_core_status")
    recall_report = metadata.get("memory_recall_report")
    if not isinstance(core_status, dict) or not isinstance(recall_report, dict):
        return
    error_codes = recall_report.get("search_error_codes")
    errors = (
        [{"code": code} for code in error_codes if isinstance(code, str) and code]
        if isinstance(error_codes, list)
        else []
    )
    metadata["memory_core_status"] = update_memory_core_status_errors(
        core_status,
        remote_errors=errors,
    )


def _chat_result_error(result: Any) -> dict[str, Any] | None:
    errors = getattr(result, "errors", None)
    if not errors:
        return None
    first = errors[0]
    return {
        "code": getattr(first, "code", "provider_unknown_error"),
        "message": getattr(first, "message", "provider error"),
        "recoverable": getattr(first, "recoverable", False),
    }


def _native_schedule_is_parallel(schedule: Any) -> bool:
    if not getattr(schedule, "groups", None):
        return False
    first_group = schedule.groups[0]
    return first_group.mode == "parallel" and len(first_group.calls) > 1


def _native_provider_budget_parallel_safe(state: AgentState, call_count: int) -> bool:
    budget = state.provider_budget
    if budget.max_calls_per_capability:
        return False
    if budget.max_estimated_cost_per_run is not None or budget.max_input_bytes_per_run is not None:
        return False
    remaining_provider_calls = budget.max_provider_calls_per_run - budget.provider_call_count
    return remaining_provider_calls >= call_count


def _run_native_parallel_tool_call(
    tool_executor: ToolExecutor,
    branch_state: AgentState,
    scheduled_call: Any,
    step_id: str,
    *,
    trace_store: TraceStore | None,
    trace_id: str | None,
) -> _ParallelToolRunResult:
    event_buffer = _BufferedEventSink()
    branch_executor = ToolExecutor(
        registry=tool_executor.registry,
        tool_history=tool_executor.tool_history,
        event_sink=event_buffer,
        recovery_policy=tool_executor.recovery_policy,
        execution_policy=tool_executor.execution_policy,
        context_metadata=tool_executor.context_metadata,
        cancel_token=tool_executor.cancel_token,
        idempotency_ledger=tool_executor.idempotency_ledger,
    )
    result = branch_executor.run_tool(
        branch_state,
        step_id,
        scheduled_call.decision.tool_name or "",
        scheduled_call.decision.tool_input or {},
        None,
        trace_store,
        trace_id,
        "native_runtime",
    )
    return _ParallelToolRunResult(
        call_index=scheduled_call.call_index,
        state=branch_state,
        tool_result=result,
        events=list(event_buffer.events),
    )


def _merge_native_parallel_state_records(
    state: AgentState,
    *,
    base_tool_call_count: int,
    base_tool_result_count: int,
    base_error_count: int,
    base_provider_call_count: int,
    ordered_results: list[_ParallelToolRunResult],
) -> None:
    state.tool_calls = list(state.tool_calls[:base_tool_call_count])
    state.tool_results = list(state.tool_results[:base_tool_result_count])
    state.errors = list(state.errors[:base_error_count])
    state.provider_budget.call_records = list(state.provider_budget.call_records[:base_provider_call_count])
    failed = False
    any_tool_record = False
    for result in ordered_results:
        state.tool_calls.extend(result.state.tool_calls[base_tool_call_count:])
        state.tool_results.extend(result.state.tool_results[base_tool_result_count:])
        state.errors.extend(result.state.errors[base_error_count:])
        state.provider_budget.call_records.extend(
            result.state.provider_budget.call_records[base_provider_call_count:]
        )
        any_tool_record = any_tool_record or len(result.state.tool_calls) > base_tool_call_count
        failed = failed or result.state.status == "failed"
    if failed:
        state.status = "failed"
    elif any_tool_record:
        state.status = "running"


def _replay_native_parallel_events(
    ordered_results: list[_ParallelToolRunResult],
    *,
    event_sink: EventSink | None,
) -> None:
    if event_sink is None:
        return
    for result in ordered_results:
        for event in result.events:
            event_sink.emit(event)


class _ResponseDeltaTrackingEventSink:
    """Forward events while tracking whether user-visible response chunks exist."""

    def __init__(self, inner: EventSink) -> None:
        self.inner = inner
        self.response_delta_emitted = False

    def emit(self, event: AgentEvent) -> None:
        if event.type == "response_delta":
            self.response_delta_emitted = True
        self.inner.emit(event)


class _NativeRuntimeResponseBuffer:
    """Buffer first-call model text until the runtime knows it is not a tool call."""

    def __init__(self, state: AgentState, event_sink: EventSink) -> None:
        self.state = state
        self.event_sink = event_sink
        self.events: list[AgentEvent] = []

    def emit_delta(self, text: str, payload: dict[str, Any]) -> None:
        event = stream_delta_to_agent_event(
            text,
            payload,
            session_id=self.state.session_id,
            run_id=self.state.run_id,
            source="assistant_native_final_answer",
        )
        if event is None:
            return
        self.events.append(event)

    def flush(self) -> None:
        for event in self.events:
            self.event_sink.emit(event)
        self.events.clear()

    def discard(self) -> None:
        self.events.clear()


def _is_mock_chat_adapter(chat_adapter: ChatAdapter) -> bool:
    return getattr(chat_adapter, "provider", "") == "mock"


def _system_prompt_profile_from_request(request: UserRequest) -> SystemPromptProfile:
    metadata = request.metadata
    explicit = _metadata_text(metadata.get("system_prompt_profile"))
    if explicit:
        try:
            return SystemPromptProfile(explicit)
        except ValueError:
            return SystemPromptProfile.TEXT_DEFAULT
    channel = _metadata_text(metadata.get("channel"))
    if channel == SystemPromptProfile.REALTIME_PHONE.value:
        return SystemPromptProfile.REALTIME_PHONE
    source = _metadata_text(metadata.get("source"))
    if source in {"phone_runtime"}:
        return SystemPromptProfile.REALTIME_PHONE
    return SystemPromptProfile.TEXT_DEFAULT


def _system_prompt_options_from_request(request: UserRequest) -> SystemPromptOptions:
    metadata = request.metadata
    locale = _metadata_text(metadata.get("locale")) or _metadata_text(metadata.get("language")) or "zh-CN"
    channel = _metadata_text(metadata.get("channel")) or "text"
    return SystemPromptOptions(
        locale=locale,
        channel=channel,
        product_mode=metadata.get("product_mode") is True,
        allow_web_search=metadata.get("allow_web_search") is not False,
        allow_memory_tools=metadata.get("allow_memory_tools") is not False,
    )


def _metadata_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _chat_adapter_supports_native_tools(chat_adapter: ChatAdapter) -> bool:
    capabilities = getattr(chat_adapter, "capabilities", None)
    if capabilities is None:
        return True
    return bool(getattr(capabilities, "supports_native_tools", True))


def _native_runtime_tool_specs(registry: Any, state: AgentState) -> list[ToolSpec] | None:
    try:
        if hasattr(registry, "list_specs"):
            specs = registry.list_specs()
        else:
            specs = registry.describe_tools()
        return [spec if isinstance(spec, ToolSpec) else ToolSpec.model_validate(spec) for spec in specs]
    except Exception as exc:
        _set_native_tool_description_failure_response(state, exc)
        return None


def _set_native_tool_description_failure_response(state: AgentState, exc: Exception) -> None:
    message = f"工具描述读取失败：{exc}"
    state.request.metadata["tool_description_error"] = {
        "code": "tool_description_unavailable",
        "message": str(exc),
    }
    error = AgentError(
        message=message,
        source="native_runtime",
        details={"code": "tool_description_unavailable", "recovery_action": "stop_with_error"},
    )
    state.errors.append(error)
    state.response = AgentResponse(
        message="工具描述不可用，无法安全执行 agent runtime。",
        data={
            "native_runtime": True,
            "errors": [{"code": error.details["code"], "message": message}],
            "provider_budget": state.provider_budget.summary(),
        },
    )
    state.status = "failed"


def _set_native_tooling_unsupported_response(state: AgentState, chat_adapter: ChatAdapter) -> None:
    provider = str(getattr(chat_adapter, "provider", "unknown") or "unknown")
    model = getattr(chat_adapter, "model", None)
    message = f"{provider} chat adapter does not support provider-native tool calling."
    error = AgentError(
        message=message,
        source="native_runtime",
        details={
            "code": "native_tool_calling_unsupported",
            "provider": provider,
            "model": model,
            "recovery_action": "configure_native_tool_provider",
        },
    )
    state.errors.append(error)
    state.response = AgentResponse(
        message="当前模型不支持原生工具调用，无法执行 agent runtime。",
        data={
            "native_runtime": True,
            "provider": provider,
            "model": model,
            "errors": [
                {
                    "code": error.details["code"],
                    "message": message,
                    "provider": provider,
                    "model": model,
                }
            ],
        },
    )
    state.status = "failed"


def _set_native_validation_rejection_response(
    state: AgentState,
    validator_result: dict[str, Any],
) -> None:
    message = str(validator_result.get("message") or "工具调用未通过校验。")
    code = str(validator_result.get("code") or "invalid_tool_call")
    state.errors.append(
        AgentError(
            message=message,
            source="native_runtime",
            details={
                "code": code,
                "recovery_action": "stop_with_error",
                "validator_result": validator_result,
            },
        )
    )
    state.set_response(
        AgentResponse(
            message=f"我没有执行这个工具调用：{message}",
            data={
                "native_runtime": True,
                "assistant_decision": "final_answer",
                "validator_result": validator_result,
                "tool_count": len(state.tool_calls),
                "errors": [{"code": code, "message": message}],
                "provider_budget": state.provider_budget.summary(),
            },
        )
    )


def _record_native_decision_metadata(
    state: AgentState,
    *,
    request: UserRequest,
    decision: AssistantDecision,
    iteration: int,
    max_iterations: int,
    safety_notes: list[str] | None = None,
) -> None:
    audit = build_memory_tool_selection_audit(
        request=request,
        decision=decision,
        state=state,
        iteration=iteration,
        max_iterations=max_iterations,
        is_mock=False,
    )
    record_memory_tool_selection_audit(request, audit)
    steps = request.metadata.setdefault("assistant_loop_steps", [])
    if not isinstance(steps, list):
        steps = []
        request.metadata["assistant_loop_steps"] = steps
    steps.append(
        {
            "iteration": iteration + 1,
            "decision_type": decision.type,
            "tool_name": decision.tool_name,
            "message": decision.message,
            "reason": decision.reason,
            "safety_notes": safety_notes or decision.safety_notes,
        }
    )
    trace = request.metadata.setdefault("decision_trace", [])
    if not isinstance(trace, list):
        trace = []
        request.metadata["decision_trace"] = trace
    trace.append(
        {
            "iteration": iteration + 1,
            "decision_type": decision.type,
            "tool_name": decision.tool_name,
            "answer": decision.message if decision.type in {"final_answer", "ask_followup"} else None,
            "reason": decision.reason,
        }
    )


def _record_native_observation_metadata(state: AgentState, observation: dict[str, Any]) -> None:
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if not isinstance(steps, list):
        steps = []
        state.request.metadata["assistant_loop_steps"] = steps
    steps.append(
        {
            "observation_tool": observation.get("tool_name"),
            "status": observation.get("status"),
            "success": observation.get("status") == "succeeded",
            "output_ref": observation.get("output_ref"),
            "summary": observation.get("summary"),
            "next_step_hint": observation.get("next_step_hint"),
            "error": observation.get("error_message"),
        }
    )


def _append_native_tool_observation_event(
    runtime: AgentGraphRuntime,
    state: AgentState,
    observation: dict[str, Any],
) -> None:
    runtime._append_observability_event(
        state,
        canonical_event="tool.observation",
        node_name="native_runtime",
        status=observation.get("status"),
        tool_name=observation.get("tool_name"),
        attributes={
            "summary": observation.get("summary"),
            "output_ref": observation.get("output_ref"),
            "next_step_hint": observation.get("next_step_hint"),
        },
        output_summary={
            "summary": observation.get("summary"),
            "output_ref": observation.get("output_ref"),
            "next_step_hint": observation.get("next_step_hint"),
        },
        error={
            "code": observation.get("error_code"),
            "message": observation.get("error_message"),
        }
        if observation.get("error_code")
        else None,
    )


def _record_native_tool_call_preamble(state: AgentState, *, tool_name: str, content: str) -> None:
    normalized = content.strip()
    if not normalized:
        return
    preambles = state.request.metadata.setdefault("native_tool_call_preambles", [])
    if not isinstance(preambles, list):
        preambles = []
        state.request.metadata["native_tool_call_preambles"] = preambles
    preambles.append({"tool_name": tool_name, "content": normalized})


def _emit_native_tool_progress_message(
    state: AgentState,
    *,
    tool_name: str,
    event_sink: EventSink | None,
    tool_spec: ToolSpec | None = None,
) -> None:
    if event_sink is None:
        return
    text = progress_message_for_tool(tool_name, tool_spec=tool_spec)
    event_sink.emit(
        AgentEvent(
            type="progress_message",
            session_id=state.session_id,
            run_id=state.run_id,
            tool_name=tool_name,
            text=text,
            payload={
                "source": "native_tool_wait",
                "replaceable": True,
                "tool_name": tool_name,
            },
        )
    )


def _native_finish_reason(result: Any, *, fallback: str) -> str:
    if result.finish_reason:
        return f"{fallback} finish_reason={result.finish_reason}."
    if result.message_kind:
        return f"{fallback} message_kind={result.message_kind}."
    return fallback


def _native_runtime_stream_callback(state: AgentState, event_sink: EventSink | None) -> Any | None:
    if event_sink is None:
        return None

    def emit_delta(text: str, payload: dict[str, Any]) -> None:
        event = stream_delta_to_agent_event(
            text,
            payload,
            session_id=state.session_id,
            run_id=state.run_id,
            source="assistant_native_final_answer",
        )
        if event is None:
            return
        event_sink.emit(event)

    return emit_delta
