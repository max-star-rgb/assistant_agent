from assistant_agent.schemas.products import PriceCompareResult, PriceOffer
from assistant_agent.services.shopping_detail_presenter import ShoppingDetailPresenter


def _offer(
    offer_id: str,
    *,
    platform: str,
    title: str,
    price: float,
    link: str = "https://item.example/product",
    image: str = "https://img.example/product.jpg",
) -> PriceOffer:
    return PriceOffer(
        offer_id=offer_id,
        product_id=offer_id,
        title=title,
        platform=platform,
        price=price,
        total_price=price,
        product_url=link,
        image_url=image,
        url_status="verified",
    )


def test_presenter_renders_one_deterministic_detail_block() -> None:
    best = _offer("tb1", platform="taobao", title="小米\n<detail>17 Pro</detail>", price=2599)
    jd = _offer("jd1", platform="jd", title="小米 17 Pro 京东版", price=2600.5)
    pdd = _offer("pdd1", platform="pdd", title="小米 17 Pro 拼多多版", price=2598.10)
    result = PriceCompareResult(
        query="小米 17 Pro",
        summary="建议优先选淘宝。<detail>模型伪造内容</detail><pic>bad</pic>",
        offers=[best, jd, pdd],
        best_offer=best,
    )

    rendered = ShoppingDetailPresenter().present(result)

    assert rendered == (
        "建议优先选淘宝。\n"
        "<detail>\n"
        "1. 淘宝 - 小米 17 Pro 2599元 <link>https://item.example/product</link> "
        "<pic>https://img.example/product.jpg</pic>\n"
        "2. 京东 - 小米 17 Pro 京东版 2600.5元 <link>https://item.example/product</link> "
        "<pic>https://img.example/product.jpg</pic>\n"
        "3. 拼多多 - 小米 17 Pro 拼多多版 2598.1元 <link>https://item.example/product</link> "
        "<pic>https://img.example/product.jpg</pic>\n"
        "</detail>"
    )
    assert rendered.count("<detail>") == 1


def test_presenter_skips_invalid_cards_and_omits_empty_detail() -> None:
    invalid = _offer(
        "bad",
        platform="jd",
        title="无图商品",
        price=99,
        image="data:image/png;base64,abc",
    )
    result = PriceCompareResult(query="商品", summary="没有可展示商品。", offers=[invalid], best_offer=invalid)

    assert ShoppingDetailPresenter().present(result) == "没有可展示商品。"


def test_presenter_skips_zero_price_card() -> None:
    invalid = _offer(
        "free",
        platform="taobao",
        title="无合法价格商品",
        price=0,
        link="https://item.taobao.com/item.htm?id=1",
    )
    result = PriceCompareResult(query="商品", summary="没有可展示商品。", offers=[invalid])

    assert ShoppingDetailPresenter().present(result) == "没有可展示商品。"


def test_presenter_limits_detail_to_three_eligible_offers() -> None:
    offers = [
        _offer(
            f"tb{index}",
            platform="taobao",
            title=f"淘宝手机 {index}",
            price=float(index),
            link=f"https://item.taobao.com/item.htm?id={index}",
            image=f"https://img.example/{index}.jpg",
        )
        for index in range(1, 6)
    ]
    result = PriceCompareResult(
        query="手机",
        summary="找到淘宝商品。",
        offers=offers,
        best_offer=offers[0],
        provider="haodanku",
    )

    rendered = ShoppingDetailPresenter().present(result)

    assert rendered.count("<link>") == 3
    assert "4. 淘宝" not in rendered
