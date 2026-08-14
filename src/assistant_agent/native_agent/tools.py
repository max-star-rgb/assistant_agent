"""Static LangChain Tool and official MCP assembly for the native graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.prebuilt import ToolRuntime
from pydantic import ConfigDict, create_model

from assistant_agent.config import ProviderConfig
from assistant_agent.mcp.config import (
    MCPServerConfig,
    resolve_mcp_server_env,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.base import Tool, ToolContext
from assistant_agent.tools.input_binding import (
    RuntimeInputBinding,
    llm_forbidden_input_fields,
    validate_tool_input_contract,
)
from assistant_agent.tools.plugins.contracts import ToolPluginContext


@dataclass(frozen=True)
class NativeToolResources:
    """Optional process resources consumed by explicitly installed built-ins."""

    video_context_store: Any | None = None
    realtime_video_memory_store: Any | None = None
    durable_task_service: Any | None = None
    calendar_adapter: Any | None = None
    embedding_coordinator_store: Any | None = None
    visual_semantic_store_pool: Any | None = None
    visual_reminder_registry: Any | None = None
    visual_memory_text_index: Any | None = None


def create_native_tools(
    config: ProviderConfig,
    *,
    resources: NativeToolResources,
) -> list[BaseTool]:
    """Build the trusted in-process inventory without Registry or discovery."""

    context = ToolPluginContext(
        config=config,
        mcp_server_configs=[],
        video_context_store=resources.video_context_store,
        realtime_video_memory_store=resources.realtime_video_memory_store,
        durable_task_service=resources.durable_task_service,
        calendar_adapter=resources.calendar_adapter,
        embedding_coordinator_store=resources.embedding_coordinator_store,
        visual_semantic_store_pool=resources.visual_semantic_store_pool,
        visual_reminder_registry=resources.visual_reminder_registry,
        visual_memory_text_index=resources.visual_memory_text_index,
    )
    concrete_tools: list[Tool] = []
    for plugin in _builtin_plugins():
        concrete_tools.extend(plugin.build_tools(context))
    names = [tool.name for tool in concrete_tools]
    if len(names) != len(set(names)):
        raise ValueError("native tool names must be unique")
    return [to_langchain_tool(tool) for tool in sorted(concrete_tools, key=lambda x: x.name)]


def to_langchain_tool(tool: Tool) -> StructuredTool:
    """Adapt one concrete Tool to ToolRuntime and ToolMessage conventions."""

    validate_tool_input_contract(tool)
    args_schema = _visible_input_model(tool)

    async def invoke_native_tool(
        runtime: ToolRuntime[AssistantRunContext],
        **payload: Any,
    ) -> tuple[str, dict[str, Any]]:
        bound_input = _bind_native_input(tool, payload, runtime)
        result = await asyncio.to_thread(
            tool.run,
            bound_input,
            _tool_context(runtime),
        )
        if not result.success:
            raise ToolException(result.error or f"{tool.name} failed")
        observation = result.model_observation
        if observation is None:
            observation = result.data or {"status": "succeeded"}
        return (
            json.dumps(observation, ensure_ascii=False, sort_keys=True),
            dict(result.data or {}),
        )

    return StructuredTool.from_function(
        coroutine=invoke_native_tool,
        name=tool.name,
        description=tool.description,
        args_schema=args_schema,
        response_format="content_and_artifact",
        metadata={
            "effect": getattr(tool, "category", "dangerous"),
            "source": "builtin",
        },
    )


def mcp_connections(
    server_configs: Sequence[MCPServerConfig],
) -> dict[str, dict[str, Any]]:
    """Translate trusted stdio config to the official adapter schema."""

    connections: dict[str, dict[str, Any]] = {}
    for server in server_configs:
        command, *args = server.command
        connection: dict[str, Any] = {
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": resolve_mcp_server_env(server.env),
        }
        if server.cwd is not None:
            connection["cwd"] = server.cwd
        connections[server.server_name] = connection
    return connections


async def create_mcp_tools(
    server_configs: Sequence[MCPServerConfig],
    *,
    client_factory: Callable[..., Any] | None = None,
) -> list[BaseTool]:
    """Load allowlisted MCP tools through MultiServerMCPClient."""

    if not server_configs:
        return []
    if client_factory is None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client_factory = MultiServerMCPClient
    client = client_factory(mcp_connections(server_configs), tool_name_prefix=False)
    assembled: list[BaseTool] = []
    for server in server_configs:
        discovered = await client.get_tools(server_name=server.server_name)
        allowed = set(server.allowed_tools)
        for tool in discovered:
            if tool.name not in allowed:
                continue
            name = f"{server.namespace_prefix}_{server.server_name}_{tool.name}"
            assembled.append(
                tool.model_copy(
                    update={
                        "name": name,
                        "metadata": {
                            **(tool.metadata or {}),
                            "effect": (
                                "read"
                                if tool.name in set(server.read_only_tools)
                                else "dangerous"
                            ),
                            "source": "mcp",
                            "mcp_server": server.server_name,
                        },
                    }
                )
            )
    names = [tool.name for tool in assembled]
    if len(names) != len(set(names)):
        raise ValueError("namespaced MCP tool names must be unique")
    return assembled


def _visible_input_model(tool: Tool):
    forbidden = set(llm_forbidden_input_fields(tool))
    fields = {
        name: (field.annotation, field)
        for name, field in tool.input_schema.model_fields.items()
        if name not in forbidden
    }
    # ToolRuntime belongs to the execution schema so ToolNode can inject it.
    # LangChain removes this annotated field from ``tool_call_schema``, which is
    # the schema exposed to the model.
    fields["runtime"] = (ToolRuntime[AssistantRunContext], ...)
    return create_model(
        f"{type(tool).__name__}NativeInput",
        __config__=ConfigDict(
            arbitrary_types_allowed=True,
            extra="forbid",
            strict=True,
        ),
        **fields,
    )


def _bind_native_input(
    tool: Tool,
    payload: Mapping[str, Any],
    runtime: ToolRuntime[AssistantRunContext],
) -> dict[str, Any]:
    bound = dict(payload)
    for raw in getattr(tool, "runtime_input_bindings", ()):
        binding = (
            raw
            if isinstance(raw, RuntimeInputBinding)
            else RuntimeInputBinding.model_validate(raw)
        )
        value = _binding_value(binding, runtime)
        if value is not _MISSING:
            bound[binding.field] = value
    return bound


def _binding_value(
    binding: RuntimeInputBinding,
    runtime: ToolRuntime[AssistantRunContext],
) -> Any:
    context = runtime.context
    execution = runtime.execution_info
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    if binding.source == "runtime_identity":
        identity = {
            "user_id": context.user_id,
            "tenant_id": context.tenant_id,
            "session_id": getattr(execution, "thread_id", None),
            "run_id": getattr(execution, "run_id", None),
        }
        return identity.get(binding.key or "", _MISSING)
    if binding.source == "memory_context":
        memories = tuple(state.get("memory_context", ()))
        if binding.key == "summaries":
            return list(memories)
        if binding.key == "text":
            return "\n".join(memories)
    if binding.source == "request":
        request = _latest_human_request(state)
        return request.get(binding.key or "", _MISSING)
    if binding.source == "durable_idempotency":
        thread_id = getattr(execution, "thread_id", "") or "thread"
        return f"native:{thread_id}:{runtime.tool_call_id or 'tool-call'}"
    return _MISSING


def _latest_human_request(state: Mapping[str, Any]) -> dict[str, Any]:
    for message in reversed(state.get("messages", ())):
        if not isinstance(message, HumanMessage):
            continue
        result: dict[str, Any] = {"image_ids": [], "video_ids": []}
        if isinstance(message.content, str):
            result["text"] = message.content
            return result
        texts: list[str] = []
        for block in message.content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block_type in {"image", "image_url"} and block.get("id"):
                result["image_ids"].append(str(block["id"]))
            elif block_type in {"video", "file"} and block.get("id"):
                result["video_ids"].append(str(block["id"]))
        result["text"] = "\n".join(texts)
        return result
    return {}


def _tool_context(runtime: ToolRuntime[AssistantRunContext]) -> ToolContext:
    execution = runtime.execution_info
    return ToolContext(
        user_id=runtime.context.user_id,
        session_id=getattr(execution, "thread_id", None),
        run_id=getattr(execution, "run_id", None),
        metadata={
            "tenant_id": runtime.context.tenant_id,
            "entry_profile": runtime.context.entry_profile,
        },
    )


def _builtin_plugins() -> tuple[Any, ...]:
    """Return an explicit list; no filesystem or configured-module discovery."""

    from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.plugin import (
        CalendarContactsPlugin,
    )
    from assistant_agent.tools.plugins.builtin.email_access.plugin import EmailAccessPlugin
    from assistant_agent.tools.plugins.builtin.image_generation.plugin import (
        ImageGenerationToolPlugin,
    )
    from assistant_agent.tools.plugins.builtin.image_to_3d.plugin import ImageTo3DToolPlugin
    from assistant_agent.tools.plugins.builtin.local_file_access.plugin import LocalFileAccessPlugin
    from assistant_agent.tools.plugins.builtin.lodging.plugin import LodgingToolPlugin
    from assistant_agent.tools.plugins.builtin.media_inspection.plugin import MediaInspectionPlugin
    from assistant_agent.tools.plugins.builtin.python_execution.plugin import PythonExecutionPlugin
    from assistant_agent.tools.plugins.builtin.shopping.plugin import ShoppingToolPlugin
    from assistant_agent.tools.plugins.builtin.skill_loading.plugin import SkillLoadingPlugin
    from assistant_agent.tools.plugins.builtin.visual_image_search.plugin import (
        VisualImageSearchPlugin,
    )
    from assistant_agent.tools.plugins.builtin.website_guidance.plugin import WebsiteGuidancePlugin

    return (
        EmailAccessPlugin(),
        LocalFileAccessPlugin(),
        SkillLoadingPlugin(),
        LodgingToolPlugin(),
        PythonExecutionPlugin(),
        MediaInspectionPlugin(),
        VisualImageSearchPlugin(),
        WebsiteGuidancePlugin(),
        ShoppingToolPlugin(),
        CalendarContactsPlugin(),
        ImageGenerationToolPlugin(),
        ImageTo3DToolPlugin(),
    )


_MISSING = object()


__all__ = [
    "NativeToolResources",
    "create_mcp_tools",
    "create_native_tools",
    "mcp_connections",
    "to_langchain_tool",
]
