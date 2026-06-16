import json

from multimodal_agent.services.product_adapter import (
    LocalJsonProductSearchAdapter,
    MockProductSearchAdapter,
    ProductSearchInput,
    ProductSearchRequest,
)


def test_mock_product_search_adapter_defaults_to_structured_result() -> None:
    result = MockProductSearchAdapter().search(ProductSearchRequest(query="白色低帮运动鞋"))

    assert result.success is True
    assert result.provider == "mock"
    assert result.query_used == "白色低帮运动鞋"
    assert result.total == 3
    assert result.output_ref == "mock://products/white-low-top-sneaker"
    assert result.errors == []


def test_mock_product_search_accepts_text_only_query() -> None:
    result = MockProductSearchAdapter().search(ProductSearchInput(query="帮我找 500 元以内的白色运动鞋"))

    assert result.success is True
    assert result.items
    assert all(item.price <= 500 for item in result.items)


def test_mock_product_search_accepts_visual_summary_query() -> None:
    result = MockProductSearchAdapter().search(
        ProductSearchInput(visual_summary="图片中是一双白色低帮运动鞋", top_k=2)
    )

    assert result.success is True
    assert result.query_used == "图片中是一双白色低帮运动鞋"
    assert len(result.items) == 2


def test_local_json_product_search_provider_reads_small_local_dataset(tmp_path) -> None:
    dataset = tmp_path / "products.json"
    dataset.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "product_id": "local-1",
                        "title": "本地白色运动鞋",
                        "price": 199,
                        "platform": "local-shop",
                        "reason": "本地 demo 商品",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = LocalJsonProductSearchAdapter(dataset).search(ProductSearchInput(query="白色运动鞋"))

    assert result.success is True
    assert result.provider == "local_json"
    assert result.items[0].product_id == "local-1"
    assert result.output_ref == "local://products/search"
