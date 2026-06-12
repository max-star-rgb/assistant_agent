"""Price comparison tool backed by an adapter."""

from multimodal_agent.schemas.products import PriceCompareResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.product_adapter import (
    MockProductSearchAdapter,
    PriceCompareInput,
    ProductSearchAdapter,
)
from multimodal_agent.tools.base import MockTool, ToolContext


class PriceCompareTool(MockTool):
    name = "price_compare"
    description = "Price comparison through a product adapter."
    input_schema = PriceCompareInput
    output_schema = PriceCompareResult

    def __init__(self, adapter: ProductSearchAdapter | None = None) -> None:
        self.adapter = adapter or MockProductSearchAdapter()

    def _run(self, input: PriceCompareInput, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.compare(input)
        except ValueError as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc))

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=result.model_dump(),
            output_ref="mock://compare/white-low-top-sneaker",
            latency_ms=1,
        )
