"""Price comparison tool backed by an adapter."""

from multimodal_agent.schemas.products import PriceCompareResult
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.services.product_adapter import (
    PriceCompareAdapter,
    PriceCompareInput,
    create_price_compare_adapter,
)
from multimodal_agent.tools.base import MockTool, ToolContext


class PriceCompareTool(MockTool):
    name = "price_compare"
    description = "Price comparison through a product adapter."
    input_schema = PriceCompareInput
    output_schema = PriceCompareResult

    def __init__(self, adapter: PriceCompareAdapter | None = None) -> None:
        self.adapter = adapter or create_price_compare_adapter()

    def _run(self, input: PriceCompareInput, context: ToolContext) -> ToolResult:
        result = self.adapter.compare(input)
        data = result.model_dump(mode="json")
        contract = build_capability_output_contract(
            capability="price_compare",
            status="failed" if result.errors else "succeeded",
            output_ref=result.output_ref,
            data={
                "offers": data.get("offers", []),
                "best_offer": data.get("best_offer"),
                "ranking_reason": data.get("ranking_reason"),
                "summary": result.summary,
            },
            errors=[error.model_dump(mode="json") for error in result.errors],
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )
        if result.errors:
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
