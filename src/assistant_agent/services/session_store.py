"""Session/thread index storage."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from assistant_agent.agent.state import new_session_id
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.sessions import SessionCreate, SessionRecord


REPO_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("assistant_agent.services.session_store")


class SessionStore(Protocol):
    """Storage boundary for user-owned conversation thread metadata."""

    def create(self, session: SessionCreate, *, session_id: str | None = None) -> SessionRecord:
        """Create or replace a session index record."""

    def touch_run(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        trace_id: str,
        message_preview: str,
        status: str,
    ) -> SessionRecord:
        """Update session metadata after a run."""

    def get(self, user_id: str, session_id: str) -> SessionRecord | None:
        """Return a single session for one user."""

    def list_by_user(self, user_id: str) -> list[SessionRecord]:
        """Return sessions for one user."""

    def delete(self, user_id: str, session_id: str) -> bool:
        """Delete one session index record."""

    def delete_by_user(self, user_id: str) -> int:
        """Delete all sessions for one user."""


def create_session_store(config: ProviderConfig | None = None) -> SessionStore:
    """Create a session index store from runtime configuration."""

    resolved_config = config or ProviderConfig.from_env({})
    if resolved_config.conversation_history_backend == "jsonl":
        session_path = Path(resolved_config.conversation_history_path).with_name("sessions.jsonl")
        return JsonlSessionStore(_repo_relative_path(str(session_path)))
    return InMemorySessionStore()


class InMemorySessionStore:
    """In-memory session index."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], SessionRecord] = {}

    def create(self, session: SessionCreate, *, session_id: str | None = None) -> SessionRecord:
        record = _new_session_record(session, session_id=session_id)
        self._records[(record.user_id, record.session_id)] = record
        return record

    def touch_run(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        trace_id: str,
        message_preview: str,
        status: str,
    ) -> SessionRecord:
        existing = self.get(user_id, session_id)
        record = _touch_record(
            existing or _new_session_record(SessionCreate(user_id=user_id), session_id=session_id),
            run_id=run_id,
            trace_id=trace_id,
            message_preview=message_preview,
            status=status,
        )
        self._records[(user_id, session_id)] = record
        return record

    def get(self, user_id: str, session_id: str) -> SessionRecord | None:
        return self._records.get((user_id, session_id))

    def list_by_user(self, user_id: str) -> list[SessionRecord]:
        records = [record for (record_user_id, _), record in self._records.items() if record_user_id == user_id]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def delete(self, user_id: str, session_id: str) -> bool:
        return self._records.pop((user_id, session_id), None) is not None

    def delete_by_user(self, user_id: str) -> int:
        keys = [key for key in self._records if key[0] == user_id]
        for key in keys:
            self._records.pop(key, None)
        return len(keys)


class JsonlSessionStore:
    """JSONL-backed session index."""

    def __init__(self, path: Path | str = ".local/sessions/sessions.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def create(self, session: SessionCreate, *, session_id: str | None = None) -> SessionRecord:
        record = _new_session_record(session, session_id=session_id)
        self._upsert(record)
        return record

    def touch_run(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        trace_id: str,
        message_preview: str,
        status: str,
    ) -> SessionRecord:
        existing = self.get(user_id, session_id)
        record = _touch_record(
            existing or _new_session_record(SessionCreate(user_id=user_id), session_id=session_id),
            run_id=run_id,
            trace_id=trace_id,
            message_preview=message_preview,
            status=status,
        )
        self._upsert(record)
        return record

    def get(self, user_id: str, session_id: str) -> SessionRecord | None:
        for record in self.list_by_user(user_id):
            if record.session_id == session_id:
                return record
        return None

    def list_by_user(self, user_id: str) -> list[SessionRecord]:
        records = [record for record in self._read_all() if record.user_id == user_id]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def delete(self, user_id: str, session_id: str) -> bool:
        records = self._read_all()
        remaining = [
            record for record in records if not (record.user_id == user_id and record.session_id == session_id)
        ]
        if len(remaining) == len(records):
            return False
        self._write_all(remaining)
        return True

    def delete_by_user(self, user_id: str) -> int:
        records = self._read_all()
        remaining = [record for record in records if record.user_id != user_id]
        deleted = len(records) - len(remaining)
        if deleted:
            self._write_all(remaining)
        return deleted

    def _upsert(self, record: SessionRecord) -> None:
        records = [
            item
            for item in self._read_all()
            if not (item.user_id == record.user_id and item.session_id == record.session_id)
        ]
        records.append(record)
        self._write_all(records)

    def _read_all(self) -> list[SessionRecord]:
        if not self.path.exists():
            return []
        records: list[SessionRecord] = []
        with self.path.open("r", encoding="utf-8") as file:
            for lineno, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(SessionRecord.model_validate_json(line))
                except ValidationError:
                    logger.warning("Skipping invalid session record in %s at line %s", self.path, lineno)
        return records

    def _write_all(self, records: list[SessionRecord]) -> None:
        with self.path.open("w", encoding="utf-8") as file:
            for record in records:
                file.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")


def _new_session_record(session: SessionCreate, *, session_id: str | None = None) -> SessionRecord:
    resolved_session_id = session_id or new_session_id()
    now = datetime.now(timezone.utc)
    return SessionRecord(
        user_id=session.user_id,
        session_id=resolved_session_id,
        thread_id=resolved_session_id,
        title=session.title,
        metadata=session.metadata,
        created_at=now,
        updated_at=now,
    )


def _touch_record(
    record: SessionRecord,
    *,
    run_id: str,
    trace_id: str,
    message_preview: str,
    status: str,
) -> SessionRecord:
    preview = _preview(message_preview)
    return record.model_copy(
        update={
            "title": record.title or _default_title(preview),
            "run_count": record.run_count + 1,
            "last_run_id": run_id,
            "last_trace_id": trace_id,
            "last_message_preview": preview,
            "last_status": status,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def _preview(text: str, *, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _default_title(preview: str) -> str | None:
    if not preview:
        return None
    return preview if len(preview) <= 24 else preview[:23] + "…"


def _repo_relative_path(path: str) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return REPO_ROOT / resolved
