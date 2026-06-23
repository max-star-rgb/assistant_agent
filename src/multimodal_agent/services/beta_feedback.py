"""Small-scope beta feedback storage and evaluation export."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from multimodal_agent.services.provider_errors import sanitize_error_message


FeedbackRating = Literal["up", "down"]
FeedbackCategory = Literal[
    "wrong_answer",
    "bad_tool_choice",
    "missing_tool_call",
    "unnecessary_tool",
    "bad_followup",
    "slow",
    "crash",
    "privacy_concern",
    "other",
]


class BetaFeedbackCreate(BaseModel):
    """User feedback payload for one beta run."""

    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    rating: FeedbackRating
    category: FeedbackCategory = "other"
    note: str = Field(default="", max_length=1000)

    @field_validator("note")
    @classmethod
    def sanitize_note(cls, value: str) -> str:
        return sanitize_error_message(value.strip())


class BetaFeedbackRecord(BetaFeedbackCreate):
    """Stored beta feedback record."""

    feedback_id: str = Field(default_factory=lambda: f"fb_{uuid4().hex}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BetaFeedbackSummary(BaseModel):
    """Aggregate feedback metrics for beta evaluation."""

    user_id: str | None = None
    total: int = 0
    up: int = 0
    down: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)


class BetaEvaluationItem(BaseModel):
    """One feedback item joined with a redacted run summary."""

    feedback: BetaFeedbackRecord
    run: dict[str, object] = Field(default_factory=dict)


class BetaEvaluationExport(BaseModel):
    """Redacted feedback export for small-scope beta review."""

    user_id: str | None = None
    summary: BetaFeedbackSummary
    items: list[BetaEvaluationItem] = Field(default_factory=list)


class BetaFeedbackStore:
    """JSONL-backed beta feedback store."""

    def __init__(self, path: Path | str = ".data/beta_feedback.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, feedback: BetaFeedbackCreate) -> BetaFeedbackRecord:
        record = BetaFeedbackRecord(**feedback.model_dump())
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return record

    def read_all(self) -> list[BetaFeedbackRecord]:
        if not self.path.exists():
            return []
        records: list[BetaFeedbackRecord] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    records.append(BetaFeedbackRecord.model_validate_json(line))
        return records

    def list_by_user(self, user_id: str) -> list[BetaFeedbackRecord]:
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


class InMemoryBetaFeedbackStore:
    """In-memory feedback store for tests."""

    def __init__(self) -> None:
        self.records: list[BetaFeedbackRecord] = []

    def append(self, feedback: BetaFeedbackCreate) -> BetaFeedbackRecord:
        record = BetaFeedbackRecord(**feedback.model_dump())
        self.records.append(record)
        return record

    def read_all(self) -> list[BetaFeedbackRecord]:
        return list(self.records)

    def list_by_user(self, user_id: str) -> list[BetaFeedbackRecord]:
        return [record for record in self.records if record.user_id == user_id]

    def delete_by_user(self, user_id: str) -> int:
        before = len(self.records)
        self.records = [record for record in self.records if record.user_id != user_id]
        return before - len(self.records)


def summarize_feedback(records: list[BetaFeedbackRecord], *, user_id: str | None = None) -> BetaFeedbackSummary:
    summary = BetaFeedbackSummary(user_id=user_id, total=len(records))
    for record in records:
        if record.rating == "up":
            summary.up += 1
        else:
            summary.down += 1
        summary.by_category[record.category] = summary.by_category.get(record.category, 0) + 1
    return summary
