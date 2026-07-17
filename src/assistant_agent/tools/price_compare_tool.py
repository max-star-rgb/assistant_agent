"""Price comparison tool backed by an adapter."""

from typing import Any

from assistant_agent.schemas.products import PriceCompareResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.services.product_adapter import (
    PriceCompareAdapter,
    PriceCompareInput,
    create_price_compare_adapter,
)
from assistant_agent.tools.base import MockTool, ToolContext


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
        model_observation = _price_compare_model_observation(data)
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
                model_observation=model_observation,
                error=result.errors[0].message,
                output_ref=result.output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )

        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=model_observation,
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )


def _price_compare_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "summary": data.get("summary"),
        "query": data.get("query"),
        "offers": [
            _offer_model_observation(offer)
            for offer in data.get("offers", [])
            if isinstance(offer, dict)
        ],
        "best_offer": (
            _offer_model_observation(data["best_offer"])
            if isinstance(data.get("best_offer"), dict)
            else None
        ),
        "best_value_product_id": data.get("best_value_product_id"),
        "ranking_reason": data.get("ranking_reason"),
    }
    errors = data.get("errors")
    if errors:
        observation["errors"] = errors
    return {
        key: value for key, value in observation.items() if value not in (None, [], {})
    }


def _offer_model_observation(offer: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "offer_id",
        "product_id",
        "title",
        "platform",
        "shop",
        "price",
        "original_price",
        "coupon_amount",
        "effective_price",
        "unconditional_price",
        "conditional_price",
        "conditional_price_note",
        "currency",
        "shipping_fee",
        "total_price",
        "product_url",
        "image_url",
        "url_status",
        "availability",
        "rating",
        "sales",
        "similarity_score",
        "comparison_group",
        "same_product_confidence",
        "data_completeness",
        "reason",
        "ranking_reason",
    )
    return {key: offer[key] for key in keys if offer.get(key) not in (None, "", [], {})}
