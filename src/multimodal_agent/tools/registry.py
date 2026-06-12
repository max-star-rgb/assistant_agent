"""Tool registry and default mock tool registration."""

from typing import Any

from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.tools.base import BaseTool, ToolContext
from multimodal_agent.tools.image_generation_tool import ImageGenerationTool
from multimodal_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool, MemoryTool
from multimodal_agent.tools.price_compare_tool import PriceCompareTool
from multimodal_agent.tools.product_search_tool import ProductSearchTool
from multimodal_agent.tools.render_tool import Render3DTool
from multimodal_agent.services.provider_selection import create_vision_adapter
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


def create_default_registry(config: ProviderConfig | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        VisionUnderstandingTool(adapter=create_vision_adapter(config)),
        ProductSearchTool(),
        PriceCompareTool(),
        ImageGenerationTool(),
        Render3DTool(),
        MemoryTool(),
        MemoryRetrievalTool(),
        MemorySaveTool(),
    ):
        registry.register(tool)
    return registry
