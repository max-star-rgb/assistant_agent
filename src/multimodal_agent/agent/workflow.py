"""Agent workflow compatibility wrapper."""

from time import perf_counter

from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.response_composer import compose_response, save_demo_memory
from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.agent.tool_input_builder import build_tool_input
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.run_history import RunHistoryStore
from multimodal_agent.services.tool_history import ToolHistoryStore
from multimodal_agent.tools.registry import ToolRegistry, create_default_registry


class AgentWorkflow:
    """Compatibility wrapper around the default graph runtime."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        intent_detector: IntentDetector | None = None,
        router: ToolRouter | None = None,
        run_history: RunHistoryStore | None = None,
        tool_history: ToolHistoryStore | None = None,
        use_graph_runtime: bool = True,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.intent_detector = intent_detector or IntentDetector()
        self.router = router or ToolRouter()
        self.run_history = run_history
        self.tool_history = tool_history
        self.tool_executor = ToolExecutor(registry=self.registry, tool_history=self.tool_history)
        self.use_graph_runtime = use_graph_runtime

    def run(self, request: UserRequest) -> AgentState:
        if self.use_graph_runtime:
            from multimodal_agent.agent.runtime import AgentGraphRuntime

            return AgentGraphRuntime(
                registry=self.registry,
                intent_detector=self.intent_detector,
                router=self.router,
                run_history=self.run_history,
                tool_history=self.tool_history,
            ).run_state(request)
        return self.run_legacy(request)

    def run_legacy(self, request: UserRequest) -> AgentState:
        """Run the pre-graph synchronous workflow for compatibility tests."""

        run_started_at = perf_counter()
        state = AgentState.from_request(request)
        if self.run_history is not None:
            self.run_history.record_start(state.run_id, state.user_id, state.session_id)

        intent = self.intent_detector.detect(request)
        state.set_intent(intent)
        plan = self.router.route(intent, request)
        state.set_plan(plan)
        state.selected_tools = self.router.select_tools(intent, request)

        outputs_by_step: dict[str, ToolResult] = {}
        for step in plan.steps:
            if step.tool_name is None:
                continue
            tool_input = build_tool_input(step.action, request, outputs_by_step)
            result = self.tool_executor.run_tool(state, step.step_id, step.tool_name, tool_input)
            if result.success:
                outputs_by_step[step.step_id] = result
            else:
                break

        if state.status != "failed":
            save_demo_memory(request, state, self.tool_executor)
            state.set_response(compose_response(state))

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
        return state
