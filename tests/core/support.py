from __future__ import annotations

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from assistant_agent.config import AppConfig, RuntimeConfig
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.tools.native_boundary import (
    builtin_tool_metadata,
    native_content_and_artifact,
)


def create_probe_tool() -> BaseTool:
    @tool("probe_tool", response_format="content_and_artifact")
    def probe_tool(
        value: str,
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        """Return a deterministic offline probe result."""

        del runtime
        return native_content_and_artifact({"value": value}, {"value": value})

    probe_tool.metadata = builtin_tool_metadata()
    return probe_tool


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


def offline_config() -> AppConfig:
    return AppConfig(runtime=RuntimeConfig(langgraph_checkpointer_backend="none"))
