from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.products import ProductResult
from multimodal_agent.services.product_adapter import (
    HttpPriceCompareAdapter,
    LocalPriceCompareAdapter,
    MockPriceCompareAdapter,
    PriceCompareInput,
    create_price_compare_adapter,
)


def products() -> list[ProductResult]:
    return [
        ProductResult(
            product_id="p1",
            title="白色低帮运动鞋 A",
            price=299.0,
            platform="mock-shop-a",
            similarity=0.92,
            rating=4.7,
            reason="相似度最高",
        ),
        ProductResult(
            product_id="p2",
            title="简约白色板鞋 B",
            price=259.0,
            platform="mock-shop-b",
            similarity=0.86,
            rating=4.5,
            reason="价格更低",
        ),
    ]


def test_mock_price_compare_returns_structured_error_without_products() -> None:
    result = MockPriceCompareAdapter().compare(PriceCompareInput(items=[]))

    assert result.success is False
    assert result.provider == "mock"
    assert result.errors[0].code == "price_no_products"
    assert result.errors[0].recoverable is True
    assert result.offers == []


def test_mock_price_compare_turns_products_into_offers() -> None:
    result = MockPriceCompareAdapter().compare(PriceCompareInput(items=products(), query="白色运动鞋"))

    assert result.success is True
    assert result.provider == "mock"
    assert [offer.product_id for offer in result.offers] == ["p2", "p1"]
    assert result.best_offer is not None
    assert result.best_offer.product_id == "p2"
    assert result.best_value_product_id == "p2"
    assert result.ranking_reason


def test_mock_price_compare_filters_by_budget() -> None:
    result = MockPriceCompareAdapter().compare(
        PriceCompareInput(items=products(), budget_max=280, query="白色运动鞋")
    )

    assert result.success is True
    assert [offer.product_id for offer in result.offers] == ["p2"]
    assert result.best_offer is not None
    assert result.best_offer.total_price <= 280


def test_mock_price_compare_returns_stable_no_offers_error_code() -> None:
    result = MockPriceCompareAdapter().compare(
        PriceCompareInput(items=products(), budget_max=100, query="白色运动鞋")
    )

    assert result.success is False
    assert result.errors[0].code == "price_no_offers"


def test_create_price_compare_adapter_defaults_to_mock() -> None:
    adapter = create_price_compare_adapter(ProviderConfig())

    assert isinstance(adapter, MockPriceCompareAdapter)


def test_create_price_compare_adapter_selects_local() -> None:
    adapter = create_price_compare_adapter(ProviderConfig(price_compare_provider="local"))

    assert isinstance(adapter, LocalPriceCompareAdapter)


def test_http_price_compare_missing_config_returns_provider_unconfigured_without_network() -> None:
    adapter = create_price_compare_adapter(ProviderConfig(price_compare_provider="http"))

    assert isinstance(adapter, HttpPriceCompareAdapter)
    result = adapter.compare(PriceCompareInput(items=products(), query="白色运动鞋"))

    assert result.success is False
    assert result.provider == "http"
    assert result.errors[0].code == "provider_unconfigured"
    assert "PRICE_COMPARE_BASE_URL" in result.errors[0].message
    assert "PRICE_COMPARE_API_KEY" in result.errors[0].message


def test_http_price_compare_skeleton_does_not_call_real_provider_when_configured() -> None:
    adapter = create_price_compare_adapter(
        ProviderConfig(
            price_compare_provider="http",
            price_compare_base_url="https://provider.invalid/compare",
            price_compare_api_key="test-key",
        )
    )

    result = adapter.compare(PriceCompareInput(items=products(), query="白色运动鞋"))

    assert result.success is False
    assert result.provider == "http"
    assert result.errors[0].code == "provider_unavailable"
