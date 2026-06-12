"""Default LangGraph runtime for agent execution."""

from time import perf_counter
from typing import Any

from multimodal_agent.agent.conditional_graph import build_conditional_agent_graph
from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.memory.factory import create_memory_store
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.api import api_error_from_agent_error
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.requests import AgentResponse, UserRequest
from multimodal_agent.services.event_sink import EventSink
from multimodal_agent.services.run_history import RunHistoryStore
from multimodal_agent.services.tool_history import ToolHistoryStore
from multimodal_agent.services.trace_store import InMemoryTraceStore, TraceStore
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
        tool_history: ToolHistoryStore | None = None,
        event_sink: EventSink | None = None,
        trace_store: TraceStore | None = None,
    ) -> None:
        self.config = config or ProviderConfig.from_env()
        self.registry = registry or create_default_registry(self.config)
        self.memory_store = memory_store or create_memory_store(self.config)
        self.intent_detector = intent_detector or IntentDetector()
        self.router = router or ToolRouter()
        self.run_history = run_history
        self.tool_history = tool_history
        self.event_sink = event_sink
        self.trace_store = trace_store or InMemoryTraceStore()
        self.tool_executor = ToolExecutor(registry=self.registry, tool_history=self.tool_history, event_sink=self.event_sink)
        self._graph = build_conditional_agent_graph()

    def run_state(self, request: UserRequest) -> AgentState:
        """Run the graph and return the full state for compatibility callers."""

        state = AgentState.from_request(request)
        run_started_at = perf_counter()
        self._emit(
            AgentEvent(
                type="task_started",
                session_id=state.session_id,
                run_id=state.run_id,
                payload={"user_id": state.user_id},
            )
        )
        if self.run_history is not None:
            self.run_history.record_start(state.run_id, state.user_id, state.session_id)

        initial_state = {
            "request": request,
            "state": state,
            "intent_detector": self.intent_detector,
            "router": self.router,
            "tool_executor": self.tool_executor,
            "memory_store": self.memory_store,
            "outputs_by_step": {},
            "current_step_index": 0,
            "trace_id": state.trace_id,
            "trace_store": self.trace_store,
        }
        self._emit(AgentEvent(type="graph_node_started", session_id=state.session_id, run_id=state.run_id, node_name="agent_graph"))
        final_state = self._graph.invoke(initial_state)
        state = final_state["state"]
        self._emit(AgentEvent(type="graph_node_finished", session_id=state.session_id, run_id=state.run_id, node_name="agent_graph"))
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
                )
            )
        else:
            self._emit(
                AgentEvent(
                    type="final_response",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    text=state.response.message if state.response else "",
                )
            )
        return state

    def run(self, request: UserRequest) -> AgentResponse:
        """Run the graph and return the final AgentResponse."""

        state = self.run_state(request)
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

    def _emit(self, event: AgentEvent) -> None:
        if self.event_sink is not None:
            self.event_sink.emit(event)
