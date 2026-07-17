"""Product search tool backed by an adapter."""

from typing import Any

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
        model_observation = _product_search_model_observation(data)
        contract = build_capability_output_contract(
            capability="product_search",
            status="succeeded" if succeeded else "failed",
            output_ref=result.output_ref,
            data={
                "items": data.get("items", []),
                "query_used": result.query_used,
                "total": result.total,
            },
            errors=[error.model_dump(mode="json") for error in result.errors],
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )
        if not succeeded:
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


def _product_search_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "query_used": data.get("query_used"),
        "total": data.get("total"),
        "items": [
            _product_item_model_observation(item)
            for item in data.get("items", [])
            if isinstance(item, dict)
        ],
    }
    for key in (
        "requested_platforms",
        "succeeded_platforms",
        "failed_platforms",
        "errors",
    ):
        value = data.get(key)
        if value:
            observation[key] = value
    return {
        key: value for key, value in observation.items() if value not in (None, [], {})
    }


def _product_item_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "product_id",
        "title",
        "brand",
        "category",
        "price",
        "original_price",
        "coupon_amount",
        "effective_price",
        "unconditional_price",
        "conditional_price",
        "conditional_price_note",
        "currency",
        "platform",
        "shop",
        "url",
        "product_url",
        "url_status",
        "availability",
        "image_url",
        "model",
        "specifications",
        "similarity",
        "similarity_score",
        "text_match_score",
        "rating",
        "sales",
        "material",
        "color",
        "style_tags",
        "reason",
        "ranking_reason",
    )
    return {key: item[key] for key in keys if item.get(key) not in (None, "", [], {})}
