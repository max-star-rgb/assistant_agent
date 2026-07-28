"""Unified shopping Tool that searches products and compares prices."""

from typing import Any

from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareRequest,
    PriceCompareResult,
    ProductProviderError,
    ShoppingSearchConstraints,
    ShoppingSearchOutcome,
    ShoppingSearchRequest,
    ProductSearchResult,
    ShoppingSearchResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.shopping.backend import (
    PriceCompareAdapter,
    ProductSearchAdapter,
    create_shopping_compare_adapter,
    create_shopping_search_adapter,
)
from assistant_agent.tools.ids import SHOPPING_SEARCH_CAPABILITY, SHOPPING_SEARCH_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext


class ShoppingSearchTool(ToolBase):
    """Search current product candidates and compare their offers in one call."""

    name = SHOPPING_SEARCH_TOOL_NAME
    description = (
        "搜索、推荐和比较商品及购买链接；仅表达购买意向时先追问。不能下单。"
    )
    input_schema = ShoppingSearchRequest
    output_schema = ShoppingSearchResult
    category = "read"
    llm_hidden_input_fields = ("platforms", "top_k")

    def __init__(
        self,
        *,
        search_adapter: ProductSearchAdapter | None = None,
        compare_adapter: PriceCompareAdapter | None = None,
    ) -> None:
        self.search_adapter = search_adapter or create_shopping_search_adapter()
        self.compare_adapter = compare_adapter or create_shopping_compare_adapter()

    def _run(self, input: ShoppingSearchRequest, context: ToolContext) -> ToolResult:
        search_result = self.search_adapter.search(input)
        comparison_result: PriceCompareResult | None = None
        if search_result.items:
            comparison_result = self.compare_adapter.compare(
                _compare_input_from_search(input, search_result)
            )

        result = _shopping_result(input, search_result, comparison_result)
        data = result.model_dump(mode="json")
        errors = [error.model_dump(mode="json") for error in result.errors]
        model_observation = _shopping_search_model_observation(data)
        contract = build_capability_output_contract(
            capability=SHOPPING_SEARCH_CAPABILITY,
            status="succeeded" if result.success else "failed",
            output_ref=result.output_ref,
            data={
                "outcome": result.outcome,
                "query": result.query,
                "requested_constraints": result.requested_constraints.model_dump(
                    mode="json"
                ),
                "search": {
                    "items": data.get("search", {}).get("items", []),
                    "query_used": data.get("search", {}).get("query_used"),
                    "total": data.get("search", {}).get("total"),
                    "requested_platforms": data.get("search", {}).get(
                        "requested_platforms", []
                    ),
                    "succeeded_platforms": data.get("search", {}).get(
                        "succeeded_platforms", []
                    ),
                    "failed_platforms": data.get("search", {}).get(
                        "failed_platforms", []
                    ),
                },
                "comparison": data.get("comparison"),
                "offers": data.get("offers", []),
                "best_offer": data.get("best_offer"),
                "ranking_reason": data.get("ranking_reason"),
                "summary": result.summary,
            },
            errors=errors,
            metadata={"provider": result.provider, "latency_ms": result.latency_ms},
        )
        if not result.success:
            first_error = result.errors[0].message if result.errors else result.summary
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                model_observation=model_observation,
                error=first_error,
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


def _compare_input_from_search(
    input: ShoppingSearchRequest,
    search_result: ProductSearchResult,
) -> PriceCompareRequest:
    platforms = search_result.succeeded_platforms or input.platforms
    return PriceCompareRequest(
        items=search_result.items,
        query=search_result.query_used or _query_text(input) or SHOPPING_SEARCH_CAPABILITY,
        budget_min=input.budget_min,
        budget_max=input.budget_max,
        platforms=platforms,
        sort_by="value",
        top_k=input.top_k,
    )


def _shopping_result(
    input: ShoppingSearchRequest,
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> ShoppingSearchResult:
    errors = _combined_errors(search_result, comparison_result)
    query = (
        search_result.query_used
        or (comparison_result.query if comparison_result is not None else None)
        or _query_text(input)
        or SHOPPING_SEARCH_CAPABILITY
    )
    items = (
        comparison_result.items
        if comparison_result is not None
        else search_result.items
    )
    offers = comparison_result.offers if comparison_result is not None else []
    provider = _combined_provider(search_result, comparison_result)
    latency_ms = _combined_latency(search_result, comparison_result)
    output_ref = (
        comparison_result.output_ref
        if comparison_result is not None and comparison_result.output_ref
        else search_result.output_ref
    )
    summary = _shopping_summary(search_result, comparison_result, errors)
    return ShoppingSearchResult(
        outcome=_shopping_outcome(search_result, comparison_result),
        query=query,
        requested_constraints=ShoppingSearchConstraints(
            budget_min=input.budget_min,
            budget_max=input.budget_max,
            platforms=input.platforms,
        ),
        search=search_result,
        comparison=comparison_result,
        items=items,
        offers=offers,
        best_offer=comparison_result.best_offer
        if comparison_result is not None
        else None,
        best_value_product_id=(
            comparison_result.best_value_product_id
            if comparison_result is not None
            else None
        ),
        ranking_reason=comparison_result.ranking_reason
        if comparison_result is not None
        else None,
        summary=summary,
        provider=provider,
        errors=errors,
        latency_ms=latency_ms,
        output_ref=output_ref,
    )


def _combined_errors(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> list[ProductProviderError]:
    errors = [*search_result.errors]
    if comparison_result is not None:
        errors.extend(comparison_result.errors)
    return errors


def _shopping_outcome(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> ShoppingSearchOutcome:
    if not search_result.items:
        return "failed" if search_result.errors else "empty"
    if (
        search_result.errors
        or comparison_result is None
        or not comparison_result.success
    ):
        return "partial"
    return "success"


def _shopping_summary(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
    errors: list[ProductProviderError],
) -> str:
    if comparison_result is not None and comparison_result.best_offer is not None:
        return comparison_result.summary
    if search_result.items and errors:
        return (
            f"已取得 {len(search_result.items)} 个商品候选，"
            f"但部分搜索或比价失败：{errors[0].message}"
        )
    if errors:
        return errors[0].message
    if not search_result.items:
        return "未找到符合条件的商品。"
    return f"已搜索到 {search_result.total} 个商品候选，但没有可比价报价。"


def _combined_provider(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> str:
    if (
        comparison_result is None
        or comparison_result.provider == search_result.provider
    ):
        return search_result.provider
    return f"{search_result.provider}+{comparison_result.provider}"


def _combined_latency(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> int | None:
    values = [
        value
        for value in (
            search_result.latency_ms,
            comparison_result.latency_ms if comparison_result is not None else None,
        )
        if value is not None
    ]
    return sum(values) if values else None


def _query_text(input: ShoppingSearchRequest) -> str:
    return input.query.strip()


def _shopping_search_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    requested_constraints = (
        data.get("requested_constraints")
        if isinstance(data.get("requested_constraints"), dict)
        else {}
    )
    offers = [
        _shopping_offer_model_observation(offer)
        for offer in data.get("offers", [])[:3]
        if isinstance(offer, dict)
    ]
    items = offers or [
        _shopping_item_model_observation(item)
        for item in search.get("items", [])[:3]
        if isinstance(item, dict)
    ]
    observation: dict[str, Any] = {
        "outcome": data.get("outcome"),
        "query": data.get("query"),
        "requested_constraints": {
            key: value
            for key, value in requested_constraints.items()
            if value not in (None, [], {})
        },
        "total": search.get("total"),
        "items": items,
        "best_item_id": (
            data.get("best_offer", {}).get("offer_id")
            or data.get("best_value_product_id")
            if isinstance(data.get("best_offer"), dict)
            else data.get("best_value_product_id")
        ),
        "ranking_reason": data.get("ranking_reason"),
        "response_contract": {
            "type": "shopping_detail_v1",
            "max_items": 3,
            "wrapper": "{summary}\n<detail>\n{items}\n</detail>",
            "item_template": (
                "{index}. {platform} - {title} {total_price}元 "
                "<link>{product_url}</link> <pic>{image_url}</pic>"
            ),
            "required_item_fields": ["total_price", "product_url", "image_url"],
            "fallback": "无合格商品时仅自然语言回答，不输出 <detail>。",
            "rules": ["仅使用 data 中的真实字段", "不得声称已下单"],
        },
    }
    if not items:
        observation["summary"] = data.get("summary")
    errors = data.get("errors")
    if errors:
        observation["errors"] = errors
    return {
        key: value for key, value in observation.items() if value not in (None, [], {})
    }


def _shopping_item_model_observation(item: dict[str, Any]) -> dict[str, Any]:
    observation = {
        key: item[key]
        for key in (
            "product_id",
            "title",
            "platform",
            "shop",
            "effective_price",
            "currency",
            "url_status",
            "availability",
            "image_url",
            "reason",
        )
        if item.get(key) not in (None, "", [], {})
    }
    product_url = item.get("product_url") or item.get("url")
    if product_url:
        observation["product_url"] = product_url
    total_price = next(
        (
            value
            for value in (
                item.get("total_price"),
                item.get("effective_price"),
                item.get("price"),
            )
            if value is not None
        ),
        None,
    )
    if total_price is not None:
        observation["total_price"] = total_price
    return observation


def _shopping_offer_model_observation(offer: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "offer_id",
        "product_id",
        "title",
        "platform",
        "shop",
        "total_price",
        "currency",
        "product_url",
        "url_status",
        "availability",
        "image_url",
        "reason",
    )
    observation = {
        key: offer[key]
        for key in keys
        if offer.get(key) not in (None, "", [], {})
    }
    total_price = next(
        (
            value
            for value in (
                offer.get("total_price"),
                offer.get("effective_price"),
                offer.get("price"),
            )
            if value is not None
        ),
        None,
    )
    if total_price is not None:
        observation["total_price"] = total_price
    return observation
