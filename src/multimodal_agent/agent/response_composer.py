"""Compose final agent responses from AgentState."""

from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.schemas.requests import AgentResponse, UserRequest


def save_demo_memory(request: UserRequest, state: AgentState, tool_executor: ToolExecutor) -> None:
    """Persist the MVP demo summary for multi-tool tasks."""

    if state.intent is None or state.intent.intent != "multi_tool_task":
        return
    if any(result.tool_name == "memory_save" for result in state.tool_results):
        return
    memory_input = {
        "action": "save",
        "user_id": request.user_id,
        "content": {
            "summary": "完成视频鞋子识别、商品搜索、比价和日系海报生成。",
            "text": request.text,
        },
    }
    tool_executor.run_tool(state, "memory_save", "memory_save", memory_input)


def compose_response(state: AgentState) -> AgentResponse:
    """Build the final AgentResponse from successful tool outputs."""

    product_title = None
    best_price = None
    image_url = None
    memory_ref = None
    memory_summaries = [item.summary for item in state.memory_context]
    memory_context_text = state.request.metadata.get("memory_context_text", "")
    successful_results = [result for result in state.tool_results if result.success]
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
    for result in state.tool_results:
        if not result.success or not result.data:
            continue
        if result.tool_name == "price_compare" and result.data.get("items"):
            first_item = result.data["items"][0]
            product_title = first_item.get("title")
            best_price = first_item.get("price")
        elif result.tool_name == "image_generation":
            image_url = result.data.get("image_url")
        elif result.tool_name == "memory_save":
            memory_ref = result.output_ref

    parts = []
    if product_title and best_price is not None:
        parts.append(f"商品：{product_title}，最低价格：{best_price}")
    if image_url:
        parts.append(f"图片生成结果：{image_url}")
    if memory_ref:
        parts.append(f"记忆已保存：{memory_ref}")
    if memory_summaries:
        parts.append(f"参考记忆：{memory_summaries[0]}")
    if failures:
        failure_text = "；".join(f"{item['source']} 失败：{item['message']}" for item in failures)
        if successful_results:
            parts.append(f"部分步骤失败：{failure_text}")
        else:
            parts.append(f"处理失败：{failure_text}")
    return AgentResponse(
        message="；".join(parts) if parts else "已完成请求处理。",
        data={
            "intent": state.intent.intent if state.intent else None,
            "tool_count": len(state.tool_calls),
            "product_title": product_title,
            "best_price": best_price,
            "image_url": image_url,
            "memory_ref": memory_ref,
            "memory_context_count": len(state.memory_context),
            "memory_context_summaries": memory_summaries,
            "memory_context_text": memory_context_text,
            "errors": failures,
            "partial_success": bool(successful_results and failures),
        },
    )
