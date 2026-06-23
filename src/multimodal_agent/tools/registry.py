"""Tool registry and default mock tool registration."""

from __future__ import annotations

from typing import Any, List, Dict

from pydantic import BaseModel

from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.tools import ToolResult, ToolSpec
from multimodal_agent.tools.base import BaseTool, ToolContext
from multimodal_agent.tools.image_generation_tool import ImageGenerationTool
from multimodal_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool, MemoryTool
from multimodal_agent.tools.price_compare_tool import PriceCompareTool
from multimodal_agent.tools.product_search_tool import ProductSearchTool
from multimodal_agent.tools.render_tool import Render3DTool
from multimodal_agent.services.image_generation_adapter import create_image_generation_adapter
from multimodal_agent.services.product_adapter import create_price_compare_adapter, create_product_search_adapter
from multimodal_agent.services.provider_selection import create_vision_adapter
from multimodal_agent.services.render_adapter import create_render_adapter
from multimodal_agent.services.video_adapter import create_video_understanding_adapter
from multimodal_agent.services.video_context import VideoContextStore
from multimodal_agent.tools.video_tool import VideoUnderstandingTool
from multimodal_agent.tools.vision_tool import VisionUnderstandingTool


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
                    input_schema=_schema_to_dict(tool.input_schema),
                    required_inputs=_required_inputs(tool.input_schema),
                    when_to_use=usage.get("when_to_use", []),
                    when_not_to_use=usage.get("when_not_to_use", []),
                    runtime_constraints=usage.get("runtime_constraints", ["Use only through ToolExecutor."]),
                )
            )
        return specs

    def describe_tools(self) -> List[Dict[str, Any]]:
        """Return legacy dict descriptions of all registered tools for the assistant."""

        return [spec.model_dump(mode="json") for spec in self.list_specs()]


def _schema_to_dict(schema_type):
    """Convert a Pydantic model to a safe schema description."""
    try:
        schema = schema_type.model_json_schema()
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        fields = {}
        for field_name, field_info in properties.items():
            fields[field_name] = {
                "type": field_info.get("type", "string"),
                "description": field_info.get("description", ""),
                "required": field_name in required,
            }
        return {"fields": fields}
    except Exception:
        return {"fields": {}}


def _required_inputs(schema_type) -> list[str]:
    try:
        schema = schema_type.model_json_schema()
        required = schema.get("required", [])
        return [str(item) for item in required if isinstance(item, str)]
    except Exception:
        return []


_ACTION_USAGE: dict[str, dict[str, list[str]]] = {
    "vision_understanding": {
        "when_to_use": ["Describe, analyze, or identify image content.", "User provided image_ids and asks what is in the image."],
        "when_not_to_use": ["User asks to generate a new image.", "User asks to render or build a 3D scene."],
        "runtime_constraints": ["Requires image_ids.", "Do not use for video-only requests."],
    },
    "video_understanding": {
        "when_to_use": ["Summarize or analyze video content.", "User provided video_ids and asks what happens in the video."],
        "when_not_to_use": ["User only asks for image generation.", "User asks for 3D rendering without video context."],
        "runtime_constraints": ["Requires video_ref or video_ids."],
    },
    "image_generation": {
        "when_to_use": ["Generate an image, poster, product hero image, or visual creative from text."],
        "when_not_to_use": ["User asks to describe an existing image or video."],
        "runtime_constraints": ["Prompt must describe the image to generate."],
    },
    "render_3d": {
        "when_to_use": ["User explicitly asks for 3D, rendering, modeling, scene preview, or displaying an object in a space."],
        "when_not_to_use": ["User only asks to describe the scene in an image or video.", "Do not trigger from the word 场景 alone."],
        "runtime_constraints": ["Requires explicit render intent."],
    },
    "product_search": {
        "when_to_use": ["Search for products, similar items, or product candidates."],
        "when_not_to_use": ["User only asks for general chat or image description."],
        "runtime_constraints": ["Requires query or visual summary."],
    },
    "price_compare": {
        "when_to_use": ["Compare prices, offers, or cheapest options."],
        "when_not_to_use": ["No product candidates or product query are available."],
        "runtime_constraints": ["Use product_search first if no candidates are available."],
    },
    "memory_retrieval": {
        "when_to_use": ["User references previous, last, remembered, or preference context."],
        "when_not_to_use": ["No historical context is needed."],
        "runtime_constraints": ["Requires user_id and query."],
    },
    "memory_save": {
        "when_to_use": ["User explicitly asks to remember or save preference/task context."],
        "when_not_to_use": ["Do not save sensitive data or incidental content without intent."],
        "runtime_constraints": ["Requires user_id and content."],
    },
}


def create_default_registry(
    config: ProviderConfig | None = None,
    *,
    video_context_store: VideoContextStore | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        VisionUnderstandingTool(adapter=create_vision_adapter(config)),
        VideoUnderstandingTool(adapter=create_video_understanding_adapter(config), context_store=video_context_store),
        ProductSearchTool(adapter=create_product_search_adapter(config)),
        PriceCompareTool(adapter=create_price_compare_adapter(config)),
        ImageGenerationTool(adapter=create_image_generation_adapter(config)),
        Render3DTool(adapter=create_render_adapter(config)),
        MemoryTool(),
        MemoryRetrievalTool(),
        MemorySaveTool(),
    ):
        registry.register(tool)
    return registry
