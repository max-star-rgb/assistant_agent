"""Template-based final response summaries."""

from typing import Any


def compose_followup_message(question: str | None) -> str:
    """Return a clear follow-up question for ambiguous requests."""

    return question or "请补充你想让我处理的对象或目标：解释内容、找相似商品、生成图片，还是进行 3D 展示？"


def compose_contract_response(
    contracts: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> str:
    """Compose a readable response from capability output contracts."""

    parts = [_summary_for_contract(contract) for contract in contracts if contract.get("status") == "succeeded"]
    parts = [part for part in parts if part]

    if failures:
        failure_text = "；".join(
            f"{item['source']} 失败：{item.get('code', 'unknown_error')}: {item['message']}" for item in failures
        )
        if parts:
            parts.append(f"部分步骤失败：{failure_text}")
        else:
            parts.append(f"处理失败：{failure_text}")

    return "；".join(parts) if parts else "已完成请求处理。"


def extract_response_fields(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract legacy response.data fields from contracts."""

    product_title = None
    best_price = None
    image_url = None
    render_ref = None
    for contract in contracts:
        capability = contract.get("capability")
        data = contract.get("data") or {}
        if capability == "price_compare":
            best_offer = data.get("best_offer") or {}
            product_title = best_offer.get("title") or product_title
            best_price = best_offer.get("total_price") or best_offer.get("price") or best_price
        elif capability == "product_search" and not product_title:
            items = data.get("items") or []
            if items:
                product_title = items[0].get("title")
        elif capability == "image_generation":
            image_url = data.get("image_url") or contract.get("output_ref") or image_url
        elif capability == "render_3d":
            render_ref = data.get("preview_url") or contract.get("output_ref") or render_ref
    return {"product_title": product_title, "best_price": best_price, "image_url": image_url, "render_ref": render_ref}


def _summary_for_contract(contract: dict[str, Any]) -> str:
    capability = contract.get("capability")
    data = contract.get("data") or {}
    output_ref = contract.get("output_ref")
    if capability in {"image_understanding", "video_understanding"}:
        return _vision_summary(capability, data)
    if capability == "product_search":
        return _product_search_summary(data)
    if capability == "price_compare":
        return _price_compare_summary(data)
    if capability == "image_generation":
        image_url = data.get("image_url") or output_ref
        if image_url:
            return f"已根据你的需求生成图片，图片生成结果为 {image_url}。"
        return "已根据你的需求生成图片。"
    if capability == "render_3d":
        preview_url = data.get("preview_url") or output_ref
        scene = data.get("scene_description")
        if preview_url and scene:
            return f"已基于“{scene}”创建 3D 场景预览，结果为 {preview_url}。"
        if preview_url:
            return f"已创建 3D 场景预览，结果为 {preview_url}。"
        return "已创建 3D 场景预览。"
    if capability == "memory_retrieval":
        items = data.get("items") or []
        if items:
            summary = items[0].get("summary") or data.get("memory_context")
            return f"已检索到相关记忆：{summary}。" if summary else "已检索到相关记忆。"
        return "已检索记忆。"
    if capability == "memory_save":
        summary = data.get("summary")
        return f"记忆已保存：{summary}。" if summary else "记忆已保存。"
    return ""


def _vision_summary(capability: str, data: dict[str, Any]) -> str:
    summary = data.get("summary")
    subject = "视频" if capability == "video_understanding" else "图片"
    if summary:
        return f"我先理解了{subject}内容：{summary}"
    objects = data.get("objects") or []
    if objects:
        return f"我先理解了{subject}内容，识别到：{'、'.join(objects)}。"
    return f"我先理解了{subject}内容。"


def _product_search_summary(data: dict[str, Any]) -> str:
    items = data.get("items") or []
    total = data.get("total") or len(items)
    if not items:
        return "已完成商品搜索，但没有找到候选商品。"
    first = items[0]
    title = first.get("title") or "候选商品"
    source = first.get("source") or "mock"
    return f"已基于 {source} 数据找到 {total} 个商品候选，优先候选是 {title}。"


def _price_compare_summary(data: dict[str, Any]) -> str:
    best_offer = data.get("best_offer") or {}
    if not best_offer:
        summary = data.get("summary")
        return f"已尝试比价：{summary}。" if summary else "已尝试比价，但没有可用报价。"
    title = best_offer.get("title") or "推荐商品"
    total = best_offer.get("total_price") or best_offer.get("price")
    currency = best_offer.get("currency") or "CNY"
    platform = best_offer.get("platform") or "mock 平台"
    if total is not None:
        return f"已完成比价，当前推荐 {title}，最低价格为 {total} {currency}，来源为 {platform}。"
    return f"已完成比价，当前推荐 {title}，来源为 {platform}。"
