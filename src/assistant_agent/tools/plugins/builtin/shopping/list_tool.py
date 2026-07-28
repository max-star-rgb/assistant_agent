"""Multi-category shopping-list search with a total basket budget."""

from itertools import product
from typing import Any

from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.capability_output import build_capability_output_contract
from assistant_agent.tools.ids import (
    SHOPPING_LIST_SEARCH_CAPABILITY,
    SHOPPING_LIST_SEARCH_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.shopping.backend import (
    ProductSearchAdapter,
    create_shopping_search_adapter,
)
from assistant_agent.tools.plugins.builtin.shopping.models import (
    ProductResult,
    ProductSearchResult,
    ShoppingListNeed,
    ShoppingListNeedResult,
    ShoppingListSearchRequest,
    ShoppingListSearchResult,
    ShoppingListSelection,
    ShoppingSearchRequest,
)


class ShoppingListSearchTool(ToolBase):
    """Search each product category separately and compose an affordable basket."""

    name = SHOPPING_LIST_SEARCH_TOOL_NAME
    description = (
        "搜索包含多个不同商品品类的购物清单；逐项检索并在整份清单总预算内组合。"
        "单一商品搜索请使用 shopping_search。不能下单。"
    )
    input_schema = ShoppingListSearchRequest
    output_schema = ShoppingListSearchResult
    category = "read"
    llm_hidden_input_fields = ("platforms", "top_k_per_need")

    def __init__(self, *, search_adapter: ProductSearchAdapter | None = None) -> None:
        self.search_adapter = search_adapter or create_shopping_search_adapter()

    def _run(
        self,
        input: ShoppingListSearchRequest,
        context: ToolContext,
    ) -> ToolResult:
        searches = [
            self.search_adapter.search(
                ShoppingSearchRequest(
                    query=need.keyword,
                    budget_max=need.max_unit_price,
                    platforms=input.platforms,
                    top_k=input.top_k_per_need,
                )
            )
            for need in input.needs
        ]
        result = _build_list_result(input, searches)
        data = result.model_dump(mode="json")
        errors = [error.model_dump(mode="json") for error in result.errors]
        contract = build_capability_output_contract(
            capability=SHOPPING_LIST_SEARCH_CAPABILITY,
            status="succeeded" if result.success else "failed",
            output_ref=result.output_refs[0] if result.output_refs else None,
            data=data,
            errors=errors,
            metadata={
                "provider": result.provider,
                "latency_ms": result.latency_ms,
                "query_count": len(input.needs),
            },
        )
        observation = _list_model_observation(data)
        if not result.success:
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                model_observation=observation,
                error=errors[0]["message"] if errors else result.summary,
                output_ref=result.output_refs[0] if result.output_refs else None,
                latency_ms=result.latency_ms,
                contract=contract,
            )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=observation,
            output_ref=result.output_refs[0] if result.output_refs else None,
            latency_ms=result.latency_ms,
            contract=contract,
        )


def _build_list_result(
    request: ShoppingListSearchRequest,
    searches: list[ProductSearchResult],
) -> ShoppingListSearchResult:
    candidate_groups = [
        _eligible_candidates(need, search, request.top_k_per_need)
        for need, search in zip(request.needs, searches, strict=True)
    ]
    chosen = _choose_basket(request.needs, candidate_groups, request.total_budget)
    selections: list[ShoppingListSelection] = []
    need_results: list[ShoppingListNeedResult] = []
    errors = []
    for need, search, candidates, selected_product in zip(
        request.needs,
        searches,
        candidate_groups,
        chosen,
        strict=True,
    ):
        errors.extend(search.errors)
        selection = (
            _selection(need, selected_product)
            if selected_product is not None
            else None
        )
        if selection is not None:
            selections.append(selection)
            status = "selected"
        elif search.errors and not candidates:
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
                errors=search.errors,
            )
        )

    total_cost = round(sum(item.subtotal for item in selections), 2)
    uncovered = [
        result.need.keyword
        for result in need_results
        if result.need.required and result.selected is None
    ]
    all_searches_failed = bool(searches) and all(
        search.errors and not search.items for search in searches
    )
    if all_searches_failed:
        outcome = "failed"
    elif not selections:
        outcome = "empty"
    elif uncovered:
        outcome = "partial"
    else:
        outcome = "success"
    return ShoppingListSearchResult(
        outcome=outcome,
        scenario=request.scenario,
        decision_reason=request.decision_reason,
        evidence=request.evidence,
        total_budget=request.total_budget,
        total_cost=total_cost,
        within_budget=total_cost <= request.total_budget,
        needs=need_results,
        selections=selections,
        uncovered_required_needs=uncovered,
        summary=_summary(outcome, selections, uncovered, request.total_budget),
        provider="+".join(dict.fromkeys(search.provider for search in searches)),
        errors=errors,
        latency_ms=sum(
            search.latency_ms for search in searches if search.latency_ms is not None
        )
        or None,
        output_refs=list(
            dict.fromkeys(
                search.output_ref for search in searches if search.output_ref
            )
        ),
    )


def _eligible_candidates(
    need: ShoppingListNeed,
    search: ProductSearchResult,
    limit: int,
) -> list[ProductResult]:
    candidates = [
        item
        for item in search.items
        if need.max_unit_price is None or _unit_price(item) <= need.max_unit_price
    ]
    return candidates[:limit]


def _choose_basket(
    needs: list[ShoppingListNeed],
    candidate_groups: list[list[ProductResult]],
    total_budget: float,
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
        if total > total_budget:
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
    product: ProductResult,
) -> ShoppingListSelection:
    unit_price = _unit_price(product)
    return ShoppingListSelection(
        keyword=need.keyword,
        quantity=need.quantity,
        product=product,
        unit_price=unit_price,
        subtotal=round(unit_price * need.quantity, 2),
    )


def _unit_price(product: ProductResult) -> float:
    if product.effective_price is not None:
        return product.effective_price
    return product.price


def _summary(
    outcome: str,
    selections: list[ShoppingListSelection],
    uncovered: list[str],
    budget: float,
) -> str:
    if outcome == "failed":
        return "所有清单项的商品搜索均失败，未生成购物组合。"
    if not selections:
        return f"没有候选商品能组成不超过 {budget:.2f} 元的清单。"
    total = sum(item.subtotal for item in selections)
    if uncovered:
        return (
            f"已在 {budget:.2f} 元总预算内选出 {len(selections)} 项，"
            f"合计 {total:.2f} 元；仍缺少：{'、'.join(uncovered)}。"
        )
    return (
        f"已在 {budget:.2f} 元总预算内覆盖全部必需品类，"
        f"共 {len(selections)} 项，合计 {total:.2f} 元。"
    )


def _list_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    selections = data.get("selections")
    if not isinstance(selections, list):
        selections = []
    return {
        "outcome": data.get("outcome"),
        "scenario": data.get("scenario"),
        "decision_reason": data.get("decision_reason"),
        "evidence": data.get("evidence", []),
        "total_budget": data.get("total_budget"),
        "total_cost": data.get("total_cost"),
        "within_budget": data.get("within_budget"),
        "selections": selections,
        "uncovered_required_needs": data.get("uncovered_required_needs", []),
        "summary": data.get("summary"),
        "errors": data.get("errors", []),
        "rules": [
            "仅把 selections 中的商品表述为已搜索到的候选",
            "不得把预算估算伪装成搜索结果",
            "不得声称已下单",
        ],
    }
