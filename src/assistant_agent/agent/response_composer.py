"""Compose final agent responses from AgentState."""

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.response_templates import (
    compose_contract_response,
    compose_followup_message,
    extract_response_fields,
)
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.schemas.tool_ids import (
    IMAGE_GENERATION_TOOL_NAME,
    MEMORY_SAVE_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
)


def compose_response(state: AgentState) -> AgentResponse:
    """Build the final AgentResponse from successful tool outputs."""

    if state.response is not None:
        return state.response

    memory_ref = None
    memory_status = None
    memory_summaries = [item.summary for item in state.memory_context]
    memory_context_text = state.request.metadata.get("memory_context_text", "")
    successful_results = [result for result in state.tool_results if result.success]
    contracts = [
        result.contract.model_dump(mode="json")
        for result in state.tool_results
        if result.contract is not None
    ]
    failures = [
        {
            "source": error.source,
            "code": error.details.get("code", "unknown_error"),
            "message": error.message,
            "recovery_action": error.details.get("recovery_action", "stop_with_error"),
            "optional_step": error.details.get("optional_step", False),
        }
        for error in state.errors
    ]
    if state.plan is not None and state.plan.requires_followup:
        return AgentResponse(
            message=compose_followup_message(state.plan.followup_question),
            data={
                "intent": state.intent.intent if state.intent else None,
                "tool_count": len(state.tool_calls),
                "followup_question": state.plan.followup_question,
                "errors": failures,
                "partial_success": False,
                "contracts": contracts,
            },
            followup_question=state.plan.followup_question,
        )
    fields = extract_response_fields(contracts)
    product_title = fields["product_title"]
    best_price = fields["best_price"]
    image_url = fields["image_url"]
    render_ref = fields["render_ref"]
    for result in state.tool_results:
        if not result.success or not result.data:
            continue
        if result.tool_name == SHOPPING_SEARCH_TOOL_NAME and result.data.get("items") and not product_title:
            first_item = result.data["items"][0]
            product_title = first_item.get("title")
            best_price = first_item.get("price")
        elif result.tool_name == IMAGE_GENERATION_TOOL_NAME and not image_url:
            image_url = result.data.get("image_url")
        elif result.tool_name == MEMORY_SAVE_TOOL_NAME:
            memory_ref = result.output_ref
            if isinstance(result.data, dict):
                memory_status = result.data.get("status")

    message = compose_contract_response(contracts, failures)
    parts = [message] if message and message != "已完成请求处理。" else []
    if not contracts:
        if product_title and best_price is not None:
            parts.append(f"商品：{product_title}，最低价格：{best_price}")
        if image_url:
            parts.append(f"图片生成结果：{image_url}")
    if memory_ref and memory_status == "candidate_recorded":
        parts.append(f"记忆候选已记录：{memory_ref}")
    elif memory_ref:
        parts.append(f"记忆已保存：{memory_ref}")
    if memory_summaries:
        parts.append(f"参考记忆：{memory_summaries[0]}")
    return AgentResponse(
        message="；".join(parts) if parts else "已完成请求处理。",
        data={
            "intent": state.intent.intent if state.intent else None,
            "tool_count": len(state.tool_calls),
            "product_title": product_title,
            "best_price": best_price,
            "image_url": image_url,
            "render_ref": render_ref,
            "memory_ref": memory_ref,
            "memory_context_count": len(state.memory_context),
            "memory_context_summaries": memory_summaries,
            "memory_context_text": memory_context_text,
            "errors": failures,
            "partial_success": bool(successful_results and failures),
            "contracts": contracts,
        },
    )
