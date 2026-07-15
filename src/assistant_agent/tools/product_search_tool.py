"""Product search tool backed by an adapter."""

from assistant_agent.schemas.products import ProductSearchResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.services.product_adapter import (
    ProductSearchAdapter,
    ProductSearchInput,
    create_product_search_adapter,
)
from assistant_agent.tools.base import MockTool, ToolContext


class ProductSearchTool(MockTool):
    name = "product_search"
    description = "Product search through a product adapter."
    input_schema = ProductSearchInput
    output_schema = ProductSearchResult

    def __init__(self, adapter: ProductSearchAdapter | None = None) -> None:
        self.adapter = adapter or create_product_search_adapter()

    def _run(self, input: ProductSearchInput, context: ToolContext) -> ToolResult:
        result = self.adapter.search(input)
        data = result.model_dump(mode="json")
        succeeded = result.success
        contract = build_capability_output_contract(
            capability="product_search",
            status="succeeded" if succeeded else "failed",
            output_ref=result.output_ref,
            data={"items": data.get("items", []), "query_used": result.query_used, "total": result.total},
            errors=[error.model_dump(mode="json") for error in result.errors],
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )
        if not succeeded:
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                error=result.errors[0].message,
                output_ref=result.output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )
