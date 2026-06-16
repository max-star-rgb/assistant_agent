from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.product_adapter import (
    HttpProductSearchAdapter,
    LocalJsonProductSearchAdapter,
    MockProductSearchAdapter,
    ProductSearchInput,
    UnconfiguredProductSearchAdapter,
    create_product_search_adapter,
)


def test_create_product_search_adapter_defaults_to_mock() -> None:
    adapter = create_product_search_adapter(ProviderConfig())

    assert isinstance(adapter, MockProductSearchAdapter)
    result = adapter.search(ProductSearchInput(query="白色低帮运动鞋"))
    assert result.provider == "mock"
    assert result.success is True


def test_create_product_search_adapter_selects_local_json_when_configured(tmp_path) -> None:
    data_file = tmp_path / "products.json"
    data_file.write_text("[]", encoding="utf-8")

    adapter = create_product_search_adapter(
        ProviderConfig(product_search_provider="local_json", product_search_local_path=str(data_file))
    )

    assert isinstance(adapter, LocalJsonProductSearchAdapter)


def test_local_json_provider_without_path_returns_provider_unconfigured() -> None:
    adapter = create_product_search_adapter(ProviderConfig(product_search_provider="local_json"))

    assert isinstance(adapter, UnconfiguredProductSearchAdapter)
    result = adapter.search(ProductSearchInput(query="白色运动鞋"))
    assert result.success is False
    assert result.provider == "local_json"
    assert result.errors[0].code == "provider_unconfigured"


def test_http_provider_missing_config_returns_provider_unconfigured_without_network() -> None:
    adapter = create_product_search_adapter(ProviderConfig(product_search_provider="http"))

    assert isinstance(adapter, HttpProductSearchAdapter)
    result = adapter.search(ProductSearchInput(query="白色运动鞋"))
    assert result.success is False
    assert result.provider == "http"
    assert result.errors[0].code == "provider_unconfigured"
    assert "PRODUCT_SEARCH_BASE_URL" in result.errors[0].message
    assert "PRODUCT_SEARCH_API_KEY" in result.errors[0].message


def test_http_provider_skeleton_does_not_call_real_provider_when_configured() -> None:
    adapter = create_product_search_adapter(
        ProviderConfig(
            product_search_provider="http",
            product_search_base_url="https://provider.invalid/search",
            product_search_api_key="test-key",
        )
    )

    result = adapter.search(ProductSearchInput(query="白色运动鞋"))

    assert result.success is False
    assert result.provider == "http"
    assert result.errors[0].code == "provider_unavailable"
