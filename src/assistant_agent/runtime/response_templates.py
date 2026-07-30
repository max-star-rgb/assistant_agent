"""Template-based final response summaries."""

from typing import Any

from assistant_agent.tools.ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_CAPABILITY,
    SHOPPING_SEARCH_CAPABILITY,
    VIDEO_UNDERSTANDING_CAPABILITY,
    WEB_SEARCH_CAPABILITY,
)


def compose_followup_message(question: str | None) -> str:
    """Return a clear follow-up question for ambiguous requests."""

    return question or "请补充你想让我处理的对象或目标：解释内容、找相似商品，还是生成图片？"


def compose_contract_response(
    contracts: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> str:
    """Compose a readable response from capability output contracts."""

    parts = [
        _summary_for_contract(contract)
        for contract in contracts
        if contract.get("status") == "succeeded"
    ]
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
        if capability == SHOPPING_SEARCH_CAPABILITY:
            best_offer = data.get("best_offer") or {}
            product_title = best_offer.get("title") or product_title
            best_price = best_offer.get("total_price") or best_offer.get("price") or best_price
        elif capability == IMAGE_GENERATION_CAPABILITY:
            image_url = data.get("image_url") or contract.get("output_ref") or image_url
    return {
        "product_title": product_title,
        "best_price": best_price,
        "image_url": image_url,
        "render_ref": render_ref,
    }


def _summary_for_contract(contract: dict[str, Any]) -> str:
    capability = contract.get("capability")
    data = contract.get("data") or {}
    output_ref = contract.get("output_ref")
    if capability in {IMAGE_UNDERSTANDING_CAPABILITY, VIDEO_UNDERSTANDING_CAPABILITY}:
        return _vision_summary(capability, data)
    if capability == WEB_SEARCH_CAPABILITY:
        return _web_search_summary(data)
    if capability == SHOPPING_SEARCH_CAPABILITY:
        return _shopping_search_summary(data)
    if capability == IMAGE_GENERATION_CAPABILITY:
        image_url = data.get("download_url") or data.get("image_url") or output_ref
        if image_url:
            return f"已根据你的需求生成图片，图片生成结果为 {image_url}。"
        return "已根据你的需求生成图片。"
    return ""


def _vision_summary(capability: str, data: dict[str, Any]) -> str:
    summary = data.get("summary")
    subject = "视频" if capability == VIDEO_UNDERSTANDING_CAPABILITY else "图片"
    if summary:
        return f"我先理解了{subject}内容：{summary}"
    objects = data.get("objects") or []
    if objects:
        return f"我先理解了{subject}内容，识别到：{'、'.join(objects)}。"
    return f"我先理解了{subject}内容。"


def _web_search_summary(data: dict[str, Any]) -> str:
    results = data.get("results") or []
    total = data.get("total") or len(results)
    if not results:
        return "已完成联网搜索，但没有找到可用结果。"
    first = results[0]
    title = first.get("title") or "搜索结果"
    url = first.get("url")
    source = first.get("source") or data.get("provider") or "web"
    published_at = first.get("published_at")
    date_text = f"，发布时间 {published_at}" if published_at else ""
    url_text = f"，链接：{url}" if url else "，未提供来源链接"
    return f"已基于 {source} 搜索到 {total} 条结果，首条是 {title}{date_text}{url_text}。"


def _shopping_search_summary(data: dict[str, Any]) -> str:
    selections = data.get("selections") or []
    summary = data.get("summary")
    if not selections:
        return summary or "已完成真实商品搜索，但没有可用候选。"
    selection = selections[0]
    product = selection.get("product") or {}
    title = product.get("title") or selection.get("keyword") or "推荐商品"
    total = selection.get("subtotal")
    currency = product.get("currency") or "CNY"
    platform = product.get("platform")
    platform_text = f"，来源为 {platform}" if platform else ""
    url_text = _product_link_text(product)
    count = len(selections)
    if total is not None:
        return (
            f"已从真实搜索结果中选出 {count} 项，首项为 {title}，"
            f"小计 {total} {currency}{platform_text}{url_text}。"
        )
    return f"已从真实搜索结果中选出 {count} 项，首项为 {title}{platform_text}{url_text}。"


def _product_link_text(item: dict[str, Any]) -> str:
    url = item.get("product_url") or item.get("url")
    if not url:
        return "，未提供可直接打开的商品链接"
    if item.get("url_status") == "verified":
        return f"，链接：{url}"
    return f"，链接：{url}（未验证）"
