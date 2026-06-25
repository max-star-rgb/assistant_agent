"""Tests for the Haodanku (好单库) product search / price compare provider.

These tests never perform real network IO: ``urllib.request.urlopen`` is
monkeypatched with a fake response.
"""

import io
import json

import pytest

from multimodal_agent.config import ProviderConfig
from multimodal_agent.providers.haodanku_product_search import (
    HaodankuConfig,
    HaodankuPriceCompareAdapter,
    HaodankuProductSearchAdapter,
    build_haodanku_search_url,
    map_haodanku_items,
)
from multimodal_agent.schemas.products import ProductSearchResult
from multimodal_agent.services.product_adapter import (
    MockProductSearchAdapter,
    PriceCompareInput,
    ProductSearchInput,
    create_price_compare_adapter,
    create_product_search_adapter,
)


SAMPLE_PAYLOAD = {
    "code": 1,
    "data": [
        {
            "itemid": "1001",
            "itemtitle": "白色低帮运动鞋 男款",
            "itemprice": "359.00",
            "itemendprice": "299.00",
            "couponmoney": "60",
            "commission_rate": "15.0",
            "itempic": "https://img.example/1001.jpg",
            "shopname": "示例运动旗舰店",
            "shoptype": "1",
            "itemsale": "1200",
            "couponurl": "https://s.click.example/1001",
        },
        {
            "itemid": "1002",
            "itemtitle": "简约白色板鞋",
            "itemprice": "279.00",
            "itemendprice": "259.00",
            "couponmoney": "20",
            "commission_rate": "10.0",
            "itempic": "https://img.example/1002.jpg",
            "shopname": "示例鞋类店",
            "shoptype": "0",
            "itemsale": "800",
            "itemlink": "https://item.example/1002",
        },
    ],
}


def _fake_urlopen(payload: dict):
    def _opener(request, timeout=None):  # noqa: ANN001
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    return _opener


def test_build_haodanku_search_url_contains_apikey_and_keyword() -> None:
    url = build_haodanku_search_url(
        base_url="https://v3.api.haodanku.com/",
        api_key="test-key",
        keyword="白色运动鞋",
        back=5,
    )

    assert url.startswith("https://v3.api.haodanku.com/supersearch?")
    assert "apikey=test-key" in url
    assert "back=5" in url
    assert "keyword=" in url


def test_build_haodanku_search_url_normalizes_legacy_v2_base_url() -> None:
    url = build_haodanku_search_url(
        base_url="https://v2.api.haodanku.com/",
        api_key="test-key",
        keyword="小米17",
    )

    assert url.startswith("https://v3.api.haodanku.com/supersearch?")
    assert "/keyword" not in url


def test_map_haodanku_items_maps_coupon_price_and_platform() -> None:
    items = map_haodanku_items(SAMPLE_PAYLOAD)

    assert len(items) == 2
    first = items[0]
    assert first.product_id == "1001"
    assert first.title == "白色低帮运动鞋 男款"
    assert first.price == 299.0  # 券后价
    assert first.platform == "tmall"  # shoptype=1
    assert first.sales == 1200
    assert first.image_url == "https://img.example/1001.jpg"
    assert first.url == "https://s.click.example/1001"
    assert first.source == "haodanku"
    assert "coupon" in first.style_tags
    assert items[1].platform == "taobao"  # shoptype=0


def test_map_haodanku_items_skips_items_without_price_or_id() -> None:
    payload = {"code": 1, "data": [{"itemtitle": "无价商品"}, {"itemid": "x", "itemtitle": "缺价", "itemprice": ""}]}

    assert map_haodanku_items(payload) == []


def test_map_haodanku_items_builds_product_url_from_itemid_when_missing() -> None:
    payload = {
        "code": 1,
        "data": [
            {
                "itemid": "123456",
                "itemtitle": "小米17 Pro 手机",
                "itemprice": "4999.00",
                "itemendprice": "4999.00",
            }
        ],
    }

    item = map_haodanku_items(payload)[0]

    assert item.url == "https://item.taobao.com/item.htm?id=123456"
    assert item.product_url == "https://item.taobao.com/item.htm?id=123456"


def test_search_without_api_key_returns_provider_unconfigured() -> None:
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key=None))

    result = adapter.search(ProductSearchInput(query="白色运动鞋"))

    assert result.success is False
    assert result.provider == "haodanku"
    assert result.errors[0].code == "provider_unconfigured"


def test_search_with_empty_query_returns_product_query_empty() -> None:
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key="test-key"))

    result = adapter.search(ProductSearchInput())

    assert result.success is False
    assert result.errors[0].code == "product_query_empty"


def test_search_success_returns_structured_results(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen(SAMPLE_PAYLOAD),
    )
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key="test-key"))

    result = adapter.search(ProductSearchInput(query="白色运动鞋", top_k=5))

    assert isinstance(result, ProductSearchResult)
    assert result.success is True
    assert result.provider == "haodanku"
    assert result.query_used == "白色运动鞋"
    assert all(item.source == "haodanku" for item in result.items)


def test_search_propagates_haodanku_error_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen({"code": 0, "msg": "apikey invalid"}),
    )
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key="bad-key"))

    result = adapter.search(ProductSearchInput(query="白色运动鞋"))

    assert result.success is False
    assert result.errors[0].code == "provider_bad_response"


def test_compare_with_supplied_items_ranks_by_coupon_price(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen(SAMPLE_PAYLOAD),
    )
    search = HaodankuProductSearchAdapter(HaodankuConfig(api_key="test-key"))
    items = search.search(ProductSearchInput(query="白色运动鞋")).items

    compare = HaodankuPriceCompareAdapter(HaodankuConfig(api_key="test-key"))
    result = compare.compare(PriceCompareInput(items=items, query="白色运动鞋", sort_by="price"))

    assert result.success is True
    assert result.provider == "haodanku"
    assert result.best_value_product_id == "1002"  # 券后价 259 < 299
    assert result.items[0].price <= result.items[-1].price


def test_compare_without_items_runs_search_first(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen(SAMPLE_PAYLOAD),
    )
    compare = HaodankuPriceCompareAdapter(HaodankuConfig(api_key="test-key"))

    result = compare.compare(PriceCompareInput(query="白色运动鞋", sort_by="price"))

    assert result.success is True
    assert result.best_value_product_id == "1002"


def test_factory_selects_haodanku_when_configured() -> None:
    config = ProviderConfig(product_search_provider="haodanku", haodanku_api_key="test-key")

    adapter = create_product_search_adapter(config)

    assert isinstance(adapter, HaodankuProductSearchAdapter)


def test_factory_returns_unconfigured_without_api_key() -> None:
    config = ProviderConfig(product_search_provider="haodanku")

    adapter = create_product_search_adapter(config)

    result = adapter.search(ProductSearchInput(query="白色运动鞋"))
    assert result.success is False
    assert result.provider == "haodanku"
    assert result.errors[0].code == "provider_unconfigured"
    assert "HAODANKU_API_KEY" in result.errors[0].message


def test_price_factory_selects_haodanku_when_configured() -> None:
    config = ProviderConfig(price_compare_provider="haodanku", haodanku_api_key="test-key")

    adapter = create_price_compare_adapter(config)

    assert isinstance(adapter, HaodankuPriceCompareAdapter)


def test_local_demo_profile_falls_back_to_mock() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "local_demo",
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "haodanku",
            "HAODANKU_API_KEY": "test-key",
        }
    )

    assert config.product_search_provider == "mock"
    assert isinstance(create_product_search_adapter(config), MockProductSearchAdapter)


def test_provider_smoke_profile_enables_haodanku() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "haodanku",
            "HAODANKU_API_KEY": "test-key",
        }
    )

    assert config.product_search_provider == "haodanku"
    assert config.haodanku_api_key == "test-key"
