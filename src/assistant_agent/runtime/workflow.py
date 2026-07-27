"""Agent workflow compatibility wrapper."""

from time import perf_counter

from assistant_agent.runtime.intent import IntentDetector
from assistant_agent.runtime.response_composer import compose_response
from assistant_agent.runtime.router import ToolRouter
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_input_builder import build_tool_input
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.runtime.run_history import RunHistoryStore
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry


class AgentWorkflow:
    """Compatibility wrapper around the default graph runtime."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        intent_detector: IntentDetector | None = None,
        router: ToolRouter | None = None,
        run_history: RunHistoryStore | None = None,
        use_graph_runtime: bool = True,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.intent_detector = intent_detector or IntentDetector()
        self.router = router or ToolRouter()
        self.run_history = run_history
        self.tool_executor = ToolExecutor(registry=self.registry)
        self.use_graph_runtime = use_graph_runtime

    def run(self, request: UserRequest) -> AgentState:
        if self.use_graph_runtime:
            from assistant_agent.runtime.runtime import AgentGraphRuntime

            return AgentGraphRuntime(
                registry=self.registry,
                intent_detector=self.intent_detector,
                router=self.router,
                run_history=self.run_history,
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
