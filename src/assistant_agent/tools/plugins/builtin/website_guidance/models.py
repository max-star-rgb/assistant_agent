"""Provider-neutral data contracts for bounded website guidance."""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class WebPageInspectRequest(BaseModel):
    """Request a bounded observation of a public web page."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    goal: str = Field(min_length=1, max_length=500)


class WebPageExploreRequest(BaseModel):
    """Navigate an existing opaque browser session by element reference only."""

    model_config = ConfigDict(extra="forbid")

    browser_session_id: str = Field(min_length=16, max_length=128)
    action: Literal["inspect", "click", "back", "wait"]
    element_ref: str | None = Field(default=None, pattern=r"^e[1-9][0-9]*$")

    @model_validator(mode="after")
    def validate_action_input(self) -> "WebPageExploreRequest":
        if (self.action == "click") != (self.element_ref is not None):
            raise ValueError("element_ref is required only for click")
        return self


class WebPageElement(BaseModel):
    """A normalized, safe-to-reference page element."""

    ref: str = Field(pattern=r"^e[1-9][0-9]*$")
    role: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=1_000)
    href: str | None = Field(default=None, max_length=2_000)
    safe_action: Literal["click"]


class WebPageGuidanceError(BaseModel):
    """Stable, provider-neutral website guidance failure detail."""

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2_000)
    recoverable: bool = False


class WebPageGuidanceResult(BaseModel):
    """Bounded structured result for page inspection or exploration."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["success", "partial", "blocked", "failed"]
    url: HttpUrl
    requested_url: HttpUrl
    final_url: HttpUrl | None = None
    checked_at: datetime
    browser_session_id: str = Field(min_length=16, max_length=128)
    title: str = Field(default="", max_length=1_000)
    summary: str = Field(default="", max_length=4_000)
    content: str = ""
    elements: list[WebPageElement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[WebPageGuidanceError] = Field(default_factory=list)
    output_ref: str | None = Field(default=None, max_length=2_000)

    @field_validator("checked_at")
    @classmethod
    def checked_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_evidence_state(self) -> "WebPageGuidanceResult":
        effective_url = str(self.url)
        requested_url = str(self.requested_url)
        final_url = str(self.final_url) if self.final_url is not None else None
        if final_url is not None and effective_url != final_url:
            raise ValueError("url must match final_url when final_url is present")
        if self.outcome == "success":
            if final_url is None or requested_url != final_url:
                raise ValueError("success requires a verified, non-redirected final_url")
            if self.errors:
                raise ValueError("success cannot contain errors")
        elif self.outcome == "partial":
            if final_url is None or not self.warnings:
                raise ValueError("partial requires final_url and warnings")
        elif not self.errors:
            raise ValueError("blocked and failed results require errors")
        return self
