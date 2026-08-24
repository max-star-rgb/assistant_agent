from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool


class ScriptedSupervisor:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self._responses = list(responses)
        self.bound_tool_names: set[str] = set()
        self.calls = 0
        self.create_agent_calls = 0

    def bind_tools(self, tools: Sequence[BaseTool], **_kwargs: Any):
        self.bound_tool_names = {tool.name for tool in tools}
        return self

    def invoke(self, _messages: Sequence[object]) -> AIMessage:
        self.calls += 1
        if not self._responses:
            raise AssertionError("scripted supervisor responses exhausted")
        return self._responses.pop(0)

    async def ainvoke(self, messages: Sequence[object]) -> AIMessage:
        return self.invoke(messages)
