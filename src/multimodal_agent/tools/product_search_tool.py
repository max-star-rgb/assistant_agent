"""Product search tool backed by an adapter."""

from multimodal_agent.schemas.products import ProductResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.product_adapter import (
    MockProductSearchAdapter,
    ProductSearchAdapter,
    ProductSearchInput,
)
from multimodal_agent.tools.base import MockTool, ToolContext


class ProductSearchTool(MockTool):
    name = "product_search"
    description = "Product search through a product adapter."
    input_schema = ProductSearchInput
    output_schema = ProductResult

    def __init__(self, adapter: ProductSearchAdapter | None = None) -> None:
        self.adapter = adapter or MockProductSearchAdapter()

    def _run(self, input: ProductSearchInput, context: ToolContext) -> ToolResult:
        try:
            products = self.adapter.search(input)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc))

        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"items": [product.model_dump() for product in products]},
            output_ref="mock://products/white-low-top-sneaker",
            latency_ms=1,
        )
