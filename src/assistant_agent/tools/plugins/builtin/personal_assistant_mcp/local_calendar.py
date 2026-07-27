"""Local SQLite calendar adapter for development and evaluation."""

from __future__ import annotations

import json
import sqlite3
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant_agent.tools.plugins.builtin.personal_assistant_mcp.models import (
    CalendarCreateRequest,
    CalendarCreateResult,
    CalendarEvent,
    CalendarSearchRequest,
    CalendarSearchResult,
)


class LocalSQLiteCalendarAdapter:
    """Persist one user's calendar events without an external MCP service."""

    provider = "local_sqlite"

    def __init__(self, path: str | Path, *, namespace: str = "local") -> None:
        self.path = Path(path)
        self.namespace = namespace.strip()
        if not self.namespace:
            raise ValueError("Local calendar namespace must not be empty.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def for_namespace(self, namespace: str) -> LocalSQLiteCalendarAdapter:
        """Return a user-scoped view over the same database."""

        normalized = namespace.strip()
        if normalized == self.namespace:
            return self
        return type(self)(self.path, namespace=normalized)

    def search(self, request: CalendarSearchRequest) -> CalendarSearchResult:
        query = request.query.strip()
        normalized_query = query.casefold()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, title, start_time, end_time, timezone, location,
                       attendees_json
                FROM calendar_events
                WHERE namespace = ?
                  AND (? IS NULL OR start_time >= ?)
                  AND (? IS NULL OR start_time <= ?)
                ORDER BY start_time, event_id
                """,
                (
                    self.namespace,
                    request.start_time,
                    request.start_time,
                    request.end_time,
                    request.end_time,
                ),
            ).fetchall()
        events = [
            self._event_from_row(row)
            for row in rows
            if normalized_query in {"all", "today"}
            or normalized_query in str(row["title"]).casefold()
            or (
                row["location"] is not None
                and normalized_query in str(row["location"]).casefold()
            )
        ][: request.limit]
        return CalendarSearchResult(
            success=True,
            query_used=query,
            events=events,
            summary=f"Local calendar search returned {len(events)} event(s).",
            provider=self.provider,
            output_ref="sqlite://calendar/search",
            raw_data_ref="sqlite://calendar/events",
        )

    def create(self, request: CalendarCreateRequest) -> CalendarCreateResult:
        idempotency_key = (
            request.idempotency_key or self._semantic_idempotency_key(request)
        )
        existing = self._find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._create_result(existing, replayed=True)

        event_id = f"local-calendar-{uuid4().hex}"
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO calendar_events (
                        namespace, event_id, title, start_time, end_time,
                        timezone, location, attendees_json, notes,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.namespace,
                        event_id,
                        request.title,
                        request.start_time,
                        request.end_time,
                        request.timezone,
                        request.location,
                        json.dumps(request.attendees, ensure_ascii=False),
                        request.notes,
                        idempotency_key,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            return self._create_result(existing, replayed=True)

        event = {
            "event_id": event_id,
            "title": request.title,
            "start_time": request.start_time,
        }
        return self._create_result(event, replayed=False)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-compatible state snapshot for eval evidence."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, title, start_time, end_time, timezone, location,
                       attendees_json, notes, idempotency_key, created_at
                FROM calendar_events
                WHERE namespace = ?
                ORDER BY start_time, event_id
                """,
                (self.namespace,),
            ).fetchall()
        return {
            "schema_version": "local_calendar_state_v1",
            "namespace": self.namespace,
            "events": [
                {
                    "event_id": row["event_id"],
                    "title": row["title"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "timezone": row["timezone"],
                    "location": row["location"],
                    "attendees": json.loads(row["attendees_json"]),
                    "notes": row["notes"],
                    "idempotency_key": row["idempotency_key"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        before_events = _events_by_id(before)
        after_events = _events_by_id(after)
        return {
            "schema_version": "local_calendar_state_diff_v1",
            "added": [
                after_events[event_id]
                for event_id in sorted(after_events.keys() - before_events.keys())
            ],
            "modified": [
                {
                    "event_id": event_id,
                    "before": before_events[event_id],
                    "after": after_events[event_id],
                }
                for event_id in sorted(after_events.keys() & before_events.keys())
                if before_events[event_id] != after_events[event_id]
            ],
            "deleted": [
                before_events[event_id]
                for event_id in sorted(before_events.keys() - after_events.keys())
            ],
            "duplicate_groups": _duplicate_groups(list(after_events.values())),
        }

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_events (
                    namespace TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    timezone TEXT,
                    location TEXT,
                    attendees_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, event_id)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_calendar_events_namespace_idempotency
                ON calendar_events(namespace, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_calendar_events_namespace_start
                ON calendar_events(namespace, start_time)
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT event_id, title, start_time
                FROM calendar_events
                WHERE namespace = ? AND idempotency_key = ?
                """,
                (self.namespace, idempotency_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def _semantic_idempotency_key(
        self,
        request: CalendarCreateRequest,
    ) -> str:
        payload = json.dumps(
            {
                "namespace": self.namespace,
                "title": request.title,
                "start_time": request.start_time,
                "end_time": request.end_time,
                "timezone": request.timezone,
                "location": request.location,
                "attendees": request.attendees,
                "notes": request.notes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"semantic:{hashlib.sha256(payload).hexdigest()}"

    def _event_from_row(self, row: sqlite3.Row) -> CalendarEvent:
        return CalendarEvent(
            event_id=row["event_id"],
            title=row["title"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            timezone=row["timezone"],
            location=row["location"],
            attendee_count=len(json.loads(row["attendees_json"])),
        )

    def _create_result(
        self,
        event: dict[str, Any],
        *,
        replayed: bool,
    ) -> CalendarCreateResult:
        return CalendarCreateResult(
            success=True,
            event_id=str(event["event_id"]),
            title=str(event["title"]),
            start_time=str(event["start_time"]),
            summary=(
                f"Reused local calendar event: {event['title']}"
                if replayed
                else f"Created local calendar event: {event['title']}"
            ),
            side_effect_level="idempotent_replay" if replayed else "committed",
            provider=self.provider,
            output_ref=f"sqlite://calendar/events/{event['event_id']}",
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
