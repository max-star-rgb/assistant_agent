"""Plugin-private real product search and price comparison adapters."""

from typing import Protocol

from assistant_agent.config import ShoppingConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareRequest,
    PriceCompareResult,
    ProductProviderError,
    ProductSearchRequest,
    ProductSearchResult,
)


class ProductSearchAdapter(Protocol):
    """Adapter contract for real product search providers."""

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        """Return structured product candidates."""


class PriceCompareAdapter(Protocol):
    """Adapter contract for real price comparison providers."""

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        """Return structured price offers."""


class HttpProductSearchAdapter:
    """Configured HTTP shopping search boundary."""

    provider = "http"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        return _failed_search_result(
            provider=self.provider,
            code="provider_unavailable",
            message=(
                "generic HTTP shopping search has no configured response contract; "
                "use the Haodanku provider."
            ),
            recoverable=False,
            query=request.query,
        )


class HttpPriceCompareAdapter:
    """Configured HTTP shopping comparison boundary."""

    provider = "http"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        return _failed_price_result(
            provider=self.provider,
            query=request.query,
            code="provider_unavailable",
            message=(
                "generic HTTP shopping compare has no configured response contract; "
                "use the Haodanku provider."
            ),
            recoverable=False,
        )


def create_shopping_search_adapter(
    config: ShoppingConfig,
    *,
    provider_mode: ProviderMode,
) -> ProductSearchAdapter:
    """Create only explicitly configured real shopping search adapters."""

    if provider_mode != "real":
        raise ValueError("real provider mode is required for shopping search adapters")
    if config.shopping_search_provider == "haodanku":
        if not config.haodanku_api_key:
            raise ValueError(
                "configured real shopping search provider requires HAODANKU_API_KEY"
            )
        from assistant_agent.tools.plugins.builtin.shopping.haodanku import (
            HaodankuConfig,
            HaodankuProductSearchAdapter,
        )

        return HaodankuProductSearchAdapter(
            HaodankuConfig(
                api_key=config.haodanku_api_key,
                base_url=config.haodanku_base_url,
                timeout_seconds=config.haodanku_timeout_seconds,
                enabled_platforms=config.haodanku_enabled_platforms,
                taobao_pid=config.haodanku_taobao_pid,
                taobao_authorized_name=config.haodanku_taobao_authorized_name,
                jd_sub_union_id=config.haodanku_jd_sub_union_id,
                pdd_channel=config.haodanku_pdd_channel,
            )
        )
    if config.shopping_search_provider == "http":
        if not config.shopping_search_base_url or not config.shopping_search_api_key:
            raise ValueError(
                "configured real shopping search provider requires "
                "SHOPPING_SEARCH_BASE_URL and SHOPPING_SEARCH_API_KEY"
            )
        return HttpProductSearchAdapter(
            base_url=config.shopping_search_base_url,
            api_key=config.shopping_search_api_key,
            timeout_seconds=config.shopping_search_timeout_seconds,
        )
    raise ValueError("configured real shopping search provider is required")


def create_shopping_compare_adapter(
    config: ShoppingConfig,
    *,
    provider_mode: ProviderMode,
) -> PriceCompareAdapter:
    """Create only explicitly configured real shopping comparison adapters."""

    if provider_mode != "real":
        raise ValueError("real provider mode is required for shopping compare adapters")
    if config.shopping_compare_provider == "haodanku":
        if not config.haodanku_api_key:
            raise ValueError(
                "configured real shopping compare provider requires HAODANKU_API_KEY"
            )
        from assistant_agent.tools.plugins.builtin.shopping.haodanku import (
            HaodankuConfig,
            HaodankuPriceCompareAdapter,
        )

        return HaodankuPriceCompareAdapter(
            HaodankuConfig(
                api_key=config.haodanku_api_key,
                base_url=config.haodanku_base_url,
                timeout_seconds=config.haodanku_timeout_seconds,
                enabled_platforms=config.haodanku_enabled_platforms,
                taobao_pid=config.haodanku_taobao_pid,
                taobao_authorized_name=config.haodanku_taobao_authorized_name,
                jd_sub_union_id=config.haodanku_jd_sub_union_id,
                pdd_channel=config.haodanku_pdd_channel,
            )
        )
    if config.shopping_compare_provider == "http":
        if not config.shopping_compare_base_url or not config.shopping_compare_api_key:
            raise ValueError(
                "configured real shopping compare provider requires "
                "SHOPPING_COMPARE_BASE_URL and SHOPPING_COMPARE_API_KEY"
            )
        return HttpPriceCompareAdapter(
            base_url=config.shopping_compare_base_url,
            api_key=config.shopping_compare_api_key,
            timeout_seconds=config.shopping_compare_timeout_seconds,
        )
    raise ValueError("configured real shopping compare provider is required")


def _failed_search_result(
    *,
    provider: str,
    code: str,
    message: str,
    recoverable: bool,
    query: str,
) -> ProductSearchResult:
    return ProductSearchResult(
        provider=provider,
        query_used=query,
        errors=[
            ProductProviderError(
                code=code,
                message=message,
                recoverable=recoverable,
            )
        ],
    )


def _failed_price_result(
    *,
    provider: str,
    query: str,
    code: str,
    message: str,
    recoverable: bool,
) -> PriceCompareResult:
    return PriceCompareResult(
        query=query,
        summary=message,
        provider=provider,
        errors=[
            ProductProviderError(
                code=code,
                message=message,
                recoverable=recoverable,
            )
        ],
    )
