"""Request projection for snapshot-bound coding analysis model calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage

from assistant_agent.coding.analysis import CodingAnalysisResponse


class CodingAnalysisPhaseMiddleware(AgentMiddleware):
    """Enforce the fixed no-search request projection for analysis calls."""

    def __init__(self, model_settings: Mapping[str, Any] | None = None) -> None:
        self._model_settings = dict(model_settings or {})

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
        if request.state.get("provider_search_profile") != "none":
            raise ValueError("coding_analysis_search_profile_invalid")
        model_settings = dict(request.model_settings or {})
        model_settings.update(self._model_settings)
        return request.override(model_settings=model_settings)


def coding_analysis_response_format() -> ToolStrategy:
    """Return the public structured-output strategy for analysis workers."""

    return ToolStrategy(CodingAnalysisResponse)


__all__ = [
    "CodingAnalysisPhaseMiddleware",
    "coding_analysis_response_format",
]
