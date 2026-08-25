"""Native Agent Skills driven Tool disclosure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from deepagents.middleware.skills import SkillMetadata
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool


class NativeSkillToolExposureMiddleware(AgentMiddleware):
    """Expose repo-owned Tool schemas only after their native Skill is loaded."""

    def __init__(self, skills: Sequence[SkillMetadata]) -> None:
        super().__init__()
        self._allowed_tools_by_skill = {
            skill["name"]: frozenset(skill["allowed_tools"])
            for skill in skills
        }
        self._claimed_tool_names = frozenset(
            tool_name
            for allowed_tools in self._allowed_tools_by_skill.values()
            for tool_name in allowed_tools
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

    def _request_with_visible_tools(self, request: ModelRequest) -> ModelRequest:
        active_skill_ids = _string_values(request.state.get("active_skill_ids"))
        granted_tool_names = {
            tool_name
            for skill_id in active_skill_ids
            for tool_name in self._allowed_tools_by_skill.get(skill_id, ())
        }
        visible_tools = [
            tool
            for tool in request.tools
            if not isinstance(tool, BaseTool)
            or tool.name not in self._claimed_tool_names
            or tool.name in granted_tool_names
        ]
        return request.override(tools=visible_tools)


def discoverable_skill_metadata(
    skills: Sequence[SkillMetadata],
) -> tuple[SkillMetadata, ...]:
    """Return the validated native metadata advertised in the L0 index."""

    return tuple(skills)


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


__all__ = [
    "NativeSkillToolExposureMiddleware",
    "discoverable_skill_metadata",
]
