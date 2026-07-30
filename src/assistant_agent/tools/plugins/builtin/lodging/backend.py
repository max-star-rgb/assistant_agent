"""Plugin-private lodging provider adapters."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)


class LodgingSearchAdapter(Protocol):
    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult: ...


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class FlyAILodgingSearchAdapter:
    """Read-only FlyAI CLI adapter for structured hotel search results."""

    provider = "flyai"

    def __init__(
        self,
        *,
        cli_path: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self.cli_path = cli_path
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        observed_at = datetime.now(timezone.utc)
        command = _flyai_hotel_command(self.cli_path, request)
        try:
            completed = self.runner(
                command,
                shell=False,
                capture_output=True,
                text=True,
                env={**os.environ, "FLYAI_API_KEY": self.api_key},
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failed_flyai_result(
                observed_at,
                code="provider_timeout",
                message="FlyAI hotel search timed out.",
            )
        except FileNotFoundError:
            return _failed_flyai_result(
                observed_at,
                code="provider_unconfigured",
                message="FlyAI CLI executable was not found.",
            )
        except OSError:
            return _failed_flyai_result(
                observed_at,
                code="provider_unavailable",
                message="FlyAI CLI could not be started.",
            )

        if completed.returncode != 0:
            return _failed_flyai_result(
                observed_at,
                code="provider_unavailable",
                message="FlyAI hotel search command failed.",
            )
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return _failed_flyai_result(
                observed_at,
                code="provider_bad_response",
                message="FlyAI hotel search returned invalid JSON.",
            )
        if not isinstance(payload, dict):
            return _failed_flyai_result(
                observed_at,
                code="provider_bad_response",
                message="FlyAI hotel search response must be a JSON object.",
            )
        if payload.get("status") != 0:
            return _failed_flyai_result(
                observed_at,
                code="provider_unavailable",
                message=_safe_text(payload.get("message"))
                or "FlyAI hotel search was rejected.",
            )

        data = payload.get("data")
        items = data.get("itemList") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return _failed_flyai_result(
                observed_at,
                code="provider_bad_response",
                message="FlyAI hotel search response did not contain itemList.",
            )
        nights = (request.check_out - request.check_in).days
        offers: list[LodgingOffer] = []
        for index, item in enumerate(items[: request.limit], start=1):
            offer = _flyai_offer(item, index=index, nights=nights)
            if offer is not None:
                offers.append(offer)
        if items and not offers:
            return _failed_flyai_result(
                observed_at,
                code="provider_bad_response",
                message="FlyAI hotel search did not contain a valid hotel offer.",
            )
        return LodgingSearchResult(
            success=True,
            provider=self.provider,
            offers=offers,
            observed_at=observed_at,
            output_ref="flyai://lodging/search",
            provider_notice=_flyai_notice(payload.get("systemMessage")),
        )


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


def _flyai_hotel_command(
    cli_path: str,
    request: LodgingSearchRequest,
) -> list[str]:
    command = [
        cli_path,
        "search-hotel",
        "--dest-name",
        request.destination,
        "--check-in-date",
        request.check_in.isoformat(),
        "--check-out-date",
        request.check_out.isoformat(),
    ]
    optional: tuple[tuple[str, str | None], ...] = (
        ("--key-words", request.keywords),
        ("--poi-name", request.nearby_poi),
        ("--hotel-types", ",".join(request.hotel_types) or None),
        ("--hotel-stars", ",".join(str(item) for item in request.star_ratings) or None),
        ("--hotel-bed-types", ",".join(request.bed_types) or None),
        (
            "--max-price",
            _number_text(request.max_nightly_price)
            if request.max_nightly_price is not None
            else None,
        ),
        ("--sort", request.sort if request.sort != "no_rank" else None),
    )
    for flag, value in optional:
        if value:
            command.extend([flag, value])
    return command


def _flyai_offer(
    item: Any,
    *,
    index: int,
    nights: int,
) -> LodgingOffer | None:
    if not isinstance(item, dict):
        return None
    name = _safe_text(item.get("name"), max_length=200)
    price = _price_value(item.get("price"))
    if not name or price is None:
        return None
    offer_id = _safe_text(item.get("shId")) or f"flyai-hotel-{index}"
    booking_url = _safe_text(item.get("detailUrl"), max_length=2_000)
    source_ref = booking_url or f"flyai://hotel/{offer_id}"
    try:
        return LodgingOffer(
            offer_id=offer_id,
            property_name=name,
            nightly_price=price,
            total_price=price * nights,
            currency="CNY",
            price_basis="nightly_estimate",
            refundable=None,
            source_ref=source_ref,
            address=_safe_text(item.get("address"), max_length=300),
            latitude=_optional_float(item.get("latitude")),
            longitude=_optional_float(item.get("longitude")),
            star=_safe_text(item.get("star"), max_length=80),
            score=_optional_float(item.get("score")),
            review=_safe_text(item.get("review"), max_length=500),
            image_url=_safe_text(item.get("mainPic"), max_length=2_000),
            booking_url=booking_url,
        )
    except ValidationError:
        return None


def _price_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    if re.search(r"\d[\d,]*\s*[xX]{2}", value):
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    parsed = float(match.group(0).replace(",", ""))
    return parsed if parsed > 0 else None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _safe_text(value: Any, *, max_length: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:max_length] or None


def _flyai_notice(value: Any) -> str:
    provider_notice = _safe_text(value, max_length=700)
    confirmation = "展示价为每晚起价估算；价格、库存、入住人数和房型以 OTA 页面为准。"
    if not provider_notice:
        return confirmation
    return f"{provider_notice}；{confirmation}"[:1_000]


def _failed_flyai_result(
    observed_at: datetime,
    *,
    code: str,
    message: str,
) -> LodgingSearchResult:
    return LodgingSearchResult(
        success=False,
        provider="flyai",
        offers=[],
        observed_at=observed_at,
        output_ref="flyai://lodging/search",
        error_code=code,
        error_message=message,
    )
