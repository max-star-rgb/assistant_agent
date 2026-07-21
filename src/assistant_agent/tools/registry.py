"""Tool registry and default mock tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Dict

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tools import (
    ToolExecutionPolicy,
    ToolPolicyMetadata,
    ToolResult,
    ToolSideEffectPolicy,
    ToolSpec,
    VisibilityPolicy,
)
from assistant_agent.tools.base import BaseTool, ToolContext
from assistant_agent.tools.agent_delegation_tool import AgentDelegationTool
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from assistant_agent.tools.memory_media_tool import MemoryIngestStatusTool, MemoryMediaIngestTool
from assistant_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool
from assistant_agent.tools.personal_assistant_tools import (
    CalendarCreateTool,
    CalendarSearchTool,
    ContactsSearchTool,
    ReminderCreateTool,
    WeatherTool,
)
from assistant_agent.tools.python_interpreter_tool import PythonInterpreterTool
from assistant_agent.tools.render_tool import Render3DTool
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool
from assistant_agent.tools.tool_search_tool import ToolSearchTool
from assistant_agent.services.web_search_adapter import create_web_search_adapter
from assistant_agent.services.web_fetch_adapter import create_web_fetch_adapter
from assistant_agent.services.image_generation_adapter import create_image_generation_adapter
from assistant_agent.services.memory_media_ingestion import create_memory_media_ingestion_service
from assistant_agent.services.personal_assistant_mcp_adapters import create_personal_assistant_adapter_bundle
from assistant_agent.services.product_adapter import create_shopping_compare_adapter, create_shopping_search_adapter
from assistant_agent.services.render_adapter import create_render_adapter
from assistant_agent.services.tool_manifest import (
    CALENDAR_CREATE_TOOL_NAME,
    CALENDAR_SEARCH_TOOL_NAME,
    CONTACTS_SEARCH_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    MEMORY_INGEST_STATUS_TOOL_NAME,
    MEMORY_MEDIA_INGEST_TOOL_NAME,
    MEMORY_RETRIEVAL_TOOL_NAME,
    MEMORY_SAVE_TOOL_NAME,
    PYTHON_INTERPRETER_TOOL_NAME,
    REMINDER_CREATE_TOOL_NAME,
    RENDER_3D_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
    TOOL_SEARCH_TOOL_NAME,
    VIDEO_UNDERSTANDING_TOOL_NAME,
    VISUAL_IMAGE_SEARCH_TOOL_NAME,
    WEATHER_TOOL_NAME,
    WEB_FETCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
)
from assistant_agent.services.tool_visual_image_search_adapter import create_visual_image_search_adapter
from assistant_agent.services.vision_client import (
    create_realtime_vision_understanding_client,
    create_vision_understanding_client,
)
from assistant_agent.services.video_context import VideoContextStore
from assistant_agent.services.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.tools.video_tool import VideoUnderstandingTool
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
from assistant_agent.tools.visual_image_search_tool import VisualImageSearchTool
from assistant_agent.tools.web_search_tool import WebSearchTool
from assistant_agent.tools.web_fetch_tool import WebFetchTool
from assistant_agent.tools.task_plan_tool import TaskPlanSubmitTool

if TYPE_CHECKING:
    from assistant_agent.mcp.config import MCPServerConfig
    from assistant_agent.mcp.registration import MCPToolDiscoveryRunner
    from assistant_agent.services.agent_communication import AgentCommunicationService
    from assistant_agent.services.durable_tasks.service import DurableTaskService


class ToolRegistry:
    """In-memory registry for tool lookup and execution."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tool_spec(tool)
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool not registered: {name}") from exc

    def list(self) -> list[str]:
        return sorted(self._tools)

    def run(
        self,
        name: str,
        input: dict[str, Any],
        context: ToolContext | None = None,
    ) -> ToolResult:
        return self.get(name).run(input, context)

    def get_spec(self, name: str) -> ToolSpec:
        """Return the provider-neutral contract for one registered tool."""

        return self._tool_spec(self.get(name))

    def list_specs(self) -> list[ToolSpec]:
        """Return provider-neutral specs for all registered tools."""

        return [self._tool_spec(self._tools[name]) for name in sorted(self._tools)]

    @staticmethod
    def _tool_spec(tool: BaseTool) -> ToolSpec:
        usage = _ACTION_USAGE.get(tool.name, {})
        policy = tool_policy_metadata(tool)
        execution = tool_execution_metadata(tool) or tool_execution_policy(tool.name)
        return ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=_schema_to_dict(tool.input_schema, tool_name=tool.name),
            required_inputs=_required_inputs(tool.input_schema),
            when_to_use=usage.get("when_to_use", []),
            when_not_to_use=usage.get("when_not_to_use", []),
            runtime_constraints=usage.get("runtime_constraints", ["Use only through ToolExecutor."]),
            side_effect=_resolved_tool_side_effect_policy(
                tool_name=tool.name,
                usage=usage,
                policy=policy,
                execution=execution,
            ),
            execution=execution,
            visibility=tool_visibility_policy(tool.name),
            policy=policy,
        )

    def describe_tools(self) -> List[Dict[str, Any]]:
        """Return legacy dict descriptions of all registered tools for the assistant."""

        return [spec.model_dump(mode="json") for spec in self.list_specs()]


def _schema_to_dict(schema_type, *, tool_name: str | None = None):
    """Convert a Pydantic model to a safe schema description."""
    try:
        schema = schema_type.model_json_schema()
        definitions = schema.get("$defs", {})
        normalized = _inline_local_schema_refs(schema, definitions)
        normalized.pop("$defs", None)
        properties = normalized.get("properties", {})
        required = list(normalized.get("required", []))
        for field_name in list(properties):
            if _hide_runtime_identity_field(tool_name, field_name):
                properties.pop(field_name, None)
                required = [item for item in required if item != field_name]
        normalized["properties"] = properties
        normalized["required"] = required
        normalized["fields"] = {
            field_name: {
                "type": field_info.get("type", "string"),
                "description": field_info.get("description", ""),
                "required": field_name in required,
            }
            for field_name, field_info in properties.items()
        }
        return _close_object_schemas(normalized)
    except Exception:
        return {"fields": {}}


def _inline_local_schema_refs(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_inline_local_schema_refs(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        name = reference.removeprefix("#/$defs/")
        target = definitions.get(name, {})
        merged = {**target, **{key: item for key, item in value.items() if key != "$ref"}}
        return _inline_local_schema_refs(merged, definitions)
    return {
        key: _inline_local_schema_refs(item, definitions)
        for key, item in value.items()
        if key != "$defs"
    }


def _close_object_schemas(value: Any) -> Any:
    if isinstance(value, list):
        return [_close_object_schemas(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _close_object_schemas(item) for key, item in value.items()}
    if normalized.get("type") == "object" or "properties" in normalized:
        normalized["additionalProperties"] = False
    return normalized


def _hide_runtime_identity_field(tool_name: str | None, field_name: str) -> bool:
    if tool_name in {IMAGE_UNDERSTANDING_TOOL_NAME, VIDEO_UNDERSTANDING_TOOL_NAME} and field_name in {
        "frame_refs",
        "context_id",
        "metadata",
        "memory_context",
        "sample_strategy",
        "user_id",
        "session_id",
    }:
        return True
    return tool_name in {
        MEMORY_RETRIEVAL_TOOL_NAME,
        MEMORY_SAVE_TOOL_NAME,
        MEMORY_MEDIA_INGEST_TOOL_NAME,
        MEMORY_INGEST_STATUS_TOOL_NAME,
    } and field_name in {"user_id", "session_id"}


def _required_inputs(schema_type) -> list[str]:
    try:
        schema = schema_type.model_json_schema()
        required = schema.get("required", [])
        return [str(item) for item in required if isinstance(item, str)]
    except Exception:
        return []


def tool_side_effect_policy(tool_name: str) -> ToolSideEffectPolicy:
    """Return static side-effect policy for a tool name.

    Unknown tools intentionally receive the conservative default policy.
    """

    payload = _ACTION_USAGE.get(tool_name, {}).get("side_effect")
    return _side_effect_policy_from_payload(payload)


def _side_effect_policy_from_payload(payload: Any) -> ToolSideEffectPolicy:
    if isinstance(payload, ToolSideEffectPolicy):
        return payload
    if isinstance(payload, dict):
        return ToolSideEffectPolicy.model_validate(payload)
    return ToolSideEffectPolicy()


def _resolved_tool_side_effect_policy(
    *,
    tool_name: str,
    usage: dict[str, Any],
    policy: ToolPolicyMetadata | None,
    execution: ToolExecutionPolicy,
) -> ToolSideEffectPolicy:
    declared_payload = usage.get("side_effect")
    declared = _side_effect_policy_from_payload(declared_payload)
    if policy is None:
        return declared

    from assistant_agent.services.tool_policy import ToolPolicyInterpreter

    canonical_view = ToolPolicyInterpreter().view_for_metadata(
        tool_name=tool_name,
        metadata=policy,
        execution=execution,
    )
    canonical = ToolSideEffectPolicy(
        level=canonical_view.side_effect_level,
        requires_confirmation=canonical_view.requires_confirmation,
        description=canonical_view.description,
        confirmation_kind=canonical_view.confirmation_kind,
        compensation_hint=canonical_view.compensation_hint,
    )
    if declared_payload is None:
        return canonical
    declared_core = (
        declared.level,
        declared.requires_confirmation,
        declared.confirmation_kind,
    )
    canonical_core = (
        canonical.level,
        canonical.requires_confirmation,
        canonical.confirmation_kind,
    )
    if declared_core != canonical_core:
        raise ValueError(
            "Conflicting side-effect declarations for "
            f"{tool_name}: usage={declared_core!r}, policy={canonical_core!r}"
        )
    return declared


def tool_execution_policy(tool_name: str) -> ToolExecutionPolicy:
    """Return static execution policy for a tool name.

    Unknown tools intentionally receive the conservative default policy.
    """

    payload = _ACTION_USAGE.get(tool_name, {}).get("execution")
    if isinstance(payload, ToolExecutionPolicy):
        return payload
    if isinstance(payload, dict):
        return ToolExecutionPolicy.model_validate(payload)
    return ToolExecutionPolicy()


def tool_visibility_policy(tool_name: str) -> VisibilityPolicy:
    """Return static catalog visibility policy for a tool name."""

    payload = _ACTION_USAGE.get(tool_name, {}).get("visibility")
    if isinstance(payload, VisibilityPolicy):
        return payload
    if isinstance(payload, dict):
        return VisibilityPolicy.model_validate(payload)
    return VisibilityPolicy()


def tool_policy_metadata(tool: BaseTool) -> ToolPolicyMetadata | None:
    """Return optional declarative policy metadata from a tool object."""

    payload = getattr(tool, "policy", None)
    if payload is None:
        return None
    if isinstance(payload, ToolPolicyMetadata):
        return payload
    if isinstance(payload, dict):
        return ToolPolicyMetadata.model_validate(payload)
    raise TypeError(f"Invalid policy metadata for tool {tool.name}: {type(payload).__name__}")


def tool_execution_metadata(tool: BaseTool) -> ToolExecutionPolicy | None:
    """Return optional scheduling metadata from a tool object."""

    payload = getattr(tool, "execution", None)
    if payload is None:
        return None
    if isinstance(payload, ToolExecutionPolicy):
        return payload
    if isinstance(payload, dict):
        return ToolExecutionPolicy.model_validate(payload)
    raise TypeError(f"Invalid execution metadata for tool {tool.name}: {type(payload).__name__}")


_ACTION_USAGE: dict[str, dict[str, Any]] = {
    IMAGE_UNDERSTANDING_TOOL_NAME: {
        "when_to_use": [
            "Describe, analyze, or identify image content.",
            "Summarize or analyze an explicit video_ref or video_ids supplied by the current request.",
            "Use as the primary visual understanding tool for image or explicit video inputs.",
        ],
        "when_not_to_use": [
            "User asks to generate a new image.",
            "User asks to render or build a 3D scene.",
            "No image, active video, or explicit video reference exists in the current turn.",
        ],
        "runtime_constraints": [
            "Requires image_ids, video_ref, video_ids, or a trusted active video reference.",
            "Do not pass internal frame paths, JPEG/base64 payloads, local media paths, metadata, or provider fields.",
            "If evidence is insufficient, stale, or uncertain, report that uncertainty from the tool result.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads media/provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["media:image", "media:video"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我看一下。",
        },
        "visibility": {
            "requires_media": ["image", "video"],
        },
    },
    VIDEO_UNDERSTANDING_TOOL_NAME: {
        "when_to_use": [
            "Answer visual-fact questions about the current realtime camera when this tool is exposed for an active-video turn.",
            "Summarize or analyze an explicit video_ref or video_ids supplied by the current request.",
            "Call when the current answer needs fresh visual facts rather than relying only on passive realtime_video_context.",
        ],
        "when_not_to_use": [
            "User only asks for image generation.",
            "User asks for 3D rendering without video context.",
            "No active realtime camera or explicit video reference exists in the current turn.",
        ],
        "runtime_constraints": [
            "Requires current active video from the request, video_ref, or video_ids.",
            "Do not pass internal frame paths, JPEG/base64 payloads, local media paths, or provider fields.",
            "Use the current turn's video reference; runtime binds trusted active-video turns.",
            "If evidence is insufficient, stale, or uncertain, report that uncertainty from the tool result.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads media/provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["media:video"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我分析一下。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
            "requires_media": ["video"],
        },
    },
    IMAGE_GENERATION_TOOL_NAME: {
        "when_to_use": ["Generate an image, poster, product hero image, or visual creative from text."],
        "when_not_to_use": ["User asks to describe an existing image or video."],
        "runtime_constraints": ["Prompt must describe the image to generate."],
        "side_effect": {
            "level": "compensatable",
            "requires_confirmation": False,
            "description": "Creates a generated artifact; an interrupt cannot erase the existing artifact or provider cost.",
            "compensation_hint": "Generate a corrected replacement or explain that the previous artifact already exists.",
        },
        "execution": {
            "dependency_mode": "terminal",
            "resource_writes": ["artifact:image"],
            "realtime_safety": "needs_progress",
            "artifact_reuse": "requires_validation",
            "progress_message": "我开始生成，可能需要一点时间。",
        },
    },
    RENDER_3D_TOOL_NAME: {
        "when_to_use": ["User explicitly asks for 3D, rendering, modeling, scene preview, or displaying an object in a space."],
        "when_not_to_use": ["User only asks to describe the scene in an image or video.", "Do not trigger from the word 场景 alone."],
        "runtime_constraints": ["Requires valid render inputs accepted by the tool schema."],
        "side_effect": {
            "level": "compensatable",
            "requires_confirmation": False,
            "description": "Creates a render/model artifact; an interrupt should revise or replace it rather than claim it was undone.",
            "compensation_hint": "Render a corrected replacement or explain which preview already exists.",
        },
        "execution": {
            "dependency_mode": "terminal",
            "resource_writes": ["artifact:3d"],
            "realtime_safety": "needs_progress",
            "artifact_reuse": "requires_validation",
        },
    },
    SHOPPING_SEARCH_TOOL_NAME: {
        "when_to_use": [
            "Search current product candidates and compare prices or offers in one call.",
            "User asks for shopping recommendations, purchase advice, value judgement, or price comparison.",
        ],
        "when_not_to_use": ["User only asks for general chat, image description, or non-shopping web facts."],
        "runtime_constraints": [
            "Requires query, visual summary, video summary, or product descriptors.",
            "Does not place orders or perform payment.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads product/provider offer data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["product_catalog", "offers"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我查一下并比一下价格。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    PYTHON_INTERPRETER_TOOL_NAME: {
        "when_to_use": [
            "Run short local Python snippets for math, scientific, data, or code analysis when the tool is explicitly enabled.",
            "Use when deterministic computation or parsing is needed and the required input data is already in the prompt or tool input.",
        ],
        "when_not_to_use": [
            "The task needs network access, package installation, shell commands, browser automation, or access to arbitrary local files.",
            "The analysis can be answered directly without executing code.",
            "The Python interpreter is not explicitly enabled for this run.",
        ],
        "runtime_constraints": [
            "Requires code and explicit MULTIMODAL_AGENT_PYTHON_INTERPRETER_ENABLED opt-in.",
            "Code runs in a short-lived restricted local subprocess with timeout and output limits.",
            "Only prompt-provided JSON input_data is available; do not request local paths, network, shell, or package installation.",
            "Assign the final structured value to result; printed stdout is truncated.",
        ],
        "side_effect": {
            "level": "local_read",
            "requires_confirmation": False,
            "description": "Runs restricted local analysis code and does not intentionally read/write local or external state.",
        },
        "execution": {
            "dependency_mode": "requires_prior_observation",
            "concurrency_group": PYTHON_INTERPRETER_TOOL_NAME,
            "resource_reads": ["analysis:input_data"],
            "realtime_safety": "needs_progress",
            "artifact_reuse": "requires_validation",
            "progress_message": "我用本地 Python 算一下。",
        },
        "visibility": {
            "toolset": "analysis.local",
            "tags": ["python", "analysis"],
            "requires_env": ["MULTIMODAL_AGENT_PYTHON_INTERPRETER_ENABLED"],
            "enabled_by_default": False,
        },
    },
    TOOL_SEARCH_TOOL_NAME: {
        "when_to_use": [
            "Only when the exposed core tools cannot satisfy the user request.",
            "Inspect configured MCP servers for additional allowlisted tools, including tools that are configured but not enabled by default.",
        ],
        "when_not_to_use": [
            "A core exposed tool can answer or complete the request.",
            "User asks to execute an MCP tool directly; tool_search only discovers candidates and does not grant execution permission.",
            "User asks to install, connect, or enable a new external tool; report that permission is required instead of executing.",
        ],
        "runtime_constraints": [
            "Discovery only; does not execute returned tools.",
            "Returns prompt-safe MCP tool names, descriptions, input summaries, and permission status.",
            "Unallowlisted MCP server tools are omitted from model-facing results.",
            "A discovered tool still must be explicitly enabled when required and must execute through ActionValidator and ToolExecutor.",
        ],
        "side_effect": {
            "level": "local_read",
            "requires_confirmation": False,
            "description": "Reads configured MCP tool catalog metadata and does not execute MCP tools or mutate state.",
        },
        "execution": {
            "dependency_mode": "requires_prior_observation",
            "resource_reads": ["mcp.tool_catalog"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我看一下还有哪些可用工具。",
        },
        "visibility": {
            "toolset": "tool.discovery",
            "tags": ["tool_search", "mcp"],
        },
    },
    WEB_SEARCH_TOOL_NAME: {
        "when_to_use": [
            "Answer current, latest, recent, realtime, news, or public web lookup requests when no dedicated tool covers the requested fact.",
            "User explicitly asks to search the web, look up online information, or check current information.",
        ],
        "when_not_to_use": [
            "User asks for weather, calendar, contacts, commute, morning, or departure briefing facts that can be handled by weather, calendar_search, or contacts_search.",
            "User asks for shopping/product candidates; use shopping_search instead.",
            "User asks to use saved preferences or prior chats; memory tools may be relevant but do not replace web_search for current facts.",
            "Do not use for ordinary chat or timeless explanations that can be answered from available context.",
        ],
        "runtime_constraints": [
            "Requires query.",
            "Read-only search result retrieval; v1 does not fetch full pages or run a browser.",
            "Real HTTP search requires provider_smoke or pilot runtime profile plus explicit MULTIMODAL_AGENT_SEARCH_PROVIDER=http.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads web search provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": [WEB_SEARCH_TOOL_NAME],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我联网查一下。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    VISUAL_IMAGE_SEARCH_TOOL_NAME: {
        "when_to_use": [
            "Search the internet for visually similar images from a public image URL.",
            "User asks to search by image, find visually similar images, trace an image source, or find same-style images online.",
        ],
        "when_not_to_use": [
            "User asks to describe or understand image content; use vision_understanding instead.",
            "User asks for text web search; use web_search instead.",
            "Only local paths, base64 payloads, private media IDs, or non-public image references are available.",
        ],
        "runtime_constraints": [
            "Requires image_url or image_ids containing public http or https image URLs.",
            "v1 uses Qwen Responses API image_search only; do not fallback to vision_understanding or text web_search.",
            "Do not pass local media paths, base64 payloads, provider raw responses, API keys, or private media IDs.",
            "Real Qwen image_search requires provider_smoke or pilot runtime profile plus explicit MULTIMODAL_AGENT_VISUAL_IMAGE_SEARCH_PROVIDER=qwen.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads public image search/provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["media:image", "web_image_search"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我找一下相似图片。",
        },
        "visibility": {
            "requires_media": ["image"],
        },
    },
    WEB_FETCH_TOOL_NAME: {
        "when_to_use": [
            "Fetch readable page content from a specific HTTP(S) URL provided by the user or returned by web_search.",
            "Use when search snippets are insufficient and the answer needs content from a known web page.",
        ],
        "when_not_to_use": [
            "No specific URL is available; use web_search first for general web lookup.",
            "User asks for browser automation, form submission, login-only content, or JavaScript-rendered interaction.",
            "User asks for shopping/product candidates; use shopping_search instead.",
        ],
        "runtime_constraints": [
            "Requires an http or https URL.",
            "Returns extracted readable text only; does not render a browser, submit forms, or crawl multiple pages.",
            "Real HTTP fetch requires provider_smoke or pilot runtime profile plus explicit MULTIMODAL_AGENT_SEARCH_PROVIDER=http.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads web page/provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "requires_prior_observation",
            "resource_reads": ["web_page"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我打开这个网页看一下。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    WEATHER_TOOL_NAME: {
        "when_to_use": [
            "User asks for current weather, short-range forecast, rain, temperature, or 出门天气 for a named location.",
            "Use for morning or departure briefings that need weather facts.",
        ],
        "when_not_to_use": [
            "User asks for general news or arbitrary web facts; use web_search when current web facts are needed.",
            "No location is available; ask for the location or use a trusted runtime location when one exists.",
        ],
        "runtime_constraints": [
            "Requires location.",
            "Default adapter is deterministic mock/offline; real weather providers must be explicitly configured in a future provider boundary.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads weather provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["weather.forecast"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我查一下天气。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    CALENDAR_SEARCH_TOOL_NAME: {
        "when_to_use": [
            "User asks to inspect calendar events, meetings, free/busy context, or 日程.",
            "Use for morning or departure briefings to inspect today's personal schedule before advising travel timing or conflicts.",
            "Use before scheduling workflows that need existing events or availability context.",
        ],
        "when_not_to_use": [
            "User asks to create, update, or delete an event; use a mutating calendar tool with confirmation.",
            "User asks for a reminder/todo rather than a calendar event.",
        ],
        "runtime_constraints": [
            "Query may be omitted or blank for today's calendar in morning/departure briefings.",
            "Use explicit query, time window, or a clear natural-language calendar search when available.",
            "Return prompt-safe event summaries only; do not expose raw provider payloads.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads calendar provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["calendar.events"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我查一下日历。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    CALENDAR_CREATE_TOOL_NAME: {
        "when_to_use": [
            "User asks to create a calendar event after event details are known.",
            "Use in meeting scheduling workflows only after availability/contact context is gathered and the user confirms the write.",
        ],
        "when_not_to_use": [
            "User only asks to inspect schedule; use calendar_search.",
            "Event title or start time is missing; ask for missing details before attempting the write.",
        ],
        "runtime_constraints": [
            "Requires title, start_time, runtime confirmation metadata, and idempotency_key.",
            "Model-supplied confirmation flags do not authorize execution.",
        ],
        "side_effect": {
            "level": "committed",
            "requires_confirmation": True,
            "description": "Creates an external calendar event after runtime confirmation.",
            "confirmation_kind": "calendar_write",
            "compensation_hint": "Report the created event reference and offer an explicit follow-up change or deletion path.",
        },
        "execution": {
            "dependency_mode": "terminal",
            "resource_reads": ["calendar.events"],
            "resource_writes": ["calendar.events"],
            "realtime_safety": "needs_confirmation",
            "artifact_reuse": "do_not_reuse",
            "progress_message": "需要你确认后我再创建日程。",
        },
    },
    CONTACTS_SEARCH_TOOL_NAME: {
        "when_to_use": [
            "User asks to find a contact, candidate attendee, phone number, or email address.",
            "Use in scheduling workflows when attendee names must be resolved before creating an event.",
        ],
        "when_not_to_use": [
            "User asks to send a message, email, or call someone; those actions are not implemented.",
            "No contact query or candidate name is available.",
        ],
        "runtime_constraints": [
            "Requires query.",
            "Return candidate contact details only; do not expose raw provider payloads.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads contacts provider data and does not mutate external state.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["contacts"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
            "progress_message": "我查一下联系人。",
        },
        "visibility": {
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    REMINDER_CREATE_TOOL_NAME: {
        "when_to_use": [
            "User asks to create a reminder, todo, or action item from the current conversation.",
            "Use after extracting a concrete task title and optional due time.",
        ],
        "when_not_to_use": [
            "User only asks to brainstorm or summarize action items without saving them.",
            "The reminder title is missing; ask for the missing task.",
        ],
        "runtime_constraints": [
            "Requires title, runtime confirmation metadata, and idempotency_key.",
            "Model-supplied confirmation flags do not authorize execution.",
        ],
        "side_effect": {
            "level": "committed",
            "requires_confirmation": True,
            "description": "Creates a reminder/todo after runtime confirmation.",
            "confirmation_kind": "reminder_write",
            "compensation_hint": "Report the created reminder reference and offer an explicit follow-up change or deletion path.",
        },
        "execution": {
            "dependency_mode": "terminal",
            "resource_writes": ["reminders"],
            "realtime_safety": "needs_confirmation",
            "artifact_reuse": "do_not_reuse",
            "progress_message": "需要你确认后我再创建提醒。",
        },
    },
    MEMORY_RETRIEVAL_TOOL_NAME: {
        "when_to_use": [
            "User explicitly asks to use prior chats, saved memory, remembered preferences, or previous/last context.",
            "User asks to continue a prior task or says to follow their saved preferences.",
        ],
        "when_not_to_use": [
            "No historical context is needed.",
            "User asks for a first-pass answer, copywriting, product search, image generation, or general advice without referencing saved context.",
            "Do not infer memory need from broad words like preference/style/tone unless the user refers to their own saved preference or prior conversation.",
        ],
        "runtime_constraints": ["Requires user_id and query.", "Return final_answer directly when the current request can be answered without prior context."],
        "side_effect": {
            "level": "local_read",
            "requires_confirmation": False,
            "description": "Reads user-scoped memory context and does not mutate memory.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["memory"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
        },
        "visibility": {
            "toolset": "memory",
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    MEMORY_SAVE_TOOL_NAME: {
        "when_to_use": [
            "User explicitly asks to remember or save a preference, project fact, or task context; set source_intent=user_explicit.",
            "The assistant infers a stable, non-sensitive user preference or project fact may be useful later; set source_intent=assistant_candidate.",
        ],
        "when_not_to_use": [
            "Do not save sensitive data or incidental content without intent.",
            "Do not save ordinary one-off task outputs, generated copy, search results, or transient wording unless the user asks to remember them.",
            "Do not use source_intent=user_confirmed; it is reserved for the confirmation service.",
        ],
        "runtime_constraints": [
            "Requires user_id and query, content.text, or content.summary.",
            "Assistant-loop calls must include source_intent, source_reason, future_use, and evidence.",
            "source_intent must be user_explicit or assistant_candidate for LLM calls.",
            "Memory writes must remain concise, auditable, and about long-term user/project value.",
        ],
        "side_effect": {
            "level": "pending_confirmation",
            "requires_confirmation": True,
            "description": "Writes durable user memory when policy allows; sensitive writes create a pending confirmation instead.",
            "confirmation_kind": "memory_write",
            "compensation_hint": "Confirm, reject, delete, or update the saved memory through the memory confirmation/audit path.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_writes": ["memory"],
            "realtime_safety": "needs_confirmation",
            "artifact_reuse": "do_not_reuse",
        },
        "visibility": {
            "toolset": "memory",
            "allowed_entry_profiles": ["agent_service"],
        },
    },
    MEMORY_MEDIA_INGEST_TOOL_NAME: {
        "when_to_use": [
            "User explicitly asks to upload, ingest, or import media into long-term memory for later retrieval.",
            "The request contains safe file references and asks for Memory Server media ingestion.",
        ],
        "when_not_to_use": [
            "Do not use for ordinary video/image understanding; use video_understanding or vision_understanding instead.",
            "Do not use for explicit text memory saves; use memory_save instead.",
            "Do not use unless the user asks for durable media ingestion into memory.",
        ],
        "runtime_constraints": [
            "Requires runtime user identity and at least one file reference.",
            "Requires explicit Memory Server remote configuration.",
            "Must execute through ToolExecutor; this is not memory_save.",
        ],
        "side_effect": {
            "level": "committed",
            "requires_confirmation": True,
            "description": "Submits media to an external Memory Server ingestion task that may create durable remote memories.",
            "confirmation_kind": MEMORY_MEDIA_INGEST_TOOL_NAME,
            "compensation_hint": "Report the submitted task id and use memory_ingest_status to monitor completion.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_writes": ["memory_media"],
            "realtime_safety": "needs_confirmation",
            "artifact_reuse": "do_not_reuse",
        },
        "visibility": {"toolset": "memory"},
    },
    MEMORY_INGEST_STATUS_TOOL_NAME: {
        "when_to_use": [
            "Check the processing state of a previously submitted Memory Server media ingestion task.",
        ],
        "when_not_to_use": [
            "Do not use to submit media; use memory_media_ingest.",
            "Do not use for general memory retrieval; use memory_retrieval.",
        ],
        "runtime_constraints": [
            "Requires runtime user identity and task_id.",
            "Current external Memory Server task lookup accepts user_id but is not user-enforced.",
        ],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads external Memory Server task status and does not submit new media.",
        },
        "execution": {
            "dependency_mode": "independent",
            "resource_reads": ["memory_media"],
            "realtime_safety": "safe",
            "artifact_reuse": "reusable",
        },
        "visibility": {"toolset": "memory"},
    },
    "delegate_to_agent": {
        "when_to_use": [
            "A task must be delegated to another enabled local agent instance.",
            "The caller has an explicit target_agent_id and a bounded delegation task.",
        ],
        "when_not_to_use": [
            "Default single-agent runs.",
            "No target_agent_id is provided.",
            "The target is the current/source agent.",
            "Remote A2A or network agent calls are needed; this local tool does not perform network transport.",
        ],
        "runtime_constraints": [
            "Opt-in only; not registered in the default ToolRegistry.",
            "Requires target_agent_id and text, image_ids, video_ids, or audio_id.",
            "Must execute through ActionValidator and ToolExecutor.",
        ],
        "side_effect": {
            "level": "compensatable",
            "requires_confirmation": False,
            "description": "Starts work in another local agent; interruption should cancel, supersede, or follow up rather than claim the child task never started.",
            "compensation_hint": "Cancel or supersede the delegated task when the transport supports it, otherwise send a follow-up correction.",
        },
        "execution": {
            "dependency_mode": "terminal",
            "resource_writes": ["agent_task"],
            "realtime_safety": "needs_progress",
            "artifact_reuse": "do_not_reuse",
        },
    },
    "task_plan_submit": {
        "when_to_use": [
            "The request explicitly requires durable execution.",
            "A complex task must survive the current request or client connection.",
        ],
        "when_not_to_use": [
            "A simple request can be answered or completed in the foreground.",
            "The request explicitly selects foreground execution.",
        ],
        "runtime_constraints": [
            "Must be the only provider-native tool call in its batch.",
            "Task identity and revision binding come only from ToolContext.",
            "Use only through ToolExecutor.",
        ],
        "side_effect": {
            "level": "committed",
            "requires_confirmation": False,
            "description": "Creates or revises a local durable task record.",
        },
        "execution": {
            "dependency_mode": "terminal",
            "resource_writes": ["durable_task"],
            "realtime_safety": "needs_progress",
            "artifact_reuse": "do_not_reuse",
            "progress_message": "我先把任务整理成可恢复的执行计划。",
        },
    },
}


def create_default_registry(
    config: ProviderConfig | None = None,
    *,
    video_context_store: VideoContextStore | None = None,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
    enable_agent_delegation: bool = False,
    agent_communication_service: AgentCommunicationService | None = None,
    durable_task_service: DurableTaskService | None = None,
    enable_mcp_tools: bool = False,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_config_path: str | None = None,
    mcp_runner: MCPToolDiscoveryRunner | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    should_use_mcp_personal = getattr(config, "personal_assistant_provider", "mock") == "mcp"
    resolved_mcp_server_configs = mcp_server_configs
    if (enable_mcp_tools or should_use_mcp_personal) and resolved_mcp_server_configs is None:
        from assistant_agent.mcp.config import load_mcp_server_configs_from_env

        resolved_mcp_server_configs = load_mcp_server_configs_from_env(config_path=mcp_config_path)
    personal_adapters = create_personal_assistant_adapter_bundle(
        config,
        mcp_server_configs=resolved_mcp_server_configs,
        mcp_runner=mcp_runner,
    )
    memory_media_service = create_memory_media_ingestion_service(config)
    shopping_search_adapter = create_shopping_search_adapter(config)
    shopping_compare_adapter = create_shopping_compare_adapter(config)
    vision_client = create_vision_understanding_client(config)
    for tool in (
        VisionUnderstandingTool(
            client=vision_client,
            context_store=video_context_store,
            memory_store=realtime_video_memory_store,
        ),
        VideoUnderstandingTool(
            client=vision_client,
            context_store=video_context_store,
            memory_store=realtime_video_memory_store,
        ),
        ShoppingSearchTool(
            search_adapter=shopping_search_adapter,
            compare_adapter=shopping_compare_adapter,
        ),
        WeatherTool(adapter=personal_adapters.weather),
        CalendarSearchTool(adapter=personal_adapters.calendar),
        CalendarCreateTool(adapter=personal_adapters.calendar),
        ContactsSearchTool(adapter=personal_adapters.contacts),
        ReminderCreateTool(adapter=personal_adapters.reminder),
        WebSearchTool(adapter=create_web_search_adapter(config)),
        VisualImageSearchTool(adapter=create_visual_image_search_adapter(config)),
        WebFetchTool(adapter=create_web_fetch_adapter(config)),
        ToolSearchTool(
            server_configs=resolved_mcp_server_configs or [],
            runner=mcp_runner,
        ),
        ImageGenerationTool(adapter=create_image_generation_adapter(config)),
        Render3DTool(adapter=create_render_adapter(config)),
        PythonInterpreterTool(),
        MemoryRetrievalTool(),
        MemorySaveTool(),
        MemoryMediaIngestTool(memory_media_service),
        MemoryIngestStatusTool(memory_media_service),
    ):
        registry.register(tool)
    if enable_agent_delegation:
        if agent_communication_service is None:
            raise ValueError("agent_communication_service is required when agent delegation is enabled")
        registry.register(AgentDelegationTool(agent_communication_service))
    if config is not None and config.durable_tasks_enabled:
        if durable_task_service is not None:
            registry.register(TaskPlanSubmitTool(durable_task_service))
    if enable_mcp_tools or mcp_server_configs is not None:
        from assistant_agent.mcp.registration import register_configured_mcp_tools

        server_configs = resolved_mcp_server_configs or []
        register_configured_mcp_tools(registry, server_configs, runner=mcp_runner)
    return registry


def create_realtime_video_observation_registry(
    config: ProviderConfig | None = None,
    *,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
) -> ToolRegistry:
    """Create the governed, realtime-observer-only video tool registry."""

    registry = ToolRegistry()
    registry.register(
        VideoUnderstandingTool(
            client=create_realtime_vision_understanding_client(config),
            memory_store=realtime_video_memory_store,
        )
    )
    return registry
