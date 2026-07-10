"""Tool call history storage."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallHistoryRecord(BaseModel):
    """Serializable record for one tool call lifecycle event."""

    run_id: str = Field(min_length=1)
    user_id: str | None = None
    session_id: str | None = None
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: Literal["started", "succeeded", "failed"]
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    audit_payload: dict[str, Any] = Field(default_factory=dict)
    raw_data_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolHistoryStore:
    """JSONL-backed tool call history store."""

    def __init__(self, path: Path | str = ".data/tool_calls.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_start(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        input_summary: dict[str, Any],
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> ToolCallHistoryRecord:
        record = ToolCallHistoryRecord(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            status="started",
            input_summary=input_summary,
        )
        self.append(record)
        return record

    def record_end(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        status: Literal["succeeded", "failed"],
        latency_ms: int,
        output_ref: str | None = None,
        error: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        output_summary: dict[str, Any] | None = None,
        audit_payload: dict[str, Any] | None = None,
        raw_data_ref: str | None = None,
    ) -> ToolCallHistoryRecord:
        record = ToolCallHistoryRecord(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            status=status,
            output_summary=output_summary or {},
            audit_payload=audit_payload or {},
            raw_data_ref=raw_data_ref,
            output_ref=output_ref,
            error=error,
            latency_ms=latency_ms,
        )
        self.append(record)
        return record

    def append(self, record: ToolCallHistoryRecord) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def read_all(self) -> list[ToolCallHistoryRecord]:
        if not self.path.exists():
            return []
        records: list[ToolCallHistoryRecord] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    records.append(ToolCallHistoryRecord.model_validate_json(line))
        return records

    def list_by_user(self, user_id: str) -> list[ToolCallHistoryRecord]:
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
