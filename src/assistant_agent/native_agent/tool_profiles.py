"""Generic create_agent middleware for explicit Tool Profile activation."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Annotated, Any, Literal, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelResponse,
    PrivateStateAttr,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)

from assistant_agent.tools.native_boundary import builtin_tool_metadata


ACTIVATE_TOOL_PROFILE_TOOL_NAME = "activate_tool_profile"
DEACTIVATE_TOOL_PROFILE_TOOL_NAME = "deactivate_tool_profile"
_DEACTIVATE_PROFILE_ID_KEY = "__deactivate_tool_profile_id__"


def _merge_unique_profile_ids(
    left: list[str],
    right: list[str] | dict[str, str],
) -> list[str]:
    if isinstance(right, dict):
        profile_id = right.get(_DEACTIVATE_PROFILE_ID_KEY)
        if len(right) != 1 or not isinstance(profile_id, str) or not profile_id:
            raise TypeError("invalid tool profile state update")
        return [candidate for candidate in left if candidate != profile_id]
    return list(dict.fromkeys([*left, *right]))


class ToolProfileState(AgentState):
    """Run-local state owned by ``ToolProfileMiddleware``."""

    active_tool_profile_ids: NotRequired[
        Annotated[list[str], PrivateStateAttr, _merge_unique_profile_ids]
    ]


class ToolProfile(BaseModel):
    """Trusted static mapping from one profile ID to registered Tool names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    description: str = Field(min_length=1, max_length=500)
    tool_names: tuple[str, ...] = Field(min_length=1)

    @field_validator("tool_names")
    @classmethod
    def _validate_tool_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(name.strip() for name in value)
        if any(not name for name in normalized):
            raise ValueError("tool profile names must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("tool profile names must be unique")
        return normalized


class _ToolProfileCatalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    profiles: tuple[ToolProfile, ...]

    @model_validator(mode="after")
    def _validate_profiles(self) -> "_ToolProfileCatalog":
        profile_ids = [profile.profile_id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("tool profile IDs must be unique")
        owners: dict[str, str] = {}
        for profile in self.profiles:
            for tool_name in profile.tool_names:
                previous = owners.setdefault(tool_name, profile.profile_id)
                if previous != profile.profile_id:
                    raise ValueError(
                        f"tool {tool_name!r} belongs to multiple profiles"
                    )
        return self


class ToolProfileMiddleware(AgentMiddleware[ToolProfileState, Any]):
    """Add explicit profile activation and filter pre-registered Tools by state."""

    state_schema = ToolProfileState

    def __init__(
        self,
        profiles: Sequence[ToolProfile],
        *,
        available_tool_names: Collection[str],
    ) -> None:
        super().__init__()
        available = frozenset(available_tool_names)
        configured = _ToolProfileCatalog(profiles=tuple(profiles))
        catalog = _ToolProfileCatalog(
            profiles=tuple(
                profile.model_copy(update={"tool_names": tool_names})
                for profile in configured.profiles
                if (
                    tool_names := tuple(
                        name for name in profile.tool_names if name in available
                    )
                )
            )
        )
        self._profiles_by_id = {
            profile.profile_id: profile for profile in catalog.profiles
        }
        self._profile_id_by_tool_name = {
            tool_name: profile.profile_id
            for profile in catalog.profiles
            for tool_name in profile.tool_names
        }
        self._claimed_tool_names = frozenset(
            self._profile_id_by_tool_name
        )
        self.tools = (
            [self._create_activate_tool(), self._create_deactivate_tool()]
            if catalog.profiles
            else []
        )

    @property
    def profiles(self) -> tuple[ToolProfile, ...]:
        """Return the trusted profile index in deterministic order."""

        return tuple(self._profiles_by_id.values())

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
        return self._blocked_tool_message(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command[Any]],
        ],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked_tool_message(request)
        return blocked if blocked is not None else await handler(request)

    def _request_with_visible_tools(self, request: ModelRequest) -> ModelRequest:
        active_profile_ids = self._active_profile_ids(request.state)
        active_tool_names = {
            tool_name
            for profile_id in active_profile_ids
            for tool_name in self._tool_names_for_profile(profile_id)
        }
        visible_tools = [
            candidate
            for candidate in request.tools
            if not isinstance(candidate, BaseTool)
            or (
                (
                    candidate.name != DEACTIVATE_TOOL_PROFILE_TOOL_NAME
                    or bool(active_profile_ids)
                )
                and (
                    candidate.name not in self._claimed_tool_names
                    or candidate.name in active_tool_names
                )
            )
        ]
        return request.override(tools=visible_tools)

    def _active_profile_ids(self, state: object) -> tuple[str, ...]:
        if not isinstance(state, Mapping):
            return ()
        return tuple(
            profile_id
            for profile_id in _string_values(
                state.get("active_tool_profile_ids")
            )
            if profile_id in self._profiles_by_id
        )

    def _tool_names_for_profile(self, profile_id: str) -> tuple[str, ...]:
        profile = self._profiles_by_id.get(profile_id)
        return profile.tool_names if profile is not None else ()

    def _blocked_tool_message(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = request.tool_call["name"]
        profile_id = self._profile_id_by_tool_name.get(tool_name)
        active_profile_ids = self._active_profile_ids(request.state)
        if profile_id is None or profile_id in active_profile_ids:
            return None
        observation = {
            "status": "failed",
            "error": "tool_profile_not_active",
            "summary": (
                f"工具 {tool_name!r} 尚未开放；请先调用 "
                f"{ACTIVATE_TOOL_PROFILE_TOOL_NAME} 激活 {profile_id!r}。"
            ),
            "required_profile_id": profile_id,
        }
        return ToolMessage(
            content=json.dumps(observation, ensure_ascii=False, sort_keys=True),
            artifact=observation,
            name=tool_name,
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def _create_activate_tool(self) -> BaseTool:
        profile_index = "\n".join(
            f"- {profile.profile_id}: {profile.description}"
            for profile in self.profiles
        ) or "- 当前没有可加载的 Tool Profile。"
        profiles_by_id = self._profiles_by_id
        profile_id_type = (
            Literal.__getitem__(tuple(profiles_by_id)) if profiles_by_id else str
        )
        args_schema = create_model(
            "ActivateToolProfileInput",
            profile_id=(
                profile_id_type,
                Field(description="从当前可用 Tool Profile 中选择要加载的场景。"),
            ),
        )

        @tool(
            ACTIVATE_TOOL_PROFILE_TOOL_NAME,
            args_schema=args_schema,
            description=(
                "实现用户需求时，如果发现缺少相关工具，运行本工具以获取额外的工具集。\n"
                "在多数场景下都需要使用本工具，但注意按需要加载，不要刻意说明‘正在加载工具’等文本\n\n"
                "当前可用 工具集：\n"
                f"{profile_index}"
            ),
        )
        def activate_tool_profile(
            profile_id: str,
            runtime: ToolRuntime[Any],
        ) -> Command:
            profile = profiles_by_id.get(profile_id)
            if profile is None:
                raise ToolException("tool_profile_not_found")
            observation = {
                "status": "succeeded",
                "summary": "Tool Profile 已激活。",
                "profile_id": profile.profile_id,
                "activated_tool_names": list(profile.tool_names),
            }
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=json.dumps(
                                observation,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            artifact={
                                **observation,
                                "tool_count": len(profile.tool_names),
                            },
                            name=ACTIVATE_TOOL_PROFILE_TOOL_NAME,
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                    "active_tool_profile_ids": [profile.profile_id],
                }
            )

        activate_tool_profile.metadata = {
            **builtin_tool_metadata(),
            "retryable": False,
        }
        return activate_tool_profile

    def _create_deactivate_tool(self) -> BaseTool:
        profiles_by_id = self._profiles_by_id
        profile_id_type = (
            Literal.__getitem__(tuple(profiles_by_id)) if profiles_by_id else str
        )
        args_schema = create_model(
            "DeactivateToolProfileInput",
            profile_id=(
                profile_id_type,
                Field(description="选择不再需要暴露的 Tool Profile。"),
            ),
        )

        @tool(
            DEACTIVATE_TOOL_PROFILE_TOOL_NAME,
            args_schema=args_schema,
            description=(
                "当前任务不再需要某个已激活的工具集时，调用本工具隐藏该工具集。\n"
                "是否调用由你根据任务进展自主决定。"
            ),
        )
        def deactivate_tool_profile(
            profile_id: str,
            runtime: ToolRuntime[Any],
        ) -> Command:
            profile = profiles_by_id.get(profile_id)
            if profile is None:
                raise ToolException("tool_profile_not_found")
            active_profile_ids = self._active_profile_ids(runtime.state)
            already_inactive = profile.profile_id not in active_profile_ids
            deactivated_tool_names = (
                [] if already_inactive else list(profile.tool_names)
            )
            observation = {
                "status": "succeeded",
                "summary": (
                    "Tool Profile 已处于未激活状态。"
                    if already_inactive
                    else "Tool Profile 已取消激活。"
                ),
                "profile_id": profile.profile_id,
                "deactivated_tool_names": deactivated_tool_names,
                "already_inactive": already_inactive,
            }
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=json.dumps(
                                observation,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            artifact={
                                **observation,
                                "tool_count": len(deactivated_tool_names),
                            },
                            name=DEACTIVATE_TOOL_PROFILE_TOOL_NAME,
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                    "active_tool_profile_ids": {
                        _DEACTIVATE_PROFILE_ID_KEY: profile.profile_id
                    },
                }
            )

        deactivate_tool_profile.metadata = {
            **builtin_tool_metadata(),
            "retryable": False,
        }
        return deactivate_tool_profile


def project_tool_profiles() -> tuple[ToolProfile, ...]:
    """Return the repository-owned business Tool Profile catalog."""

    return (
        ToolProfile(
            profile_id="filesystem",
            description="读取、搜索、创建、编辑或删除当前仓库中的文件和目录。",
            tool_names=(
                "ls",
                "write_file",
                "edit_file",
                "delete",
                "glob",
                "grep",
                "execute",
            ),
        ),
        ToolProfile(
            profile_id="git",
            description="在目标路径所属的 Git 仓库中执行版本控制命令。",
            tool_names=("git",),
        ),
        ToolProfile(
            profile_id="browser",
            description="通过 Playwright 浏览网页、读取页面状态并执行受审批的页面交互。",
            tool_names=(
                "mcp_playwright_browser_click",
                "mcp_playwright_browser_close",
                "mcp_playwright_browser_console_messages",
                "mcp_playwright_browser_drag",
                "mcp_playwright_browser_drop",
                "mcp_playwright_browser_evaluate",
                "mcp_playwright_browser_file_upload",
                "mcp_playwright_browser_fill_form",
                "mcp_playwright_browser_find",
                "mcp_playwright_browser_handle_dialog",
                "mcp_playwright_browser_hover",
                "mcp_playwright_browser_navigate",
                "mcp_playwright_browser_navigate_back",
                "mcp_playwright_browser_network_request",
                "mcp_playwright_browser_network_requests",
                "mcp_playwright_browser_press_key",
                "mcp_playwright_browser_resize",
                "mcp_playwright_browser_run_code_unsafe",
                "mcp_playwright_browser_select_option",
                "mcp_playwright_browser_snapshot",
                "mcp_playwright_browser_tabs",
                "mcp_playwright_browser_take_screenshot",
                "mcp_playwright_browser_type",
                "mcp_playwright_browser_wait_for",
            ),
        ),
        ToolProfile(
            profile_id="travel",
            description="酒店、地点发现、周边搜索以及步行、骑行、公交、高铁和驾车路线规划。",
            tool_names=(
                "lodging_search",
                "mcp_amap_maps_maps_geo",
                "mcp_amap_maps_maps_bicycling",
                "mcp_amap_maps_maps_direction_walking",
                "mcp_amap_maps_maps_direction_driving",
                "mcp_amap_maps_maps_direction_transit_integrated",
                "mcp_amap_maps_maps_text_search",
                "mcp_amap_maps_maps_around_search",
            ),
        ),
        ToolProfile(
            profile_id="visual-creation",
            description="图片生成，以及把本轮生成图片和已知的本地生成图片 ID 转换为 3D 模型。",
            tool_names=("image_generation", "image_to_3d"),
        ),
        ToolProfile(
            profile_id="workspace-communications",
            description="邮件、日历和联系人查询，以及日历事件创建。",
            tool_names=(
                "email_search",
                "email_read",
                "mcp_google_gmail_readonly_search_gmail_messages",
                "mcp_google_gmail_readonly_get_gmail_messages_content_batch",
                "calendar_search",
                "calendar_create",
                "contacts_search",
            ),
        ),
    )


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


__all__ = [
    "ACTIVATE_TOOL_PROFILE_TOOL_NAME",
    "DEACTIVATE_TOOL_PROFILE_TOOL_NAME",
    "ToolProfile",
    "ToolProfileMiddleware",
    "ToolProfileState",
    "project_tool_profiles",
]
