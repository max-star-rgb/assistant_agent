"""Recover unknown tool calls using the model's actual exposed tool surface."""

from __future__ import annotations

import json
from typing import Annotated, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command


class _ExposedToolState(AgentState):
    exposed_tool_names: NotRequired[Annotated[list[str], PrivateStateAttr]]


class UnknownToolFeedbackMiddleware(AgentMiddleware):
    """Place after tool filters to snapshot local tool names per model step."""

    state_schema = _ExposedToolState

    def wrap_model_call(self, request, handler):
        return self._with_exposed_tools(request, handler(request))

    async def awrap_model_call(self, request, handler):
        return self._with_exposed_tools(request, await handler(request))

    def _with_exposed_tools(self, request, response):
        names = [tool.name for tool in request.tools if isinstance(tool, BaseTool)]
        if isinstance(response, AIMessage):
            response = ModelResponse(result=[response])
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={"exposed_tool_names": names}),
        )

    def wrap_tool_call(self, request, handler):
        return self._unknown_tool_message(request) or handler(request)

    async def awrap_tool_call(self, request, handler):
        error = self._unknown_tool_message(request)
        return error if error is not None else await handler(request)

    def _unknown_tool_message(self, request):
        if request.tool is not None:
            return None
        observation = {
            "status": "failed",
            "error": "unknown_tool",
            "summary": "该工具不存在，请仅调用 available_tool_names 中已暴露的工具。",
            "available_tool_names": request.state.get("exposed_tool_names", []),
        }
        return ToolMessage(
            content=json.dumps(observation, ensure_ascii=False),
            artifact=observation,
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )
