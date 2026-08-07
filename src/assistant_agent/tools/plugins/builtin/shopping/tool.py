"""Unified real-provider shopping search for one or more product needs."""

from itertools import product
from typing import Any

from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.ids import SHOPPING_SEARCH_CAPABILITY, SHOPPING_SEARCH_TOOL_NAME
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.shopping.backend import (
    PriceCompareAdapter,
    ProductSearchAdapter,
)
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareRequest,
    PriceCompareResult,
    ProductProviderError,
    ProductResult,
    ProductSearchRequest,
    ProductSearchResult,
    ShoppingListNeed,
    ShoppingListNeedResult,
    ShoppingListSelection,
    ShoppingSearchRequest,
    ShoppingSearchResult,
)


class ShoppingSearchTool(ToolBase):
    """Search, compare, and budget one or more product needs."""

    name = SHOPPING_SEARCH_TOOL_NAME
    description = (
        "针对一个或多个明确商品需求检索候选、比较价格与购买链接，并按数量、单件"
        "上限和总预算选择组合；返回各需求的候选、选择、未覆盖项和预算结果。只读，"
        "不加入购物车、下单或付款。"
    )
    input_schema = ShoppingSearchRequest
    output_schema = ShoppingSearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    llm_hidden_input_fields = ("top_k_per_need",)

    def __init__(
        self,
        *,
        search_adapter: ProductSearchAdapter,
        compare_adapter: PriceCompareAdapter,
    ) -> None:
        self.search_adapter = search_adapter
        self.compare_adapter = compare_adapter

    def _run(self, input: ShoppingSearchRequest, context: ToolContext) -> ToolResult:
        searches: list[ProductSearchResult] = []
        comparisons: list[PriceCompareResult | None] = []
        for need in input.needs:
            search = self.search_adapter.search(
                ProductSearchRequest(
                    query=need.keyword,
                    budget_max=need.max_unit_price,
                    platforms=input.platforms,
                    top_k=input.top_k_per_need,
                )
            )
            searches.append(search)
            comparisons.append(
                self.compare_adapter.compare(
                    PriceCompareRequest(
                        items=search.items,
                        query=search.query_used or need.keyword,
                        budget_max=need.max_unit_price,
                        platforms=search.succeeded_platforms or input.platforms,
                        sort_by="value",
                        top_k=input.top_k_per_need,
                    )
                )
                if search.items
                else None
            )

        result = _build_result(input, searches, comparisons)
        data = result.model_dump(mode="json")
        errors = [error.model_dump(mode="json") for error in result.errors]
        output_ref = result.output_refs[0] if result.output_refs else None
        contract = build_capability_output_contract(
            capability=SHOPPING_SEARCH_CAPABILITY,
            status="succeeded" if result.success else "failed",
            output_ref=output_ref,
            data=data,
            errors=errors,
            metadata={
                "provider": result.provider,
                "latency_ms": result.latency_ms,
                "query_count": len(input.needs),
            },
        )
        observation = _model_observation(data)
        if not result.success:
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                model_observation=observation,
                error=errors[0]["message"] if errors else result.summary,
                output_ref=output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=observation,
            output_ref=output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )


def _build_result(
    request: ShoppingSearchRequest,
    searches: list[ProductSearchResult],
    comparisons: list[PriceCompareResult | None],
) -> ShoppingSearchResult:
    candidate_groups = [
        _eligible_candidates(
            need,
            search,
            comparison,
            request.top_k_per_need,
        )
        for need, search, comparison in zip(
            request.needs,
            searches,
            comparisons,
            strict=True,
        )
    ]
    chosen = _choose_basket(
        request.needs,
        candidate_groups,
        request.total_budget,
    )
    selections: list[ShoppingListSelection] = []
    need_results: list[ShoppingListNeedResult] = []
    errors: list[ProductProviderError] = []
    for need, search, comparison, candidates, selected_product in zip(
        request.needs,
        searches,
        comparisons,
        candidate_groups,
        chosen,
        strict=True,
    ):
        need_errors = [
            *search.errors,
            *(comparison.errors if comparison is not None else []),
        ]
        errors.extend(need_errors)
        selection = (
            _selection(need, selected_product)
            if selected_product is not None
            else None
        )
        if selection is not None:
            selections.append(selection)
            status = "selected"
        elif need_errors and not candidates:
            status = "failed"
        elif candidates:
            status = "budget_excluded"
        else:
            status = "empty"
        need_results.append(
            ShoppingListNeedResult(
                need=need,
                status=status,
                query_used=search.query_used,
                candidates=candidates,
                selected=selection,
                errors=need_errors,
            )
        )

    total_cost = round(sum(item.subtotal for item in selections), 2)
    uncovered = [
        item.need.keyword
        for item in need_results
        if item.need.required and item.selected is None
    ]
    all_searches_failed = bool(searches) and all(
        search.errors and not search.items for search in searches
    )
    if all_searches_failed:
        outcome = "failed"
    elif not selections:
        outcome = "empty"
    elif uncovered or errors:
        outcome = "partial"
    else:
        outcome = "success"
    providers = [
        provider
        for search, comparison in zip(searches, comparisons, strict=True)
        for provider in (
            search.provider,
            comparison.provider if comparison is not None else None,
        )
        if provider
    ]
    latencies = [
        latency
        for search, comparison in zip(searches, comparisons, strict=True)
        for latency in (
            search.latency_ms,
            comparison.latency_ms if comparison is not None else None,
        )
        if latency is not None
    ]
    output_refs = [
        output_ref
        for search, comparison in zip(searches, comparisons, strict=True)
        for output_ref in (
            search.output_ref,
            comparison.output_ref if comparison is not None else None,
        )
        if output_ref
    ]
    within_budget = (
        request.total_budget is None or total_cost <= request.total_budget
    )
    return ShoppingSearchResult(
        outcome=outcome,
        scenario=request.scenario,
        decision_reason=request.decision_reason,
        evidence=request.evidence,
        total_budget=request.total_budget,
        total_cost=total_cost,
        within_budget=within_budget,
        needs=need_results,
        selections=selections,
        uncovered_required_needs=uncovered,
        summary=_summary(
            outcome,
            selections,
            uncovered,
            request.total_budget,
        ),
        provider="+".join(dict.fromkeys(providers)),
        errors=errors,
        latency_ms=sum(latencies) if latencies else None,
        output_refs=list(dict.fromkeys(output_refs)),
    )


def _eligible_candidates(
    need: ShoppingListNeed,
    search: ProductSearchResult,
    comparison: PriceCompareResult | None,
    limit: int,
) -> list[ProductResult]:
    candidates = _comparison_enriched_products(search.items, comparison)
    return [
        item
        for item in candidates
        if need.max_unit_price is None or _unit_price(item) <= need.max_unit_price
    ][:limit]


def _comparison_enriched_products(
    items: list[ProductResult],
    comparison: PriceCompareResult | None,
) -> list[ProductResult]:
    if comparison is None:
        return list(items)
    offers = {offer.product_id: offer for offer in comparison.offers}
    enriched = [
        item.model_copy(
            update={
                "product_url": offer.product_url or item.product_url,
                "image_url": offer.image_url or item.image_url,
                "url_status": offer.url_status or item.url_status,
                "availability": offer.availability or item.availability,
                "effective_price": (
                    offer.effective_price
                    if offer.effective_price is not None
                    else item.effective_price
                ),
            }
        )
        if (offer := offers.get(item.product_id)) is not None
        else item
        for item in (comparison.items or items)
    ]
    best_product_id = (
        comparison.best_offer.product_id
        if comparison.best_offer is not None
        else comparison.best_value_product_id
    )
    if best_product_id is None:
        return enriched
    return sorted(
        enriched,
        key=lambda item: item.product_id != best_product_id,
    )


def _choose_basket(
    needs: list[ShoppingListNeed],
    candidate_groups: list[list[ProductResult]],
    total_budget: float | None,
) -> tuple[ProductResult | None, ...]:
    choices = [(*candidates, None) for candidates in candidate_groups]
    best: tuple[ProductResult | None, ...] = tuple(None for _ in needs)
    best_score: tuple[int, int, int, float] = (-1, -1, -10**9, float("-inf"))
    for combination in product(*choices):
        total = sum(
            _unit_price(item) * need.quantity
            for need, item in zip(needs, combination, strict=True)
            if item is not None
        )
        if total_budget is not None and total > total_budget:
            continue
        required_coverage = sum(
            item is not None
            for need, item in zip(needs, combination, strict=True)
            if need.required
        )
        total_coverage = sum(item is not None for item in combination)
        rank_cost = sum(
            candidate_groups[index].index(item)
            for index, item in enumerate(combination)
            if item is not None
        )
        score = (required_coverage, total_coverage, -rank_cost, -total)
        if score > best_score:
            best = combination
            best_score = score
    return best


def _selection(
    need: ShoppingListNeed,
    selected: ProductResult,
) -> ShoppingListSelection:
    unit_price = _unit_price(selected)
    return ShoppingListSelection(
        keyword=need.keyword,
        quantity=need.quantity,
        product=selected,
        unit_price=unit_price,
        subtotal=round(unit_price * need.quantity, 2),
    )


def _unit_price(item: ProductResult) -> float:
    return item.effective_price if item.effective_price is not None else item.price


def _summary(
    outcome: str,
    selections: list[ShoppingListSelection],
    uncovered: list[str],
    total_budget: float | None,
) -> str:
    if outcome == "failed":
        return "所有商品需求的真实 Provider 搜索均失败，未生成购物组合。"
    if not selections:
        if total_budget is None:
            return "未找到符合条件的商品。"
        return f"没有候选商品能组成不超过 {total_budget:.2f} 元的购物组合。"
    total = sum(item.subtotal for item in selections)
    if uncovered:
        prefix = (
            f"已在 {total_budget:.2f} 元总预算内"
            if total_budget is not None
            else "已"
        )
        return (
            f"{prefix}选出 {len(selections)} 项，合计 {total:.2f} 元；"
            f"仍缺少：{'、'.join(uncovered)}。"
        )
    if total_budget is None:
        return f"已选出 {len(selections)} 项商品候选，合计 {total:.2f} 元。"
    return (
        f"已在 {total_budget:.2f} 元总预算内覆盖全部必需品类，"
        f"共 {len(selections)} 项，合计 {total:.2f} 元。"
    )


def _model_observation(data: dict[str, Any]) -> dict[str, Any]:
    selections = data.get("selections")
    if not isinstance(selections, list):
        selections = []
    items = [
        _selection_observation(selection)
        for selection in selections[:3]
        if isinstance(selection, dict)
    ]
    observation: dict[str, Any] = {
        "outcome": data.get("outcome"),
        "total_cost": data.get("total_cost"),
        "within_budget": data.get("within_budget"),
        "summary": data.get("summary"),
        "items": items,
    }
    uncovered_required_needs = data.get("uncovered_required_needs")
    if uncovered_required_needs:
        observation["uncovered_required_needs"] = uncovered_required_needs
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, [], {})
    }


def _selection_observation(selection: dict[str, Any]) -> dict[str, Any]:
    item = (
        selection.get("product")
        if isinstance(selection.get("product"), dict)
        else {}
    )
    return {
        key: value
        for key, value in {
            "product_id": item.get("product_id"),
            "need": selection.get("keyword"),
            "title": item.get("title"),
            "platform": item.get("platform"),
            "shop": item.get("shop"),
            "quantity": selection.get("quantity"),
            "total_price": selection.get("subtotal"),
            "currency": item.get("currency"),
            "url_status": item.get("url_status"),
            "availability": item.get("availability"),
        }.items()
        if value not in (None, "", [], {})
    }
