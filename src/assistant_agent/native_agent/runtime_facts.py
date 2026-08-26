"""Run-scoped trusted facts captured and frozen by the parent graph."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, field_validator


DEFAULT_RUNTIME_TIMEZONE = "Asia/Shanghai"


class RuntimeLocation(BaseModel):
    """One trusted location fact and its provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    timezone: str
    source: Literal["deployment_default"]
    is_fallback: bool


class TrustedRuntimeFacts(BaseModel):
    """Facts sampled once when the capture node runs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["trusted_runtime_facts_v1"] = "trusted_runtime_facts_v1"
    current_time: datetime
    timezone: str
    current_location: RuntimeLocation

    @field_validator("current_time", mode="before")
    @classmethod
    def _parse_checkpoint_time(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @field_validator("current_time")
    @classmethod
    def _require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("current_time must be timezone-aware")
        return value


def capture_trusted_runtime_facts(
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Capture one JSON-safe trusted runtime facts snapshot."""

    timezone = ZoneInfo(DEFAULT_RUNTIME_TIMEZONE)
    current_time = (clock or (lambda: datetime.now(timezone)))()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("trusted runtime clock must return a timezone-aware datetime")
    current_time = current_time.astimezone(timezone)
    facts = TrustedRuntimeFacts(
        current_time=current_time,
        timezone=DEFAULT_RUNTIME_TIMEZONE,
        current_location=RuntimeLocation(
            name="上海市青浦区华为练秋湖研发中心",
            timezone=DEFAULT_RUNTIME_TIMEZONE,
            source="deployment_default",
            is_fallback=True,
        ),
    )
    return facts.model_dump(mode="json")


def trusted_runtime_facts_message(
    facts: TrustedRuntimeFacts | Mapping[str, Any] | None,
) -> HumanMessage | None:
    """Render the frozen snapshot as ephemeral, non-instructional context."""

    if facts is None:
        return None
    if not isinstance(facts, TrustedRuntimeFacts):
        facts = TrustedRuntimeFacts.model_validate(facts)
    location = facts.current_location
    local_time = facts.current_time.astimezone(ZoneInfo(facts.timezone))
    content = (
        f"- 当前时间: {local_time.isoformat(sep=' ')}\n"
        f"- 时区: {facts.timezone}\n"
        f"- 用户当前地点: {location.name}\n\n"
        "这是当前用户所处的时间和地点。"
    )
    return HumanMessage(content=content)


__all__ = [
    "DEFAULT_RUNTIME_TIMEZONE",
    "RuntimeLocation",
    "TrustedRuntimeFacts",
    "capture_trusted_runtime_facts",
    "trusted_runtime_facts_message",
]
