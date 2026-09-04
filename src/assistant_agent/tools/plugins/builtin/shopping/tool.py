"""Unified native shopping search for one or more product needs."""

from itertools import product
from math import isfinite
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.ids import (
    SHOPPING_SEARCH_TOOL_NAME,
)
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
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)
from assistant_agent.tools.delivery import safe_http_url, with_tool_delivery


_PLATFORM_LABELS = {
    "jd": "京东",
    "jingdong": "京东",
    "taobao": "淘宝",
    "tmall": "天猫",
    "pdd": "拼多多",
}


def create_shopping_search_tool(
    *,
    search_adapter: ProductSearchAdapter,
    compare_adapter: PriceCompareAdapter,
) -> BaseTool:
    """Create a native read-only shopping search Tool."""

    @tool(SHOPPING_SEARCH_TOOL_NAME, response_format="content_and_artifact")
    def shopping_search(
        queries: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=8,
                description="需要分别搜索的商品查询，最多八项。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """针对一个或多个商品查询检索淘宝候选、比较价格与购买链接。

        每个查询独立检索并返回候选和选择。只读，不加入购物车、下单或付款。

        优先采用本次检索得到的商品信息，其他渠道例如网络搜索仅可作为补充。
        """

        try:
            result = _execute_shopping_search(
                search_adapter,
                compare_adapter,
                ShoppingSearchRequest(
                    needs=[ShoppingListNeed(keyword=query) for query in queries],
                ),
            )
            if not result.success:
                error = result.errors[0].message if result.errors else result.summary
                raise RuntimeError(error)
            data = result.model_dump(mode="json")
            if delivery := _shopping_delivery(result):
                data = with_tool_delivery(data, text=delivery)
            return native_content_and_artifact(_model_observation(data), data)
        except Exception as exc:
            raise native_tool_exception(exc) from exc

    return configure_builtin_tool(shopping_search)


def _shopping_delivery(result: ShoppingSearchResult, *, max_items: int = 3) -> str:
    lines: list[str] = []
    for selection in result.selections:
        product = selection.product
        product_url = product.product_url or product.url
        if not safe_http_url(product_url) or not isfinite(selection.subtotal):
            continue
        title = " ".join(product.title.replace("<", "").replace(">", "").split())[:240]
        platform = " ".join(product.platform.replace("<", "").replace(">", "").split())[
            :80
        ]
        platform = _PLATFORM_LABELS.get(platform.casefold(), platform)
        price = f"{selection.subtotal:.2f}".rstrip("0").rstrip(".")
        image = (
            f"<pic>{product.image_url}</pic>"
            if safe_http_url(product.image_url)
            else ""
        )
        if title and platform:
            lines.append(
                f"{len(lines) + 1}. {platform} - {title} {price}元 "
                f"<link>{product_url}</link>{image}"
            )
        if len(lines) >= max_items:
            break
    return "<detail>\n" + "\n".join(lines) + "\n</detail>" if lines else ""


def _execute_shopping_search(
    search_adapter: ProductSearchAdapter,
    compare_adapter: PriceCompareAdapter,
    input: ShoppingSearchRequest,
) -> ShoppingSearchResult:
    searches: list[ProductSearchResult] = []
    comparisons: list[PriceCompareResult | None] = []
    for need in input.needs:
        search = search_adapter.search(
            ProductSearchRequest(
                query=need.keyword,
                budget_max=need.max_unit_price,
                platforms=input.platforms,
                top_k=input.top_k_per_need,
            )
        )
        searches.append(search)
        comparisons.append(
            compare_adapter.compare(
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
    return result


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
            _selection(need, selected_product) if selected_product is not None else None
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
    within_budget = request.total_budget is None or total_cost <= request.total_budget
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
    best_score: tuple[int, int, int, float] = (-1, -1, -(10**9), float("-inf"))
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
            f"已在 {total_budget:.2f} 元总预算内" if total_budget is not None else "已"
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
    need_results = data.get("needs")
    if not isinstance(need_results, list):
        need_results = []
    selections = data.get("selections")
    if not isinstance(selections, list):
        selections = []
    budget = {
        "total_budget": data.get("total_budget"),
        "total_cost": data.get("total_cost"),
        "currency": _selection_currency(selections),
        "within_budget": data.get("within_budget"),
    }
    observation: dict[str, Any] = {
        "schema_version": "shopping_observation_v1",
        "outcome": data.get("outcome"),
        "summary": data.get("summary"),
        "budget": {key: value for key, value in budget.items() if value is not None},
        "results": [
            _need_result_observation(item)
            for item in need_results
            if isinstance(item, dict)
        ],
    }
    uncovered_required_needs = data.get("uncovered_required_needs")
    if uncovered_required_needs:
        observation["uncovered_required_needs"] = uncovered_required_needs
    errors = data.get("errors")
    if isinstance(errors, list):
        warnings = [
            _warning_observation(item) for item in errors if isinstance(item, dict)
        ]
        if warnings:
            observation["warnings"] = warnings
    return {
        key: value for key, value in observation.items() if value not in (None, [], {})
    }


def _need_result_observation(need_result: dict[str, Any]) -> dict[str, Any]:
    need = need_result.get("need")
    if not isinstance(need, dict):
        need = {}
    selected = need_result.get("selected")
    if not isinstance(selected, dict):
        selected = None
    selected_product = (
        selected.get("product")
        if selected is not None and isinstance(selected.get("product"), dict)
        else {}
    )
    selected_product_id = selected_product.get("product_id")
    candidates = need_result.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    quantity = need.get("quantity")
    result: dict[str, Any] = {
        "need": _compact_fields(
            need,
            ("keyword", "quantity", "required", "max_unit_price"),
        ),
        "status": need_result.get("status"),
        "selected": (
            _selection_observation(selected) if selected is not None else None
        ),
        "alternatives": [
            _product_observation(item, quantity=quantity)
            for item in candidates
            if isinstance(item, dict) and item.get("product_id") != selected_product_id
        ][:2],
    }
    return {key: value for key, value in result.items() if value not in (None, [], {})}


def _selection_observation(selection: dict[str, Any]) -> dict[str, Any]:
    product = selection.get("product")
    if not isinstance(product, dict):
        product = {}
    result = _product_observation(product, quantity=selection.get("quantity"))
    result["unit_price"] = selection.get("unit_price")
    result["subtotal"] = selection.get("subtotal")
    return {key: value for key, value in result.items() if value is not None}


def _product_observation(
    product: dict[str, Any],
    *,
    quantity: Any,
) -> dict[str, Any]:
    unit_price = product.get("effective_price")
    if unit_price is None:
        unit_price = product.get("price")
    subtotal = (
        round(unit_price * quantity, 2)
        if isinstance(unit_price, (int, float)) and isinstance(quantity, int)
        else None
    )
    ranking_reason = product.get("ranking_reason")
    ranking_explanation = (
        ranking_reason.get("explanation") if isinstance(ranking_reason, dict) else None
    )
    return {
        key: value
        for key, value in {
            "product_id": product.get("product_id"),
            "title": product.get("title"),
            "platform": product.get("platform"),
            "shop": product.get("shop"),
            "unit_price": unit_price,
            "quantity": quantity,
            "subtotal": subtotal,
            "currency": product.get("currency"),
            "product_url": (
                product.get("product_url")
                or product.get("landing_url")
                or product.get("click_url")
                or product.get("url")
            ),
            "url_status": product.get("url_status"),
            "availability": product.get("availability"),
            "reason": product.get("reason") or ranking_explanation,
        }.items()
        if value not in (None, "", [], {})
    }


def _selection_currency(selections: list[Any]) -> str | None:
    currencies = {
        product.get("currency")
        for selection in selections
        if isinstance(selection, dict)
        and isinstance((product := selection.get("product")), dict)
        and product.get("currency")
    }
    return next(iter(currencies)) if len(currencies) == 1 else None


def _warning_observation(error: dict[str, Any]) -> dict[str, Any]:
    return _compact_fields(error, ("code", "message", "recoverable"))


def _compact_fields(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: data[field]
        for field in fields
        if field in data and data[field] not in (None, "", [], {})
    }
