"""Provider-neutral lodging search and hotel-watch contracts."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class LodgingSearchRequest(BaseModel):
    destination: str = Field(
        min_length=1,
        max_length=160,
        description="目的地城市或区域。",
    )
    check_in: date = Field(description="入住日期 YYYY-MM-DD。")
    check_out: date = Field(description="退房日期 YYYY-MM-DD。")
    adults: int = Field(
        default=1,
        ge=1,
        le=16,
        description="成人数。",
    )
    rooms: int = Field(
        default=1,
        ge=1,
        le=8,
        description="房间数。",
    )
    currency: str = Field(
        default="CNY",
        min_length=3,
        max_length=3,
        description="三字母币种。",
    )

    @model_validator(mode="after")
    def validate_dates(self) -> "LodgingSearchRequest":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be later than check_in")
        return self


class LodgingOffer(BaseModel):
    offer_id: str = Field(min_length=1)
    property_name: str = Field(min_length=1, max_length=200)
    nightly_price: float = Field(gt=0)
    total_price: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    refundable: bool = False
    source_ref: str = Field(min_length=1)


class LodgingSearchResult(BaseModel):
    success: bool
    provider: str = Field(min_length=1)
    offers: list[LodgingOffer] = Field(default_factory=list)
    observed_at: datetime
    output_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class HotelPriceWatchGoal(BaseModel):
    search: LodgingSearchRequest = Field(
        description="Structured lodging query repeated by each check.",
    )
    max_nightly_price: float = Field(
        gt=0,
        description="Notify when the lowest nightly price is at or below this value.",
    )
    check_interval_s: int = Field(
        default=3600,
        ge=60,
        le=604_800,
        description="Seconds between bounded price checks.",
    )
    ends_at: datetime = Field(
        description="Timezone-aware deadline after which the watch stops.",
    )
    notification_channel: str = Field(
        default="mock_app",
        min_length=1,
        max_length=80,
        description="Configured transport-neutral notification channel.",
    )

    @model_validator(mode="after")
    def validate_goal(self) -> "HotelPriceWatchGoal":
        if self.ends_at.tzinfo is None:
            raise ValueError("ends_at must be timezone-aware")
        if self.search.currency.strip() == "":
            raise ValueError("currency is required")
        return self
