"""Shanghai-calendar windows for daily runtime audits."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel


AUDIT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DailyAuditWindow(BaseModel):
    audit_date: date
    start_utc: datetime
    end_utc: datetime


def window_for_date(audit_date: date) -> DailyAuditWindow:
    local_start = datetime.combine(audit_date, time.min, tzinfo=AUDIT_TIMEZONE)
    local_end = local_start + timedelta(days=1)
    return DailyAuditWindow(
        audit_date=audit_date,
        start_utc=local_start.astimezone(timezone.utc),
        end_utc=local_end.astimezone(timezone.utc),
    )


def previous_day_window(now: datetime) -> DailyAuditWindow:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    audit_date = now.astimezone(AUDIT_TIMEZONE).date() - timedelta(days=1)
    return window_for_date(audit_date)


def pending_audit_dates(*, yesterday: date, last_completed: date | None) -> list[date]:
    start = yesterday if last_completed is None else last_completed + timedelta(days=1)
    if start > yesterday:
        return []
    return [start + timedelta(days=offset) for offset in range((yesterday - start).days + 1)]
