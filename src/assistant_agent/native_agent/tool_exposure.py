"""Native middleware for deterministic Skill-governed Tool disclosure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from assistant_agent.skills.loading import SkillCatalog, SkillDescriptor
from assistant_agent.tools.ids import LOAD_SKILL_TOOL_NAME


class ProgressiveToolExposureMiddleware(AgentMiddleware):
    """Expose Skill-governed Tool schemas only after trusted Skill activation."""

    def __init__(self, catalog: SkillCatalog) -> None:
        super().__init__()
        descriptors = {
            descriptor.name: descriptor
            for descriptor in catalog.descriptors
            if _model_invocable(descriptor)
        }
        self._descriptors = descriptors
        self._claimed_tool_names = frozenset(
            tool_name
            for descriptor in descriptors.values()
            for tool_name in descriptor.governed_tools
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse | AIMessage],
    ) -> ModelResponse | AIMessage:
        return handler(self._request_with_visible_tools(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse | AIMessage]],
    ) -> ModelResponse | AIMessage:
        return await handler(self._request_with_visible_tools(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._activation_update(request, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command[Any]],
        ],
    ) -> ToolMessage | Command[Any]:
        return self._activation_update(request, await handler(request))

    def _request_with_visible_tools(self, request: ModelRequest) -> ModelRequest:
        active_skill_ids = _string_values(request.state.get("active_skill_ids"))
        granted_tool_names = {
            tool_name
            for skill_id in active_skill_ids
            if (descriptor := self._descriptors.get(skill_id)) is not None
            for tool_name in descriptor.governed_tools
        }
        visible_tools = [
            tool
            for tool in request.tools
            if not isinstance(tool, BaseTool)
            or tool.name not in self._claimed_tool_names
            or tool.name in granted_tool_names
        ]
        return request.override(tools=visible_tools)

    def _activation_update(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        if request.tool_call.get("name") != LOAD_SKILL_TOOL_NAME:
            return result
        if not isinstance(result, ToolMessage) or result.status == "error":
            return result
        artifact = result.artifact
        if not isinstance(artifact, Mapping) or artifact.get("status") != "succeeded":
            return result
        skill_id = artifact.get("skill_id")
        requested_skill_id = request.tool_call.get("args", {}).get("skill_id")
        if not isinstance(skill_id, str) or skill_id != requested_skill_id:
            return result
        descriptor = self._descriptors.get(skill_id)
        if descriptor is None:
            return result
        return Command(
            update={
                "messages": [result],
                "active_skill_ids": [skill_id],
                "skill_reference_grants": {
                    skill_id: sorted(descriptor.references),
                },
            }
        )


def discoverable_skill_descriptors(catalog: SkillCatalog) -> tuple[SkillDescriptor, ...]:
    """Return the trusted L0 index entries available to model-driven loading."""

    return tuple(
        descriptor
        for descriptor in catalog.descriptors
        if _model_invocable(descriptor) and descriptor.discoverable
    )


def _model_invocable(descriptor: SkillDescriptor) -> bool:
    return (
        descriptor.enabled
        and descriptor.activation == "model"
        and not descriptor.disable_model_invocation
    )


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


__all__ = [
    "ProgressiveToolExposureMiddleware",
    "discoverable_skill_descriptors",
]
