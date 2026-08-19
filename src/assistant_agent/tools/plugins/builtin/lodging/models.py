"""Provider-neutral lodging search and hotel-watch contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    field_validator,
    model_validator,
)


def _parse_iso_date(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid date in YYYY-MM-DD format") from exc


LodgingDate = Annotated[date, BeforeValidator(_parse_iso_date)]


class LodgingSearchInput(BaseModel):
    """Static model-visible lodging criteria without server-owned limits."""

    destination: str = Field(
        min_length=1,
        max_length=160,
        description="目的地城市或区域。",
    )
    check_in: LodgingDate = Field(description="入住日期 YYYY-MM-DD。")
    check_out: LodgingDate = Field(description="退房日期 YYYY-MM-DD。")
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
    keywords: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="酒店名称、品牌或偏好关键词。",
    )
    nearby_poi: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="希望靠近的景点、车站或其他地点。",
    )
    hotel_types: list[Literal["酒店", "民宿", "客栈"]] = Field(
        default_factory=list,
        max_length=3,
        description="住宿类型筛选。",
    )
    star_ratings: list[int] = Field(
        default_factory=list,
        max_length=5,
        description="酒店星级筛选，取值为 1 到 5。",
    )
    bed_types: list[Literal["大床房", "双床房", "多床房"]] = Field(
        default_factory=list,
        max_length=3,
        description="床型筛选。",
    )
    max_nightly_price: float | None = Field(
        default=None,
        gt=0,
        description="每晚最高预算。",
    )
    sort: Literal[
        "distance_asc",
        "rate_desc",
        "price_asc",
        "price_desc",
        "no_rank",
    ] = Field(
        default="no_rank",
        description="候选排序方式。",
    )
    @model_validator(mode="after")
    def validate_dates(self) -> "LodgingSearchInput":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be later than check_in")
        if any(star < 1 or star > 5 for star in self.star_ratings):
            raise ValueError("star_ratings must contain values from 1 to 5")
        if len(self.star_ratings) != len(set(self.star_ratings)):
            raise ValueError("star_ratings must not contain duplicates")
        return self


class LodgingSearchRequest(LodgingSearchInput):
    """Complete server-side lodging request with a bounded result limit."""

    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="内部候选数量上限。",
    )


class LodgingOffer(BaseModel):
    offer_id: str = Field(min_length=1)
    property_name: str = Field(min_length=1, max_length=200)
    nightly_price: float = Field(gt=0)
    total_price: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    price_basis: Literal["quoted_total", "nightly_estimate"] = "quoted_total"
    refundable: bool | None = None
    source_ref: str = Field(min_length=1)
    address: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    star: str | None = Field(default=None, max_length=80)
    score: float | None = Field(default=None, ge=0, le=5)
    review: str | None = Field(default=None, max_length=500)
    image_url: str | None = Field(default=None, max_length=2_000)
    booking_url: str | None = Field(default=None, max_length=2_000)

    @field_validator("image_url", "booking_url")
    @classmethod
    def validate_external_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("external hotel URLs must use http or https")
        return value


class LodgingSearchResult(BaseModel):
    success: bool
    provider: str = Field(min_length=1)
    offers: list[LodgingOffer] = Field(default_factory=list)
    observed_at: datetime
    output_ref: str | None = None
    provider_notice: str | None = Field(default=None, max_length=1_000)
    error_code: str | None = None
    error_message: str | None = None


class HotelPriceWatchGoal(BaseModel):
    search: LodgingSearchInput = Field(
        description="每次查价时重复使用的结构化住宿检索条件。",
    )
    max_nightly_price: float = Field(
        gt=0,
        description="最低每晚价不高于此阈值时发送通知。",
    )
    check_interval_s: int = Field(
        default=3600,
        ge=60,
        le=604_800,
        description="两次查价之间的秒数，范围为 60 到 604800。",
    )
    starts_at: datetime | None = Field(
        default=None,
        description="可选的带时区首次查价时间；缺省时立即开始。",
    )
    ends_at: datetime = Field(
        description="带时区的监控截止时间；超过后停止查价。",
    )
    notification_channel: str = Field(
        default="agent_service",
        min_length=1,
        max_length=80,
        description="已配置的传输无关通知通道标识。",
    )

    @model_validator(mode="after")
    def validate_goal(self) -> "HotelPriceWatchGoal":
        if self.ends_at.tzinfo is None:
            raise ValueError("ends_at must be timezone-aware")
        if self.starts_at is not None:
            if self.starts_at.tzinfo is None:
                raise ValueError("starts_at must be timezone-aware")
            if self.starts_at >= self.ends_at:
                raise ValueError("starts_at must be earlier than ends_at")
        if self.search.currency.strip() == "":
            raise ValueError("currency is required")
        return self
