"""Input-integrity helpers for model-selected shopping tool calls."""

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.products import ProductResult
from assistant_agent.schemas.requests import UserRequest


def _price_compare_input_from_search(
    state: AgentState,
    request: UserRequest,
) -> dict[str, object] | None:
    """Build structured compare input after the model has selected price_compare."""

    search_result = next(
        (
            result
            for result in reversed(state.tool_results)
            if result.tool_name == "product_search" and result.success
        ),
        None,
    )
    if search_result is None:
        return None
    items = (search_result.data or {}).get("items")
    if not isinstance(items, list) or not items:
        return None
    query = (search_result.data or {}).get("query_used") or request.text or "price_compare"
    tool_input: dict[str, object] = {
        "query": query,
        "items": items,
        "top_k": min(len(items), 5),
        "sort_by": "value",
    }
    succeeded_platforms = (search_result.data or {}).get("succeeded_platforms")
    if isinstance(succeeded_platforms, list) and succeeded_platforms:
        tool_input["platforms"] = [
            platform
            for platform in succeeded_platforms
            if isinstance(platform, str) and platform.strip()
        ]
    return tool_input


def price_compare_completed(state: AgentState) -> bool:
    """Return whether the model-selected price comparison already succeeded."""

    return any(
        result.tool_name == "price_compare" and result.success
        for result in state.tool_results
    )


def repair_price_compare_decision_from_search(
    decision: AssistantDecision,
    state: AgentState,
    request: UserRequest,
) -> AssistantDecision:
    """Repair a model-selected compare call from the successful search result."""

    fallback_input = _price_compare_input_from_search(state, request)
    if fallback_input is None:
        return decision
    fallback_input = dict(fallback_input)
    proposed_input = decision.tool_input if isinstance(decision.tool_input, dict) else {}
    repaired_input = dict(fallback_input)
    proposed_items = proposed_input.get("items")
    if _valid_price_compare_items(proposed_items):
        repaired_input["items"] = proposed_items
    for key in ("query", "currency"):
        value = proposed_input.get(key)
        if isinstance(value, str) and value.strip():
            repaired_input[key] = value
    for key in ("budget_min", "budget_max"):
        value = proposed_input.get(key)
        if isinstance(value, int | float) and value >= 0:
            repaired_input[key] = value
    top_k = proposed_input.get("top_k")
    if isinstance(top_k, int) and top_k >= 1:
        repaired_input["top_k"] = min(top_k, len(repaired_input.get("items", [])) or top_k)
    sort_by = _normalize_price_compare_sort_by(proposed_input.get("sort_by"))
    if sort_by is not None:
        repaired_input["sort_by"] = sort_by
    return decision.model_copy(
        update={
            "tool_input": repaired_input,
            "reason": "模型已选择比价；使用本轮 product_search 的完整商品对象修复 price_compare 入参。",
            "safety_notes": [*decision.safety_notes, "price_compare_input_repaired_from_search"],
        }
    )


def _valid_price_compare_items(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        try:
            ProductResult.model_validate(item)
        except Exception:
            return False
    return True


def _normalize_price_compare_sort_by(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"price", "similarity", "rating", "value"}:
        return normalized
    if normalized in {"price_asc", "lowest_price", "cheapest", "低价", "最低价", "最便宜"}:
        return "price"
    return None
