from __future__ import annotations

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.models import ToolResult


class ProbeInput(BaseModel):
    value: str = Field(min_length=1)


class ProbeTool(ToolBase):
    name = "probe_tool"
    description = "probe-sentinel"
    input_schema = ProbeInput
    output_schema = ProbeInput
    category = "read"

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
        )


class ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


class CancelledToken:
    def is_cancelled(self) -> bool:
        return True


def offline_config() -> ProviderConfig:
    return ProviderConfig(langgraph_checkpointer_backend="none")
