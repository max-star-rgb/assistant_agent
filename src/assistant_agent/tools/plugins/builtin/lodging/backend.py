"""Plugin-private lodging provider adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)


class LodgingSearchAdapter(Protocol):
    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult: ...


class MockLodgingSearchAdapter:
    """Deterministic offline lodging inventory."""

    provider = "mock"

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        nights = (request.check_out - request.check_in).days
        nightly_price = 680.0
        offer = LodgingOffer(
            offer_id="mock-hotel-001",
            property_name="Mock Riverside Hotel",
            nightly_price=nightly_price,
            total_price=nightly_price * nights,
            currency=request.currency,
            refundable=True,
            source_ref="mock://lodging/mock-hotel-001",
        )
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=[offer],
            observed_at=datetime.now(timezone.utc),
            output_ref="mock://lodging/search",
        )


class SequenceLodgingSearchAdapter:
    """Deterministic sequence adapter used by restart-safe scenario tests."""

    provider = "mock_sequence"

    def __init__(
        self,
        nightly_prices: list[float | None],
        *,
        property_name: str = "Sequence Hotel",
    ) -> None:
        if not nightly_prices:
            raise ValueError("nightly_prices must not be empty")
        self._prices = list(nightly_prices)
        self._last = nightly_prices[-1]
        self.property_name = property_name

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        price = self._prices.pop(0) if self._prices else self._last
        self._last = price
        observed_at = datetime.now(timezone.utc)
        if price is None:
            return LodgingSearchResult(
                success=False,
                provider=self.provider,
                observed_at=observed_at,
                error_code="provider_timeout",
                error_message="The mock lodging provider timed out.",
            )
        nights = (request.check_out - request.check_in).days
        offer = LodgingOffer(
            offer_id="sequence-hotel-001",
            property_name=self.property_name,
            nightly_price=price,
            total_price=price * nights,
            currency=request.currency,
            refundable=True,
            source_ref="mock://lodging/sequence-hotel-001",
        )
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=[offer],
            observed_at=observed_at,
            output_ref="mock://lodging/sequence",
        )
