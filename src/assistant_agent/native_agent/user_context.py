"""Dynamic system-prompt context derived from trusted user configuration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING_TIMEZONE = "Asia/Shanghai"
_UNCONFIGURED_LOCATION = "未配置"


def render_user_characteristics_section(
    *,
    current_location: str | None,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Render exactly two user-characteristic facts at day granularity."""

    timezone = ZoneInfo(BEIJING_TIMEZONE)
    current_time = (clock or (lambda: datetime.now(timezone)))()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("user-context clock must return a timezone-aware datetime")
    current_date = current_time.astimezone(timezone).date().isoformat()
    location = " ".join((current_location or "").split())
    return (
        "用户特性：\n"
        f"- 当前日期（北京时间）：{current_date}\n"
        f"- 用户所处地区：{location or _UNCONFIGURED_LOCATION}"
    )


__all__ = [
    "BEIJING_TIMEZONE",
    "render_user_characteristics_section",
]
