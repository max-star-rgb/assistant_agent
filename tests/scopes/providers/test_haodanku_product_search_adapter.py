"""Tests for the Haodanku (好单库) product search / price compare provider.

These tests never perform real network IO: ``urllib.request.urlopen`` is
monkeypatched with a fake response.
"""

import io
import json

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.haodanku_product_search import (
    HaodankuConfig,
    HaodankuPriceCompareAdapter,
    HaodankuProductSearchAdapter,
    build_haodanku_search_url,
    map_haodanku_items,
)
from assistant_agent.schemas.products import ProductSearchResult
from assistant_agent.services.product_adapter import (
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
            "couponurl": "https://s.click.taobao.com/1001",
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
            "itemlink": "https://item.taobao.com/item.htm?id=1002",
        },
    ],
}


def _fake_urlopen(payload: dict):
    def _opener(request, timeout=None):  # noqa: ANN001
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    return _opener


def _sample_payload_with_items(count: int) -> dict:
    return {
        "code": 1,
        "data": [
            {
                "itemid": str(2000 + index),
                "itemtitle": f"白色运动鞋 {index}",
                "itemprice": f"{300 + index}.00",
                "itemendprice": f"{250 + index}.00",
                "shoptype": "1",
                "itemsale": str(100 + index),
            }
            for index in range(count)
        ],
    }


def _sample_payload_with_mixed_link_items() -> dict:
    return {
        "code": 1,
        "data": [
            {
                "itemid": "AAE9r8X",
                "itemtitle": "乐事薯片无链接款 A",
                "itemprice": "29.90",
                "itemendprice": "19.90",
            },
            {
                "itemid": "AAE9r8Y",
                "itemtitle": "乐事薯片有券链接款 B",
                "itemprice": "39.90",
                "itemendprice": "29.90",
                "couponurl": "https://s.click.taobao.com/lays-b",
            },
            {
                "itemid": "AAE9r8Z",
                "itemtitle": "乐事薯片无链接款 C",
                "itemprice": "49.90",
                "itemendprice": "39.90",
            },
            {
                "itemid": "AAE9r9A",
                "itemtitle": "乐事薯片落地链接款 D",
                "itemprice": "35.90",
                "itemendprice": "25.90",
                "itemlink": "https://item.taobao.com/lays-d",
            },
            {
                "itemid": "123456",
                "itemtitle": "乐事薯片数字 ID 款 E",
                "itemprice": "32.90",
                "itemendprice": "22.90",
            },
        ],
    }


def _sample_payload_with_unlinked_items() -> dict:
    return {
        "code": 1,
        "data": [
            {
                "itemid": f"AAE9r8X{index}",
                "itemtitle": f"乐事薯片无链接款 {index}",
                "itemprice": "29.90",
                "itemendprice": "19.90",
            }
            for index in range(3)
        ],
    }


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
    assert first.platform == "taobao"  # Tmall is normalized into the Taobao group.
    assert first.sales == 1200
    assert first.image_url == "https://img.example/1001.jpg"
    assert first.url == "https://s.click.taobao.com/1001"
    assert first.product_url == "https://s.click.taobao.com/1001"
    assert first.raw_url == "https://s.click.taobao.com/1001"
    assert first.coupon_url == "https://s.click.taobao.com/1001"
    assert first.click_url == "https://s.click.taobao.com/1001"
    assert first.landing_url is None
    assert first.provider_item_id == "1001"
    assert first.url_status == "unverified"
    assert first.availability == "unknown"
    assert first.source == "haodanku"
    assert "coupon" in first.style_tags
    assert items[1].platform == "taobao"  # shoptype=0
    assert items[1].product_url == "https://item.taobao.com/item.htm?id=1002"
    assert items[1].landing_url == "https://item.taobao.com/item.htm?id=1002"
    assert items[1].raw_url == "https://item.taobao.com/item.htm?id=1002"
    assert items[1].coupon_url is None
    assert items[1].click_url is None
    assert items[1].url_status == "unverified"


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
    assert item.raw_url is None
    assert item.coupon_url is None
    assert item.landing_url is None
    assert item.url_status == "unverified"
    assert item.availability == "unknown"


def test_map_haodanku_items_does_not_build_product_url_from_non_numeric_itemid() -> None:
    payload = {
        "code": 1,
        "data": [
            {
                "itemid": "AAE9r8X",
                "itemtitle": "闲鱼加密 ID 商品",
                "itemprice": "99.00",
                "itemendprice": "79.00",
            }
        ],
    }

    item = map_haodanku_items(payload)[0]

    assert item.product_id == "AAE9r8X"
    assert item.provider_item_id == "AAE9r8X"
    assert item.url is None
    assert item.product_url is None
    assert item.raw_url is None
    assert item.url_status == "invalid_id"
    assert item.availability == "unknown"


@pytest.mark.parametrize(
    ("field", "link"),
    [
        ("click_url", "https://s.click.taobao.com/tool"),
        ("clickURL", "https://s.click.taobao.com/click"),
        ("short_url", "https://s.click.taobao.com/short"),
        ("mobile_url", "https://s.click.taobao.com/mobile"),
        ("kwaiUrl", "https://s.click.taobao.com/kwai"),
        ("linkUrl", "https://s.click.taobao.com/link"),
        ("trans_url", "https://s.click.taobao.com/trans"),
        ("share_link", "https://s.click.taobao.com/share"),
        ("referral_link", "https://s.click.taobao.com/referral"),
    ],
)
def test_map_haodanku_items_preserves_documented_provider_link_fields(field: str, link: str) -> None:
    payload = {
        "code": 1,
        "data": [
            {
                "itemid": "AAE9r8X",
                "itemtitle": "有真实链接的加密 ID 商品",
                "itemprice": "99.00",
                "itemendprice": "79.00",
                field: link,
            }
        ],
    }

    item = map_haodanku_items(payload)[0]

    assert item.product_url == link
    assert item.raw_url == link
    assert item.coupon_url == link
    assert item.click_url == link
    assert item.url_status == "unverified"


def test_map_haodanku_items_ignores_non_http_deeplink_as_browser_product_url() -> None:
    payload = {
        "code": 1,
        "data": [
            {
                "itemid": "AAE9r8X",
                "itemtitle": "只有 App schema 的商品",
                "itemprice": "99.00",
                "itemendprice": "79.00",
                "deeplink": "taobao://item?id=AAE9r8X",
            }
        ],
    }

    item = map_haodanku_items(payload)[0]

    assert item.product_url is None
    assert item.raw_url is None
    assert item.url_status == "invalid_id"


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://evil.example/phish",
        "https://taobao.com.evil.example/phish",
    ],
)
def test_map_haodanku_items_rejects_non_taobao_http_links(unsafe_url: str) -> None:
    payload = {
        "code": 1,
        "data": [
            {
                "itemid": "AAE9r8X",
                "itemtitle": "不安全链接商品",
                "itemprice": "99.00",
                "itemendprice": "79.00",
                "itemlink": unsafe_url,
            }
        ],
    }

    item = map_haodanku_items(payload)[0]

    assert item.product_url is None
    assert item.url_status == "invalid_id"


def test_map_haodanku_items_replaces_unsafe_link_with_official_numeric_item_url() -> None:
    payload = {
        "code": 1,
        "data": [
            {
                "itemid": "123456",
                "itemtitle": "数字 ID 商品",
                "itemprice": "99.00",
                "itemlink": "https://evil.example/phish",
            }
        ],
    }

    item = map_haodanku_items(payload)[0]

    assert item.product_url == "https://item.taobao.com/item.htm?id=123456"
    assert item.url_status == "unverified"


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


def test_search_normalizes_top_k_to_supported_back_and_truncates(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def _opener(request, timeout=None):  # noqa: ANN001
        captured["url"] = request.full_url
        return io.BytesIO(json.dumps(_sample_payload_with_items(5)).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _opener)
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key="test-key"))

    result = adapter.search(ProductSearchInput(query="白色运动鞋", top_k=3, platforms=["taobao"]))

    assert result.success is True
    assert "back=20" in captured["url"]
    assert len(result.items) == 3
    assert result.total == 3
    assert result.filters_used["top_k"] == 3
    assert result.filters_used["provider_back"] == 20
    assert result.filters_used["linked_only"] is True
    assert result.filters_used["linked_items_found"] == 5
    assert result.filters_used["linked_items_returned"] == 3
    assert result.filters_used["unlinked_items_dropped"] == 0


def test_search_overfetches_and_returns_only_linked_items(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def _opener(request, timeout=None):  # noqa: ANN001
        captured["url"] = request.full_url
        return io.BytesIO(json.dumps(_sample_payload_with_mixed_link_items()).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _opener)
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key="test-key"))

    result = adapter.search(ProductSearchInput(query="乐事薯片", top_k=2, platforms=["taobao"]))

    assert result.success is True
    assert "back=20" in captured["url"]
    assert [item.title for item in result.items] == [
        "乐事薯片有券链接款 B",
        "乐事薯片落地链接款 D",
    ]
    assert all(item.product_url for item in result.items)
    assert result.total == 2
    assert result.filters_used["linked_only"] is True
    assert result.filters_used["linked_items_found"] == 3
    assert result.filters_used["linked_items_returned"] == 2
    assert result.filters_used["unlinked_items_dropped"] == 2


def test_search_drops_unlinked_haodanku_items_without_fake_urls(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_urlopen(_sample_payload_with_unlinked_items()),
    )
    adapter = HaodankuProductSearchAdapter(HaodankuConfig(api_key="test-key"))

    result = adapter.search(ProductSearchInput(query="乐事薯片", top_k=5, platforms=["taobao"]))

    assert result.success is True
    assert result.items == []
    assert result.total == 0
    assert result.filters_used["linked_items_found"] == 0
    assert result.filters_used["linked_items_returned"] == 0
    assert result.filters_used["unlinked_items_dropped"] == 3


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
    assert result.best_offer is not None
    assert result.best_offer.product_url == "https://item.taobao.com/item.htm?id=1002"
    assert result.best_offer.url_status == "unverified"
    assert result.best_offer.availability == "unknown"


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
    config = ProviderConfig(
        product_search_provider="haodanku",
        haodanku_api_key="test-key",
        haodanku_enabled_platforms=("taobao", "jd"),
    )

    adapter = create_product_search_adapter(config)

    assert isinstance(adapter, HaodankuProductSearchAdapter)
    assert adapter.config.enabled_platforms == ("taobao", "jd")


def test_factory_returns_unconfigured_without_api_key() -> None:
    config = ProviderConfig(product_search_provider="haodanku")

    adapter = create_product_search_adapter(config)

    result = adapter.search(ProductSearchInput(query="白色运动鞋"))
    assert result.success is False
    assert result.provider == "haodanku"
    assert result.errors[0].code == "provider_unconfigured"
    assert "HAODANKU_API_KEY" in result.errors[0].message


def test_price_factory_selects_haodanku_when_configured() -> None:
    config = ProviderConfig(
        price_compare_provider="haodanku",
        haodanku_api_key="test-key",
        haodanku_enabled_platforms=("taobao", "jd"),
    )

    adapter = create_price_compare_adapter(config)

    assert isinstance(adapter, HaodankuPriceCompareAdapter)
    assert adapter.config.enabled_platforms == ("taobao", "jd")


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
