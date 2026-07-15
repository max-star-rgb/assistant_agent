"""Unified shopping tool that searches products and compares prices."""

from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.schemas.products import (
    PriceCompareInput,
    PriceCompareResult,
    ProductProviderError,
    ProductSearchInput,
    ProductSearchResult,
    ShoppingSearchResult,
)
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.product_adapter import (
    PriceCompareAdapter,
    ProductSearchAdapter,
    create_price_compare_adapter,
    create_product_search_adapter,
)
from assistant_agent.tools.base import MockTool, ToolContext


class ShoppingSearchTool(MockTool):
    """Search current product candidates and compare their offers in one call."""

    name = "shopping_search"
    description = (
        "Search for current shopping candidates and compare prices/offers in one call. "
        "Use this for product recommendations, purchase advice, price checks, and value comparisons."
    )
    input_schema = ProductSearchInput
    output_schema = ShoppingSearchResult

    def __init__(
        self,
        *,
        search_adapter: ProductSearchAdapter | None = None,
        price_compare_adapter: PriceCompareAdapter | None = None,
    ) -> None:
        self.search_adapter = search_adapter or create_product_search_adapter()
        self.price_compare_adapter = price_compare_adapter or create_price_compare_adapter()

    def _run(self, input: ProductSearchInput, context: ToolContext) -> ToolResult:
        search_result = self.search_adapter.search(input)
        comparison_result: PriceCompareResult | None = None
        if search_result.items:
            comparison_result = self.price_compare_adapter.compare(
                _compare_input_from_search(input, search_result)
            )

        result = _shopping_result(input, search_result, comparison_result)
        data = result.model_dump(mode="json")
        errors = [error.model_dump(mode="json") for error in result.errors]
        contract = build_capability_output_contract(
            capability="shopping_search",
            status="succeeded" if result.success else "failed",
            output_ref=result.output_ref,
            data={
                "query": result.query,
                "search": {
                    "items": data.get("search", {}).get("items", []),
                    "query_used": data.get("search", {}).get("query_used"),
                    "total": data.get("search", {}).get("total"),
                    "requested_platforms": data.get("search", {}).get("requested_platforms", []),
                    "succeeded_platforms": data.get("search", {}).get("succeeded_platforms", []),
                    "failed_platforms": data.get("search", {}).get("failed_platforms", []),
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
                error=first_error,
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


def _compare_input_from_search(
    input: ProductSearchInput,
    search_result: ProductSearchResult,
) -> PriceCompareInput:
    platforms = search_result.succeeded_platforms or input.platforms
    return PriceCompareInput(
        items=search_result.items,
        query=search_result.query_used or _query_text(input) or "shopping_search",
        budget_min=input.budget_min,
        budget_max=input.budget_max if input.budget_max is not None else input.budget,
        platforms=platforms,
        sort_by="value",
        top_k=input.top_k,
        user_id=input.user_id,
        session_id=input.session_id,
    )


def _shopping_result(
    input: ProductSearchInput,
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> ShoppingSearchResult:
    errors = _combined_errors(search_result, comparison_result)
    query = (
        search_result.query_used
        or (comparison_result.query if comparison_result is not None else None)
        or _query_text(input)
        or "shopping_search"
    )
    items = comparison_result.items if comparison_result is not None else search_result.items
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
        query=query,
        search=search_result,
        comparison=comparison_result,
        items=items,
        offers=offers,
        best_offer=comparison_result.best_offer if comparison_result is not None else None,
        best_value_product_id=(
            comparison_result.best_value_product_id if comparison_result is not None else None
        ),
        ranking_reason=comparison_result.ranking_reason if comparison_result is not None else None,
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
    elif not search_result.items and not search_result.errors:
        errors.append(
            ProductProviderError(
                code="price_no_products",
                message="没有商品候选，无法比价",
                recoverable=True,
            )
        )
    return errors


def _shopping_summary(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
    errors: list[ProductProviderError],
) -> str:
    if comparison_result is not None and comparison_result.best_offer is not None:
        return comparison_result.summary
    if errors:
        return errors[0].message
    return f"已搜索到 {search_result.total} 个商品候选，但没有可比价报价。"


def _combined_provider(
    search_result: ProductSearchResult,
    comparison_result: PriceCompareResult | None,
) -> str:
    if comparison_result is None or comparison_result.provider == search_result.provider:
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


def _query_text(input: ProductSearchInput) -> str:
    parts = [
        input.query,
        input.visual_summary,
        input.video_summary,
        " ".join(input.objects),
        " ".join(input.colors),
        " ".join(input.materials),
        input.brand,
        input.category,
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())
