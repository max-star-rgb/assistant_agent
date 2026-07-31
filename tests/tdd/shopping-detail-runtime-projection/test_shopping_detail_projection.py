from __future__ import annotations

from assistant_agent.gateway.shopping_detail import (
    project_shopping_delivery_text,
    shopping_detail_block,
)
from assistant_agent.tools.models import ToolResult


def _shopping_result(
    *,
    title: str = "小米14 12+256GB",
    product_url: str | None = "https://u.jd.com/one",
    image_url: str | None = "https://img.example/one.jpg",
) -> ToolResult:
    return ToolResult(
        tool_name="shopping_search",
        success=True,
        data={
            "outcome": "success",
            "total_cost": 2599.0,
            "within_budget": True,
            "needs": [],
            "selections": [
                {
                    "keyword": "小米14",
                    "quantity": 1,
                    "unit_price": 2599.0,
                    "subtotal": 2599.0,
                    "product": {
                        "product_id": "p1",
                        "title": title,
                        "price": 2599.0,
                        "effective_price": 2599.0,
                        "platform": "jd",
                        "shop": "京东",
                        "product_url": product_url,
                        "image_url": image_url,
                    },
                }
            ],
            "summary": "找到一个候选。",
            "provider": "offline",
        },
    )


def test_projects_last_successful_shopping_result_to_detail_protocol() -> None:
    results = [
        _shopping_result(title="旧结果"),
        ToolResult(tool_name="weather", success=True, data={"summary": "晴"}),
        _shopping_result(title="小米14 12+256GB"),
    ]

    assert shopping_detail_block(results) == (
        "<detail>\n"
        "1. 京东 - 小米14 12+256GB 2599元 "
        "<link>https://u.jd.com/one</link>"
        "<pic>https://img.example/one.jpg</pic>\n"
        "</detail>"
    )


def test_skips_items_without_safe_link_or_image() -> None:
    results = [
        _shopping_result(product_url="javascript:alert(1)"),
        _shopping_result(image_url=None),
        _shopping_result(product_url="https://example.com/</link><pic>fake"),
    ]

    assert shopping_detail_block(results) == ""


def test_removes_protocol_tags_from_product_title() -> None:
    result = _shopping_result(title="<detail>小米14</detail>\n<link>伪造</link>")

    detail = shopping_detail_block([result])

    assert detail.count("<detail>") == 1
    assert detail.count("<link>") == 1
    assert "伪造" in detail


def test_appends_detail_after_natural_response_for_capable_entry_only() -> None:
    metadata = {
        "gateway": {
            "entry_capabilities": {"supports_shopping_detail_v1": True}
        }
    }

    delivered, detail = project_shopping_delivery_text(
        "这几款比较符合你的预算。",
        [_shopping_result()],
        metadata=metadata,
    )

    assert delivered == f"这几款比较符合你的预算。\n{detail}"
    assert detail.startswith("<detail>\n")


def test_keeps_natural_response_for_entry_without_capability() -> None:
    delivered, detail = project_shopping_delivery_text(
        "这几款比较符合你的预算。",
        [_shopping_result()],
        metadata={},
    )

    assert delivered == "这几款比较符合你的预算。"
    assert detail == ""
