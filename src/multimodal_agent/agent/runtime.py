"""Default LangGraph runtime for agent execution."""

from time import perf_counter
from typing import Any

from multimodal_agent.agent.conditional_graph import build_conditional_agent_graph
from multimodal_agent.agent.assistant_loop_graph import build_assistant_loop_graph
from multimodal_agent.agent.graph_runtime import GraphRuntimeContext
from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.memory.factory import create_memory_store
from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.api import api_error_from_agent_error
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.requests import AgentResponse, UserRequest
from multimodal_agent.services.event_sink import EventSink
from multimodal_agent.services.chat_adapter import ChatAdapter, create_chat_adapter
from multimodal_agent.services.checkpointer import create_checkpointer
from multimodal_agent.services.run_history import RunHistoryStore
from multimodal_agent.services.session_store import SessionStore, create_session_store
from multimodal_agent.services.tool_history import ToolHistoryStore
from multimodal_agent.services.trace_store import InMemoryTraceStore, TraceStore
from multimodal_agent.services.video_context import InMemoryVideoContextStore, VideoContextStore
from multimodal_agent.tools.registry import ToolRegistry, create_default_registry


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
        video_context_store: VideoContextStore | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.config = config or ProviderConfig.from_env()
        self.video_context_store = video_context_store or InMemoryVideoContextStore()
        self.memory_store = memory_store or create_memory_store(self.config)
        self.memory_manager = MemoryManager(self.memory_store)
        self.registry = registry or create_default_registry(self.config, video_context_store=self.video_context_store)
        self.intent_detector = intent_detector or IntentDetector()
        self.router = router or ToolRouter()
        self.run_history = run_history
        self.session_store = session_store or create_session_store(self.config)
        self.tool_history = tool_history
        self.event_sink = event_sink
        self.trace_store = trace_store or InMemoryTraceStore()
        self.chat_adapter = chat_adapter or create_chat_adapter(self.config)
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

    def run_state(self, request: UserRequest, event_sink: EventSink | None = None) -> AgentState:
        """Run the graph and return the full state for compatibility callers.

        ``event_sink`` overrides the runtime-level sink for this run only. The
        runtime stays shareable across concurrent runs (e.g. one per WebSocket
        connection) without mutating ``self.event_sink``.
        """

        run_event_sink = event_sink or self.event_sink
        # A per-run ToolExecutor binds the run's sink so tool events and agent
        # trace events emitted via graph_state["tool_executor"] reach it.
        tool_executor = ToolExecutor(
            registry=self.registry,
            tool_history=self.tool_history,
            event_sink=run_event_sink,
            context_metadata={"memory_manager": self.memory_manager},
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

        runtime_context = GraphRuntimeContext(
            intent_detector=self.intent_detector,
            router=self.router,
            tool_executor=tool_executor,
            chat_adapter=self.chat_adapter,
            memory_manager=self.memory_manager,
            trace_store=self.trace_store,
        )
        initial_state = {
            "request": request,
            "state": state,
            "outputs_by_step": {},
            "current_step_index": 0,
            "trace_id": state.trace_id,
            "assistant_tool_call_mode": self.config.assistant_tool_call_mode,
            "max_tool_iterations": self.config.max_tool_iterations,
            "max_plan_steps": self.config.max_plan_steps,
            "max_plan_revisions": self.config.max_plan_revisions,
        }
        self._emit(AgentEvent(type="graph_node_started", session_id=state.session_id, run_id=state.run_id, node_name="agent_graph"), run_event_sink)
        final_state = self._select_graph(request, runtime_context=runtime_context).invoke(
            initial_state,
            config=self._langgraph_config(request, state),
        )
        state = final_state["state"]
        self._emit(AgentEvent(type="graph_node_finished", session_id=state.session_id, run_id=state.run_id, node_name="agent_graph"), run_event_sink)
        if self.run_history is not None:
            self.run_history.record_end(
                state.run_id,
                state.user_id,
                state.session_id,
                "failed" if state.status == "failed" else "completed",
                state.intent.intent if state.intent else None,
                [tool.tool_name for tool in state.selected_tools],
                int((perf_counter() - run_started_at) * 1000),
                error=state.errors[-1].message if state.errors else None,
            )
        self.session_store.touch_run(
            user_id=state.user_id,
            session_id=state.session_id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            message_preview=request.text or "",
            status="failed" if state.status == "failed" else "completed",
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
        else:
            self._emit(
                AgentEvent(
                    type="final_response",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    text=state.response.message if state.response else "",
                ),
                run_event_sink,
            )
        return state

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

    def run(self, request: UserRequest, event_sink: EventSink | None = None) -> AgentResponse:
        """Run the graph and return the final AgentResponse."""

        state = self.run_state(request, event_sink=event_sink)
        if state.response is not None:
            return state.response
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
