"""Narrow model-call projection for the planning Worker invocation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import BaseTool

from assistant_agent.native_agent.models import WorkerResultSchema
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)


class PlanningWorkerMiddleware(AgentMiddleware):
    """Turn the shared fast agent into a scoped planning Worker when requested."""

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
        if request.state.get("agent_phase", "fast") != "worker":
            return request.override(response_format=None)
        control_names = {LOAD_SKILL_TOOL_NAME, LOAD_SKILL_REFERENCE_TOOL_NAME}
        return request.override(
            tools=[
                tool for tool in request.tools if _tool_name(tool) not in control_names
            ],
            response_format=ToolStrategy(WorkerResultSchema),
            system_message=_worker_system_message(request),
        )


def _worker_system_message(request: ModelRequest) -> SystemMessage:
    guidance = (
        "你是 planning Worker，只执行当前私有用户消息中的一个 Todo。"
        "可调用当前可见业务工具；不要创建或修改 Todo，不要加载 Skill，不要回答其他任务。"
        "正常完成时提交 WorkerResult(status='succeeded')；业务上没有足够信息、没有结果或无法继续时"
        "提交 WorkerResult(status='blocked')。运行时异常不得伪装为 blocked。"
    )
    existing = request.system_message
    if existing is None:
        return SystemMessage(content=guidance)
    if isinstance(existing.content, str):
        return existing.model_copy(update={"content": f"{existing.content}\n\n{guidance}"})
    return existing.model_copy(
        update={"content": [*existing.content, {"type": "text", "text": guidance}]}
    )


def _tool_name(tool: BaseTool | dict[str, Any]) -> str | None:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function") if isinstance(tool, dict) else None
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    name = tool.get("name") if isinstance(tool, dict) else None
    return name if isinstance(name, str) else None


__all__ = ["PlanningWorkerMiddleware"]
