import io
import json
import threading
import urllib.error

from assistant_agent.providers.haodanku_product_search import (
    HaodankuConfig,
    HaodankuPriceCompareAdapter,
    HaodankuProductSearchAdapter,
    build_haodanku_platform_search_url,
    map_haodanku_platform_items,
)
from assistant_agent.schemas.products import ProductSearchRequest
from assistant_agent.schemas.products import PriceCompareRequest, ProductResult
from assistant_agent.schemas.products import ProductProviderError, ProductSearchResult
from assistant_agent.services.product_adapter import MockPriceCompareAdapter
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool


def test_pdd_search_url_normalizes_top_k_to_supported_back() -> None:
    url = build_haodanku_platform_search_url(
        base_url="https://v3.api.haodanku.com",
        api_key="key",
        platform="pdd",
        keyword="蓝牙耳机",
        limit=3,
    )

    assert "back=10" in url
    assert "limit=" not in url


def test_jd_search_url_normalizes_top_k_to_supported_back() -> None:
    url = build_haodanku_platform_search_url(
        base_url="https://v3.api.haodanku.com",
        api_key="key",
        platform="jd",
        keyword="蓝牙耳机",
        limit=3,
    )

    assert "back=5" in url
    assert "limit=" not in url


def test_maps_taobao_jd_and_pdd_into_one_contract() -> None:
    taobao = map_haodanku_platform_items(
        "taobao",
        {"data": [{"itemid": "tb1", "itemtitle": "手机 16GB 512GB", "itemprice": "3299", "itemendprice": "2999", "couponmoney": "300", "itempic": "https://img.example/tb.jpg"}]},
    )[0]
    jd = map_haodanku_platform_items(
        "jd",
        {"data": [{"itemid": "jd1", "goodsname": "手机 16GB 512GB", "itemprice": "3199", "itemendprice": "3099", "couponmoney": "100", "itempic": "https://img.example/jd.jpg", "brand_name": "示例"}]},
    )[0]
    pdd = map_haodanku_platform_items(
        "pdd",
        {"data": [{"goods_sign": "pdd1", "goodsname": "手机 16GB 512GB", "itemprice": "3099", "itemendprice": "2899", "couponmoney": "200", "itempic": "https://img.example/pdd.jpg"}]},
    )[0]

    assert [taobao.platform, jd.platform, pdd.platform] == ["taobao", "jd", "pdd"]
    assert (taobao.original_price, taobao.coupon_amount, taobao.effective_price) == (3299, 300, 2999)
    assert jd.brand == "示例"
    assert pdd.provider_item_id == "pdd1"
    assert all(item.image_url for item in (taobao, jd, pdd))


def test_search_runs_three_platforms_concurrently_and_keeps_partial_success(monkeypatch) -> None:
    barrier = threading.Barrier(3, timeout=2)
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        path = request.full_url.split("?", 1)[0].rsplit("/", 1)[-1]
        seen.append(path)
        barrier.wait()
        if path == "unify_jdgoods_search":
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)
        if path == "supersearch":
            payload = {"code": 1, "data": [{"itemid": "1", "itemtitle": "手机", "itemprice": "10", "itempic": "https://img.example/tb.jpg"}]}
        else:
            payload = {"code": 1, "data": [{"goods_sign": "2", "goodsname": "手机", "itemprice": "9", "itempic": "https://img.example/pdd.jpg"}]}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = HaodankuProductSearchAdapter(
        HaodankuConfig(api_key="key", enabled_platforms=("taobao", "jd", "pdd"))
    ).search(
        ProductSearchRequest(query="手机", platforms=["淘宝", "京东", "拼多多"], top_k=3)
    )

    assert set(seen) == {"supersearch", "unify_jdgoods_search", "unify_pdd_goods_search"}
    assert result.requested_platforms == ["taobao", "jd", "pdd"]
    assert result.succeeded_platforms == ["taobao", "pdd"]
    assert result.failed_platforms == ["jd"]
    assert result.platform_errors["jd"][0].code == "provider_permission_denied"
    assert {item.platform for item in result.items} == {"taobao", "pdd"}


def test_default_search_only_calls_taobao_supersearch(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        seen.append(request.full_url.split("?", 1)[0].rsplit("/", 1)[-1])
        payload = {
            "code": 1,
            "data": [{"itemid": "1", "itemtitle": "淘宝手机", "itemprice": "10", "itempic": "https://img.example/tb.jpg"}],
        }
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = HaodankuProductSearchAdapter(HaodankuConfig(api_key="key")).search(
        ProductSearchRequest(query="手机", top_k=3)
    )

    assert seen == ["supersearch"]
    assert result.requested_platforms == ["taobao"]
    assert result.succeeded_platforms == ["taobao"]
    assert result.failed_platforms == []


def test_search_intersects_model_platforms_with_enabled_platforms(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        seen.append(request.full_url.split("?", 1)[0].rsplit("/", 1)[-1])
        payload = {
            "code": 1,
            "data": [{"itemid": "1", "itemtitle": "淘宝手机", "itemprice": "10", "itempic": "https://img.example/tb.jpg"}],
        }
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = HaodankuProductSearchAdapter(HaodankuConfig(api_key="key")).search(
        ProductSearchRequest(query="手机", platforms=["淘宝", "京东", "拼多多"], top_k=3)
    )

    assert seen == ["supersearch"]
    assert result.requested_platforms == ["taobao"]
    assert result.failed_platforms == []


def test_search_rejects_only_disabled_platform_without_http(monkeypatch) -> None:
    calls = 0

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        raise AssertionError("disabled platform must not access provider")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = HaodankuProductSearchAdapter(HaodankuConfig(api_key="key")).search(
        ProductSearchRequest(query="手机", platforms=["京东"])
    )

    assert calls == 0
    assert result.success is False
    assert result.errors[0].code == "provider_platform_disabled"
    assert result.failed_platforms == []


def test_compare_converts_selected_offers_with_official_platform_endpoints(monkeypatch) -> None:
    calls: dict[str, dict[str, list[str]]] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        endpoint = request.full_url.rsplit("/", 1)[-1]
        from urllib.parse import parse_qs

        calls[endpoint] = parse_qs(request.data.decode())
        payload = {
            "ratesurl": {"code": 1, "data": {"coupon_click_url": "https://s.click.taobao.com/tb"}},
            "unify_jditems_link": {"code": 1, "data": {"clickURL": "https://u.jd.com/jd"}},
            "unify_pdditems_link": {"code": 1, "data": {"url": "https://mobile.yangkeduo.com/pdd"}},
        }[endpoint]
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    items = [
        ProductResult(product_id="tb:1", provider_item_id="1", title="淘宝手机", price=10, platform="taobao", product_url="https://item.taobao.com/item.htm?id=1", image_url="https://img.example/tb.jpg"),
        ProductResult(product_id="jd:2", provider_item_id="2", title="京东手机", price=11, platform="jd", product_url="https://item.jd.com/2.html", image_url="https://img.example/jd.jpg"),
        ProductResult(product_id="pdd:3", provider_item_id="3", title="拼多多手机", price=12, platform="pdd", product_url="https://mobile.yangkeduo.com/goods.html?goods_id=3", image_url="https://img.example/pdd.jpg"),
    ]
    adapter = HaodankuPriceCompareAdapter(
        HaodankuConfig(
            api_key="key",
            enabled_platforms=("taobao", "jd", "pdd"),
            taobao_pid="pid",
            taobao_authorized_name="name",
            jd_sub_union_id="sub",
            pdd_channel="channel",
        )
    )

    result = adapter.compare(PriceCompareRequest(query="手机", items=items, top_k=9))

    assert set(calls) == {"ratesurl", "unify_jditems_link", "unify_pdditems_link"}
    assert calls["ratesurl"]["pid"] == ["pid"]
    assert calls["ratesurl"]["tb_name"] == ["name"]
    assert calls["unify_jditems_link"]["subUnionId"] == ["sub"]
    assert calls["unify_pdditems_link"]["channel"] == ["channel"]
    assert [offer.url_status for offer in result.offers] == ["verified", "verified", "verified"]
    assert {offer.product_url for offer in result.offers} == {
        "https://s.click.taobao.com/tb",
        "https://u.jd.com/jd",
        "https://mobile.yangkeduo.com/pdd",
    }


def test_shopping_search_tool_treats_partial_platform_coverage_as_success() -> None:
    class PartialAdapter:
        def search(self, request):  # noqa: ANN001
            item = ProductResult(product_id="tb", title="手机", price=10, platform="taobao")
            error = ProductProviderError(code="provider_permission_denied", message="京东未授权")
            return ProductSearchResult(
                items=[item],
                provider="haodanku",
                errors=[error],
                requested_platforms=["taobao", "jd"],
                succeeded_platforms=["taobao"],
                failed_platforms=["jd"],
                platform_errors={"jd": [error]},
                total=1,
            )

    result = ShoppingSearchTool(
        search_adapter=PartialAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run({"query": "手机"})

    assert result.success is True
    assert result.contract is not None
    assert result.contract.status == "succeeded"
    assert result.data["search"]["failed_platforms"] == ["jd"]


def test_compare_returns_structured_no_products_when_all_searches_are_empty() -> None:
    class EmptySearch:
        def search(self, request):  # noqa: ANN001
            return ProductSearchResult(provider="haodanku", requested_platforms=["taobao", "jd", "pdd"])

    result = HaodankuPriceCompareAdapter(
        HaodankuConfig(api_key="key"), search_adapter=EmptySearch()
    ).compare(PriceCompareRequest(query="不存在的商品"))

    assert result.success is False
    assert result.errors[0].code == "price_no_products"


def test_compare_normalizes_chinese_platform_filters() -> None:
    item = ProductResult(
        product_id="tb:1",
        provider_item_id="1",
        title="蓝牙耳机",
        price=29.9,
        platform="taobao",
        product_url="https://item.taobao.com/item.htm?id=1",
        image_url="https://img.example/tb.jpg",
    )

    result = HaodankuPriceCompareAdapter(HaodankuConfig(api_key=None)).compare(
        PriceCompareRequest(
            query="蓝牙耳机",
            items=[item],
            platforms=["淘宝", "京东", "拼多多"],
            top_k=9,
        )
    )

    assert result.success is True
    assert [offer.platform for offer in result.offers] == ["taobao"]


def test_compare_rejects_only_disabled_platform_without_search() -> None:
    class UnexpectedSearch:
        def search(self, request):  # noqa: ANN001
            raise AssertionError("disabled platform must not search")

    result = HaodankuPriceCompareAdapter(
        HaodankuConfig(api_key="key"), search_adapter=UnexpectedSearch()
    ).compare(PriceCompareRequest(query="手机", platforms=["京东"]))

    assert result.success is False
    assert result.errors[0].code == "provider_platform_disabled"
