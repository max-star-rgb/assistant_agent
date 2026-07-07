"""Tool registry and default mock tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Dict

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.tools import ToolResult, ToolSideEffectPolicy, ToolSpec
from assistant_agent.tools.base import BaseTool, ToolContext
from assistant_agent.tools.agent_delegation_tool import AgentDelegationTool
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from assistant_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool, MemoryTool
from assistant_agent.tools.price_compare_tool import PriceCompareTool
from assistant_agent.tools.product_search_tool import ProductSearchTool
from assistant_agent.tools.render_tool import Render3DTool
from assistant_agent.services.web_search_adapter import create_web_search_adapter
from assistant_agent.services.image_generation_adapter import create_image_generation_adapter
from assistant_agent.services.product_adapter import create_price_compare_adapter, create_product_search_adapter
from assistant_agent.services.provider_selection import create_vision_adapter
from assistant_agent.services.render_adapter import create_render_adapter
from assistant_agent.services.video_adapter import create_video_understanding_adapter
from assistant_agent.services.video_context import VideoContextStore
from assistant_agent.tools.video_tool import VideoUnderstandingTool
from assistant_agent.tools.vision_tool import VisionUnderstandingTool
from assistant_agent.tools.web_search_tool import WebSearchTool

if TYPE_CHECKING:
    from assistant_agent.services.agent_communication import AgentCommunicationService


class ToolRegistry:
    """In-memory registry for tool lookup and execution."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
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

    def list_specs(self) -> list[ToolSpec]:
        """Return provider-neutral specs for all registered tools."""

        specs: list[ToolSpec] = []
        for name in sorted(self._tools.keys()):
            tool = self._tools[name]
            usage = _ACTION_USAGE.get(tool.name, {})
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    input_schema=_schema_to_dict(tool.input_schema, tool_name=tool.name),
                    required_inputs=_required_inputs(tool.input_schema),
                    when_to_use=usage.get("when_to_use", []),
                    when_not_to_use=usage.get("when_not_to_use", []),
                    runtime_constraints=usage.get("runtime_constraints", ["Use only through ToolExecutor."]),
                    side_effect=tool_side_effect_policy(tool.name),
                )
            )
        return specs

    def describe_tools(self) -> List[Dict[str, Any]]:
        """Return legacy dict descriptions of all registered tools for the assistant."""

        return [spec.model_dump(mode="json") for spec in self.list_specs()]


def _schema_to_dict(schema_type, *, tool_name: str | None = None):
    """Convert a Pydantic model to a safe schema description."""
    try:
        schema = schema_type.model_json_schema()
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        fields = {}
        for field_name, field_info in properties.items():
            if _hide_runtime_identity_field(tool_name, field_name):
                continue
            fields[field_name] = {
                "type": field_info.get("type", "string"),
                "description": field_info.get("description", ""),
                "required": field_name in required,
            }
        return {"fields": fields}
    except Exception:
        return {"fields": {}}


def _hide_runtime_identity_field(tool_name: str | None, field_name: str) -> bool:
    return tool_name in {"memory_retrieval", "memory_save"} and field_name in {"user_id", "session_id"}


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
    if isinstance(payload, ToolSideEffectPolicy):
        return payload
    if isinstance(payload, dict):
        return ToolSideEffectPolicy.model_validate(payload)
    return ToolSideEffectPolicy()


_ACTION_USAGE: dict[str, dict[str, Any]] = {
    "vision_understanding": {
        "when_to_use": ["Describe, analyze, or identify image content.", "User provided image_ids and asks what is in the image."],
        "when_not_to_use": ["User asks to generate a new image.", "User asks to render or build a 3D scene."],
        "runtime_constraints": ["Requires image_ids.", "Do not use for video-only requests."],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads media/provider data and does not mutate external state.",
        },
    },
    "video_understanding": {
        "when_to_use": ["Summarize or analyze video content.", "User provided video_ids and asks what happens in the video."],
        "when_not_to_use": ["User only asks for image generation.", "User asks for 3D rendering without video context."],
        "runtime_constraints": ["Requires video_ref or video_ids."],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads media/provider data and does not mutate external state.",
        },
    },
    "image_generation": {
        "when_to_use": ["Generate an image, poster, product hero image, or visual creative from text."],
        "when_not_to_use": ["User asks to describe an existing image or video."],
        "runtime_constraints": ["Prompt must describe the image to generate."],
        "side_effect": {
            "level": "compensatable",
            "requires_confirmation": False,
            "description": "Creates a generated artifact; an interrupt cannot erase the existing artifact or provider cost.",
            "compensation_hint": "Generate a corrected replacement or explain that the previous artifact already exists.",
        },
    },
    "render_3d": {
        "when_to_use": ["User explicitly asks for 3D, rendering, modeling, scene preview, or displaying an object in a space."],
        "when_not_to_use": ["User only asks to describe the scene in an image or video.", "Do not trigger from the word 场景 alone."],
        "runtime_constraints": ["Requires explicit render intent."],
        "side_effect": {
            "level": "compensatable",
            "requires_confirmation": False,
            "description": "Creates a render/model artifact; an interrupt should revise or replace it rather than claim it was undone.",
            "compensation_hint": "Render a corrected replacement or explain which preview already exists.",
        },
    },
    "product_search": {
        "when_to_use": ["Search for products, similar items, or product candidates."],
        "when_not_to_use": ["User only asks for general chat or image description."],
        "runtime_constraints": ["Requires query or visual summary."],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads product/provider data and does not mutate external state.",
        },
    },
    "price_compare": {
        "when_to_use": ["Compare prices, offers, or cheapest options."],
        "when_not_to_use": ["No product candidates or product query are available."],
        "runtime_constraints": ["Use product_search first if no candidates are available."],
        "side_effect": {
            "level": "external_read",
            "requires_confirmation": False,
            "description": "Reads offer/provider data and does not mutate external state.",
        },
    },
    "web_search": {
        "when_to_use": [
            "Answer current, latest, recent, today, realtime, news, or web lookup requests.",
            "User explicitly asks to search the web, look up online information, or check current information.",
        ],
        "when_not_to_use": [
            "User asks for shopping/product candidates; use product_search instead.",
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
    },
    "memory": {
        "when_to_use": ["Legacy memory retrieve/save compatibility tool."],
        "when_not_to_use": ["Prefer memory_retrieval or memory_save in the assistant loop."],
        "runtime_constraints": ["Legacy compatibility only; use dedicated memory tools when possible."],
        "side_effect": {
            "level": "pending_confirmation",
            "requires_confirmation": True,
            "description": "May write durable user memory depending on action; treat as confirmation-sensitive.",
            "confirmation_kind": "memory_write",
        },
    },
    "memory_retrieval": {
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
    },
    "memory_save": {
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
    },
}


def create_default_registry(
    config: ProviderConfig | None = None,
    *,
    video_context_store: VideoContextStore | None = None,
    enable_agent_delegation: bool = False,
    agent_communication_service: AgentCommunicationService | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        VisionUnderstandingTool(adapter=create_vision_adapter(config)),
        VideoUnderstandingTool(adapter=create_video_understanding_adapter(config), context_store=video_context_store),
        ProductSearchTool(adapter=create_product_search_adapter(config)),
        PriceCompareTool(adapter=create_price_compare_adapter(config)),
        WebSearchTool(adapter=create_web_search_adapter(config)),
        ImageGenerationTool(adapter=create_image_generation_adapter(config)),
        Render3DTool(adapter=create_render_adapter(config)),
        MemoryTool(),
        MemoryRetrievalTool(),
        MemorySaveTool(),
    ):
        registry.register(tool)
    if enable_agent_delegation:
        if agent_communication_service is None:
            raise ValueError("agent_communication_service is required when agent delegation is enabled")
        registry.register(AgentDelegationTool(agent_communication_service))
    return registry
