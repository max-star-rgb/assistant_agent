"""Agent run history storage."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class RunHistoryRecord(BaseModel):
    """Serializable record for one agent run lifecycle event."""

    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    status: Literal["started", "completed", "failed", "cancelled"]
    intent: str | None = None
    selected_tools: list[str] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunHistoryStore:
    """JSONL-backed run history store."""

    def __init__(self, path: Path | str = ".data/runs.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_start(
        self,
        run_id: str,
        user_id: str,
        session_id: str,
    ) -> RunHistoryRecord:
        record = RunHistoryRecord(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            status="started",
        )
        self.append(record)
        return record

    def record_end(
        self,
        run_id: str,
        user_id: str,
        session_id: str,
        status: Literal["completed", "failed", "cancelled"],
        intent: str | None,
        selected_tools: list[str],
        latency_ms: int,
        error: str | None = None,
    ) -> RunHistoryRecord:
        record = RunHistoryRecord(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            status=status,
            intent=intent,
            selected_tools=selected_tools,
            latency_ms=latency_ms,
            error=error,
        )
        self.append(record)
        return record

    def append(self, record: RunHistoryRecord) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def read_all(self) -> list[RunHistoryRecord]:
        if not self.path.exists():
            return []
        records: list[RunHistoryRecord] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    records.append(RunHistoryRecord.model_validate_json(line))
        return records

    def list_by_user(self, user_id: str) -> list[RunHistoryRecord]:
        return [record for record in self.read_all() if record.user_id == user_id]

    def delete_by_user(self, user_id: str) -> int:
        records = self.read_all()
        remaining = [record for record in records if record.user_id != user_id]
        deleted = len(records) - len(remaining)
        if deleted:
            with self.path.open("w", encoding="utf-8") as file:
                for record in remaining:
                    file.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return deleted
