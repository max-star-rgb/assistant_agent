"""Minimal, daily-partitioned completeness ledger for Runtime trace export."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Sequence
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant_agent.observability.trace_store import TraceEvent, TraceEventType


LEDGER_TIMEZONE = ZoneInfo("Asia/Shanghai")
LEDGER_SCHEMA_VERSION = "assistant_agent_trace_ledger_event_v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SAFE_CANONICAL_EVENT = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9]+(?:[_.][a-z0-9]+)+$")
_NODE_NAMES = frozenset(
    {
        "agent_graph",
        "agent_service",
        "assistant",
        "assistant_loop",
        "compose_response",
        "durable_task_quantum",
        "execute_tool",
        "hotel_price_watch_probe",
        "mcp_tool_run",
        "post_response_memory_ingestion",
        "proactive_probe",
        "realtime_backend",
        "realtime_video_observer",
        "runtime",
        "session_start",
        "skill_runner",
        "tool_executor",
        "vision_understanding",
        "visual_reminder_runtime",
    }
)
_CANONICAL_EVENT_NAMESPACES = frozenset(
    {
        "action",
        "agent_service",
        "assistant",
        "context",
        "conversation",
        "embedding",
        "llm",
        "loop_guard",
        "memory",
        "realtime",
        "response",
        "run",
        "runtime",
        "semantic_frame",
        "tool",
        "trace",
        "vision",
        "visual_context",
        "visual_memory",
        "visual_reminder",
        "visual_semantic",
        "vlm",
        "workflow",
    }
)
_STATUSES = frozenset(
    {
        "accepted",
        "acked",
        "allowed",
        "blocked",
        "budget_excluded",
        "cancelled",
        "candidates_only",
        "captured",
        "cleared",
        "closed",
        "comparable",
        "completed",
        "degraded",
        "disabled",
        "disconnected_before_ack",
        "disconnected_before_send",
        "duplicate_suppressed",
        "empty",
        "error",
        "failed",
        "failed_below_hard",
        "generating",
        "hard_limit",
        "idempotency_key_required",
        "insufficient_evidence",
        "interrupted",
        "invalid_id",
        "loaded",
        "matched",
        "not_needed",
        "not_ready",
        "not_run",
        "ok",
        "partial",
        "passed",
        "pending",
        "pending_cancel",
        "processing",
        "proposed",
        "queued",
        "ready",
        "ready_with_warnings",
        "received",
        "records",
        "rejected",
        "repair",
        "replanning",
        "reserved",
        "retryable_failed",
        "reused",
        "revising",
        "revision_conflict",
        "routed",
        "running",
        "saturated",
        "scheduled",
        "selected",
        "sent",
        "skipped",
        "stale",
        "started",
        "stop_requested",
        "succeeded",
        "success",
        "timeout",
        "text",
        "tool_call",
        "transitioned",
        "triggered",
        "unavailable",
        "unset",
        "unverified",
        "unknown",
        "unknown_after_timeout",
        "validation_failed",
        "waiting_external_event",
        "waiting_input",
        "waiting_schedule",
        "warning",
        "working",
    }
)


class TraceLedgerEvent(BaseModel):
    """Prompt-safe evidence that one canonical event was emitted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["assistant_agent_trace_ledger_event_v1"] = (
        LEDGER_SCHEMA_VERSION
    )
    trace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    node_name: str = Field(min_length=1)
    event_type: TraceEventType
    canonical_event: str | None = None
    status: str | None = None
    error_code: str | None = None
    created_at: datetime
    event_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("trace_id", "run_id")
    @classmethod
    def validate_id_identifier(cls, value: str, info) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be a safe identifier")
        return value

    @field_validator("node_name")
    @classmethod
    def validate_node_identifier(cls, value: str) -> str:
        if value not in _NODE_NAMES:
            raise ValueError("node_name must be a registered machine identifier")
        return value

    @field_validator("canonical_event")
    @classmethod
    def validate_event_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        namespace = value.partition(".")[0]
        if (
            len(value) > 128
            or _SAFE_CANONICAL_EVENT.fullmatch(value) is None
            or namespace not in _CANONICAL_EVENT_NAMESPACES
        ):
            raise ValueError("canonical_event must be a registered machine identifier")
        return value

    @field_validator("status")
    @classmethod
    def validate_status_identifier(cls, value: str | None) -> str | None:
        if value is not None and value not in _STATUSES:
            raise ValueError("status must be a registered machine identifier")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_identifier(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) > 128 or _SAFE_ERROR_CODE.fullmatch(value) is None
        ):
            raise ValueError("error_code must be a structured machine identifier")
        return value

    @classmethod
    def from_trace_event(cls, event: TraceEvent) -> "TraceLedgerEvent":
        values = {
            "trace_id": event.trace_id,
            "run_id": event.run_id,
            "node_name": event.node_name,
            "event_type": event.event_type,
            "canonical_event": event.canonical_event,
            "status": event.status,
            "error_code": event.error_code,
            "created_at": event.created_at,
        }
        return cls(event_digest=_digest(values), **values)

    @model_validator(mode="after")
    def validate_digest(self) -> "TraceLedgerEvent":
        if self.event_digest != _digest(self._digest_values()):
            raise ValueError("trace ledger event digest mismatch")
        return self

    def to_trace_event(self) -> TraceEvent:
        """Restore only fields guaranteed by the completeness ledger."""

        return TraceEvent(
            trace_id=self.trace_id,
            run_id=self.run_id,
            node_name=self.node_name,
            event_type=self.event_type,
            canonical_event=self.canonical_event,
            status=self.status,
            error_code=self.error_code,
            created_at=self.created_at,
        )

    def _digest_values(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "node_name": self.node_name,
            "event_type": self.event_type,
            "canonical_event": self.canonical_event,
            "status": self.status,
            "error_code": self.error_code,
            "created_at": self.created_at,
        }


class DailyTraceLedgerStore:
    """Persist minimal trace facts in one JSONL file per Shanghai date."""

    def __init__(self, path: Path | str = ".data/trace_ledger") -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def append(self, event: TraceEvent) -> None:
        record = TraceLedgerEvent.from_trace_event(event)
        partition = self.path / _partition_name(event.created_at)
        with _ledger_lock(self.path, exclusive=True):
            with partition.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")

    def list_by_run(self, run_id: str) -> list[TraceEvent]:
        return [event for event in self._events() if event.run_id == run_id]

    def list_by_trace(self, trace_id: str) -> list[TraceEvent]:
        return [event for event in self._events() if event.trace_id == trace_id]

    def node_path(self, run_id: str) -> list[str]:
        return [
            event.node_name
            for event in self.list_by_run(run_id)
            if event.event_type == "node_finished"
        ]

    def list_by_user(self, user_id: str) -> list[TraceEvent]:
        # The ledger deliberately does not persist user identity.
        return []

    def delete_by_user(self, user_id: str) -> int:
        # No user identity or content is present in this store.
        return 0

    def _events(self) -> list[TraceEvent]:
        try:
            events, _, _, _ = iter_ledger_events(self.path)
        except OSError:
            return []
        return events


class LedgerPartitionSnapshot(BaseModel):
    """Fingerprint and parse status for one ledger partition read by an audit."""

    partition_date: date
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    valid_record_count: int = Field(ge=0)
    invalid_record_count: int = Field(ge=0)


def prune_trace_ledger(
    root: Path | str,
    *,
    retention_days: int,
    reference_date: date,
    is_day_completed: Callable[[date], bool],
    approved_snapshots: Sequence[LedgerPartitionSnapshot],
) -> list[Path]:
    """Delete expired partitions only after their audit day is confirmed."""

    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    ledger_root = Path(root)
    if not ledger_root.is_dir():
        return []
    cutoff = reference_date - timedelta(days=retention_days)
    removed: list[Path] = []
    approved = {snapshot.partition_date: snapshot for snapshot in approved_snapshots}
    with _ledger_lock(ledger_root, exclusive=True):
        for partition in sorted(ledger_root.glob("*.jsonl")):
            try:
                partition_date = date.fromisoformat(partition.stem)
            except ValueError:
                continue
            snapshot = approved.get(partition_date)
            if snapshot is None or snapshot.invalid_record_count != 0:
                continue
            content = partition.read_bytes()
            _, current_valid, current_invalid = _parse_partition(content)
            if (
                partition_date >= cutoff
                or not is_day_completed(partition_date)
                or current_invalid != 0
                or current_valid != snapshot.valid_record_count
                or _fingerprint(content) != snapshot.fingerprint
            ):
                continue
            partition.unlink()
            removed.append(partition)
    return removed


def iter_ledger_events(
    root: Path | str,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[list[TraceEvent], int, int, list[LedgerPartitionSnapshot]]:
    """Read coherent partitions for an optional half-open audit window."""

    ledger_root = Path(root)
    events: list[TraceEvent] = []
    valid = 0
    invalid = 0
    snapshots: list[LedgerPartitionSnapshot] = []
    selected_dates = _partition_dates(window_start=window_start, window_end=window_end)
    with _ledger_lock(ledger_root, exclusive=False):
        for partition in sorted(ledger_root.glob("*.jsonl")):
            try:
                partition_date = date.fromisoformat(partition.stem)
            except ValueError:
                continue
            if selected_dates is not None and partition_date not in selected_dates:
                continue
            content = partition.read_bytes()
            partition_events, partition_valid, partition_invalid = _parse_partition(
                content
            )
            events.extend(partition_events)
            valid += partition_valid
            invalid += partition_invalid
            snapshots.append(
                LedgerPartitionSnapshot(
                    partition_date=partition_date,
                    fingerprint=_fingerprint(content),
                    valid_record_count=partition_valid,
                    invalid_record_count=partition_invalid,
                )
            )
    return events, valid, invalid, snapshots


def _partition_name(created_at: datetime) -> str:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("trace event created_at must be timezone-aware")
    return f"{created_at.astimezone(LEDGER_TIMEZONE).date().isoformat()}.jsonl"


def _digest(values: dict[str, object]) -> str:
    normalized = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in values.items()
    }
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fingerprint(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _parse_partition(content: bytes) -> tuple[list[TraceEvent], int, int]:
    events: list[TraceEvent] = []
    valid = 0
    invalid = 0
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return [], 0, 1
    for line in lines:
        if not line.strip():
            continue
        try:
            record = TraceLedgerEvent.model_validate_json(line)
        except Exception:
            invalid += 1
            continue
        valid += 1
        events.append(record.to_trace_event())
    return events, valid, invalid


def _partition_dates(
    *,
    window_start: datetime | None,
    window_end: datetime | None,
) -> set[date] | None:
    if window_start is None and window_end is None:
        return None
    if window_start is None or window_end is None:
        raise ValueError("window_start and window_end must be provided together")
    if (
        window_start.tzinfo is None
        or window_start.utcoffset() is None
        or window_end.tzinfo is None
        or window_end.utcoffset() is None
    ):
        raise ValueError("ledger audit window must be timezone-aware")
    if window_end <= window_start:
        raise ValueError("ledger audit window must be increasing")
    first = window_start.astimezone(LEDGER_TIMEZONE).date()
    last = (window_end - timedelta(microseconds=1)).astimezone(LEDGER_TIMEZONE).date()
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


@contextmanager
def _ledger_lock(root: Path, *, exclusive: bool):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
