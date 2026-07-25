"""Deterministic stateful Calendar environment for Langfuse experiments."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.schemas.personal_assistant import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarEvent,
    CalendarSearchRequest,
    CalendarSearchResult,
)
from assistant_agent.tools.input_binding import ToolInputBinding
from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.tools import (
    CalendarCreateTool,
)


class EvalCalendarEvent(BaseModel):
    """Full calendar state retained by the evaluation environment."""

    event_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None
    idempotency_key: str | None = None


class CalendarEvalCreateTool(CalendarCreateTool):
    """Calendar create Tool with a run-owned key for deterministic experiments."""

    input_bindings = (
        ToolInputBinding(
            field="idempotency_key",
            source="runtime_identity",
            key="run_id",
        ),
    )


class CalendarEvalEnvironment:
    """Calendar adapter plus reset, snapshot, and diff state probes."""

    provider = "eval_fixture"

    def __init__(self, events: list[EvalCalendarEvent] | None = None) -> None:
        self._seed_events = [event.model_copy(deep=True) for event in (events or [])]
        self._events: list[EvalCalendarEvent] = []
        self._idempotency: dict[str, str] = {}
        self.reset()

    def reset(self) -> None:
        """Restore the deterministic initial state."""

        self._events = [event.model_copy(deep=True) for event in self._seed_events]
        self._idempotency = {
            event.idempotency_key: event.event_id
            for event in self._events
            if event.idempotency_key
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a stable, JSON-compatible state snapshot."""

        events = sorted(self._events, key=lambda event: (event.start_time, event.event_id))
        return {
            "schema_version": "calendar_eval_state_v1",
            "events": [event.model_dump(mode="json") for event in events],
        }

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        """Describe event additions, modifications, deletions, and duplicates."""

        before_by_id = _events_by_id(before)
        after_by_id = _events_by_id(after)
        added = [
            deepcopy(after_by_id[event_id])
            for event_id in sorted(after_by_id.keys() - before_by_id.keys())
        ]
        deleted = [
            deepcopy(before_by_id[event_id])
            for event_id in sorted(before_by_id.keys() - after_by_id.keys())
        ]
        modified = [
            {
                "event_id": event_id,
                "before": deepcopy(before_by_id[event_id]),
                "after": deepcopy(after_by_id[event_id]),
            }
            for event_id in sorted(before_by_id.keys() & after_by_id.keys())
            if before_by_id[event_id] != after_by_id[event_id]
        ]
        duplicate_groups = _duplicate_groups(list(after_by_id.values()))
        return {
            "schema_version": "calendar_eval_state_diff_v1",
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "duplicate_groups": duplicate_groups,
        }

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        """Search the current fixture state through the production adapter contract."""

        query = request.query.strip().casefold()
        matches = [
            event
            for event in self._events
            if query in {"today", "all"}
            or query in event.title.casefold()
            or (event.location is not None and query in event.location.casefold())
        ][: request.limit]
        return CalendarSearchResult(
            success=True,
            query_used=request.query,
            events=[
                CalendarEvent(
                    event_id=event.event_id,
                    title=event.title,
                    start_time=event.start_time,
                    end_time=event.end_time,
                    timezone=event.timezone,
                    location=event.location,
                    attendee_count=len(event.attendees),
                )
                for event in matches
            ],
            summary=f"Calendar search returned {len(matches)} event(s).",
            provider=self.provider,
            output_ref="eval://calendar/search",
            raw_data_ref="eval://calendar/state",
        )

    def create(self, request: CalendarCreateRequest) -> CalendarCreateResult:
        """Create exactly one event, with deterministic idempotent replay."""

        if request.idempotency_key and request.idempotency_key in self._idempotency:
            event = self._event(self._idempotency[request.idempotency_key])
            return _create_result(event, replayed=True)
        event_id = f"eval-calendar-{len(self._events) + 1:04d}"
        event = EvalCalendarEvent(
            event_id=event_id,
            title=request.title,
            start_time=request.start_time,
            end_time=request.end_time,
            timezone=request.timezone,
            location=request.location,
            attendees=list(request.attendees),
            notes=request.notes,
            idempotency_key=request.idempotency_key,
        )
        self._events.append(event)
        if request.idempotency_key:
            self._idempotency[request.idempotency_key] = event.event_id
        return _create_result(event, replayed=False)

    def _event(self, event_id: str) -> EvalCalendarEvent:
        return next(event for event in self._events if event.event_id == event_id)


def _create_result(
    event: EvalCalendarEvent,
    *,
    replayed: bool,
) -> CalendarCreateResult:
    return CalendarCreateResult(
        success=True,
        event_id=event.event_id,
        title=event.title,
        start_time=event.start_time,
        summary=(
            f"Reused calendar event: {event.title}"
            if replayed
            else f"Created calendar event: {event.title}"
        ),
        side_effect_level="idempotent_replay" if replayed else "committed",
        provider=CalendarEvalEnvironment.provider,
        output_ref=f"eval://calendar/events/{event.event_id}",
    )


def _events_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    events = snapshot.get("events")
    if not isinstance(events, list):
        return {}
    return {
        str(event["event_id"]): dict(event)
        for event in events
        if isinstance(event, dict) and event.get("event_id")
    }


def _duplicate_groups(events: list[dict[str, Any]]) -> list[list[str]]:
    groups: dict[tuple[Any, ...], list[str]] = {}
    for event in events:
        semantic_key = (
            event.get("title"),
            event.get("start_time"),
            event.get("end_time"),
            event.get("location"),
            event.get("notes"),
        )
        groups.setdefault(semantic_key, []).append(str(event.get("event_id")))
    return [
        sorted(event_ids)
        for event_ids in groups.values()
        if len(event_ids) > 1
    ]
