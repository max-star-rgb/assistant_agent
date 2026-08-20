"""Phase-specific public create_agent projections for the planning experiment."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import BaseTool

from assistant_agent.native_agent.models import NativePlanProposal


_CONTROL_TOOL_NAMES = frozenset({"load_skill", "load_skill_reference"})


class PlanningPhaseMiddleware(AgentMiddleware):
    """Project one compiled agent into its planner or finalizer phase."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse | AIMessage],
    ) -> ModelResponse | AIMessage:
        return handler(self._project(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse | AIMessage]],
    ) -> ModelResponse | AIMessage:
        return await handler(self._project(request))

    def _project(self, request: ModelRequest) -> ModelRequest:
        phase = request.state.get("agent_phase", "fast")
        if phase == "planner":
            active_skill_ids = request.state.get("active_skill_ids", ())
            return request.override(
                tools=[
                    tool
                    for tool in request.tools
                    if _tool_name(tool) in _CONTROL_TOOL_NAMES
                ],
                response_format=(
                    planner_response_format() if active_skill_ids else None
                ),
                system_message=_phase_system_message(
                    request, planner_system_prompt()
                ),
            )
        if phase == "finalizer":
            return request.override(
                tools=[],
                response_format=None,
                system_message=_phase_system_message(
                    request, finalizer_system_prompt()
                ),
            )
        if phase == "worker":
            allowed_names = _worker_tool_allowlist(request)
            model_settings = dict(request.model_settings or {})
            model_settings["provider_search_profile"] = request.state.get(
                "provider_search_profile", "none"
            )
            tools = (
                request.tools
                if not allowed_names
                else [
                    tool
                    for tool in request.tools
                    if _tool_name(tool) in allowed_names
                ]
            )
            return request.override(
                tools=tools,
                response_format=None,
                model_settings=model_settings,
            )
        return request.override(response_format=None)


def planner_response_format() -> ToolStrategy:
    """Return the public structured-output strategy for a native plan proposal."""

    return ToolStrategy(NativePlanProposal)


def planner_system_prompt() -> str:
    """Constrain the planner role without creating a separate agent loop."""

    return (
        "你是任务规划器。需要专业流程时先加载对应 Skill；"
        "随后只提交符合 NativePlanProposal schema 的最小可执行 native_plan_v1，"
        "不直接回答用户，也不执行业务工具。"
    )


def finalizer_system_prompt() -> str:
    """Constrain the finalizer role after worker work has completed."""

    return "你是结果整合器。只根据已提供的 worker 结果回答用户，不调用工具。"


def _phase_system_message(request: ModelRequest, phase_prompt: str) -> SystemMessage:
    existing = request.system_message
    if existing is None:
        return SystemMessage(content=phase_prompt)
    if isinstance(existing.content, str):
        return existing.model_copy(
            update={"content": f"{existing.content}\n\n{phase_prompt}"}
        )
    return existing.model_copy(
        update={
            "content": [
                *existing.content,
                {"type": "text", "text": phase_prompt},
            ]
        }
    )


def _tool_name(tool: BaseTool | dict[str, Any]) -> str | None:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function") if isinstance(tool, dict) else None
    if isinstance(function, dict):
        name = function.get("name")
        return name if isinstance(name, str) else None
    name = tool.get("name") if isinstance(tool, dict) else None
    return name if isinstance(name, str) else None


def _worker_tool_allowlist(request: ModelRequest) -> frozenset[str]:
    """Fail closed when a planning worker has no trusted Tool scope."""

    raw_allowlist = request.state.get("worker_tool_allowlist", ())
    if not isinstance(raw_allowlist, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        tool_name
        for tool_name in raw_allowlist
        if isinstance(tool_name, str) and tool_name
    )


__all__ = [
    "PlanningPhaseMiddleware",
    "finalizer_system_prompt",
    "planner_response_format",
    "planner_system_prompt",
]
