"""Business operation barrier for idempotent Durable Workflow publishing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowPublishConflict(RuntimeError):
    """The same publish identity was observed with incompatible business facts."""


@dataclass(frozen=True)
class PublishEffectRef:
    operation_key: str
    effect_ref: str


class SQLiteWorkflowPublisher:
    """Idempotent publisher probe/adapter keyed by the business operation key."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_publish_effects ("
                "operation_key TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, "
                "effect_ref TEXT NOT NULL)"
            )

    def publish(self, operation: "WorkflowPublishOperation") -> PublishEffectRef:
        effect_ref = "workflow-publish-effect:sha256:" + hashlib.sha256(
            operation.operation_key.encode()
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_digest, effect_ref FROM workflow_publish_effects "
                "WHERE operation_key = ?",
                (operation.operation_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO workflow_publish_effects "
                    "(operation_key, payload_digest, effect_ref) VALUES (?, ?, ?)",
                    (operation.operation_key, operation.payload_digest, effect_ref),
                )
            elif (
                row["payload_digest"] != operation.payload_digest
                or row["effect_ref"] != effect_ref
            ):
                raise WorkflowPublishConflict(
                    "publish effect identity has conflicting payload facts"
                )
            connection.commit()
        return PublishEffectRef(operation.operation_key, effect_ref)

    def effect_count(self, operation_key: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM workflow_publish_effects "
                "WHERE operation_key = ?", (operation_key,)
            ).fetchone()
        return int(row["count"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class WorkflowPublishOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_key: str = Field(min_length=1, max_length=1_024)
    workflow_id: str = Field(min_length=1, max_length=512)
    plan_version: int = Field(ge=1)
    current_generation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    user_id: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=512)
    deliverable_artifact_refs: tuple[str, ...] = Field(max_length=128)
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        plan_version: int,
        current_generation_digest: str,
        user_id: str,
        agent_id: str,
        deliverable_artifact_refs: tuple[str, ...],
        result_digest: str,
    ) -> "WorkflowPublishOperation":
        return cls(
            operation_key=(
                f"workflow:{workflow_id}:publish:{plan_version}:"
                f"{current_generation_digest}"
            ),
            workflow_id=workflow_id,
            plan_version=plan_version,
            current_generation_digest=current_generation_digest,
            user_id=user_id,
            agent_id=agent_id,
            deliverable_artifact_refs=deliverable_artifact_refs,
            result_digest=result_digest,
        )

    @model_validator(mode="after")
    def validate_operation_key(self) -> "WorkflowPublishOperation":
        expected = (
            f"workflow:{self.workflow_id}:publish:{self.plan_version}:"
            f"{self.current_generation_digest}"
        )
        if self.operation_key != expected:
            raise ValueError("publish operation key does not match its facts")
        if len(self.deliverable_artifact_refs) != len(
            set(self.deliverable_artifact_refs)
        ):
            raise ValueError("publish artifact references must be unique")
        return self

    @property
    def payload_digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class PublishCommitRef:
    operation_key: str
    status: Literal["prepared", "committed"]
    result_digest: str
    effect_ref: str | None = None


class SQLiteWorkflowPublishStore:
    """Transactional publish ledger independent from graph checkpoints."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_publish_operations (
                    operation_key TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    operation_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('prepared', 'committed')),
                    effect_ref TEXT
                );
                CREATE TABLE IF NOT EXISTS workflow_publish_events (
                    operation_key TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL CHECK(event_kind = 'completed'),
                    FOREIGN KEY(operation_key)
                        REFERENCES workflow_publish_operations(operation_key)
                );
                """
            )

    def prepare(self, operation: WorkflowPublishOperation) -> PublishCommitRef:
        encoded = operation.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_digest, operation_json, status, effect_ref "
                "FROM workflow_publish_operations WHERE operation_key = ?",
                (operation.operation_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO workflow_publish_operations "
                    "(operation_key, payload_digest, operation_json, status) "
                    "VALUES (?, ?, ?, 'prepared')",
                    (operation.operation_key, operation.payload_digest, encoded),
                )
                connection.commit()
                return PublishCommitRef(
                    operation_key=operation.operation_key,
                    status="prepared",
                    result_digest=operation.result_digest,
                )
            self._validate_existing(row, operation)
            connection.commit()
            return PublishCommitRef(
                operation_key=operation.operation_key,
                status=row["status"],
                result_digest=operation.result_digest,
                effect_ref=row["effect_ref"],
            )

    def commit(
        self,
        operation: WorkflowPublishOperation,
        *,
        effect_ref: PublishEffectRef,
    ) -> PublishCommitRef:
        if effect_ref.operation_key != operation.operation_key:
            raise WorkflowPublishConflict("publish effect belongs to another operation")
        self.prepare(operation)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_digest, operation_json, status, effect_ref "
                "FROM workflow_publish_operations WHERE operation_key = ?",
                (operation.operation_key,),
            ).fetchone()
            self._validate_existing(row, operation)
            if row["effect_ref"] not in {None, effect_ref.effect_ref}:
                raise WorkflowPublishConflict(
                    "publish operation has conflicting effect reference"
                )
            connection.execute(
                "UPDATE workflow_publish_operations "
                "SET status = 'committed', effect_ref = ? "
                "WHERE operation_key = ? AND status = 'prepared'",
                (effect_ref.effect_ref, operation.operation_key),
            )
            connection.execute(
                "INSERT OR IGNORE INTO workflow_publish_events "
                "(operation_key, event_kind) VALUES (?, 'completed')",
                (operation.operation_key,),
            )
            connection.commit()
        return PublishCommitRef(
            operation_key=operation.operation_key,
            status="committed",
            result_digest=operation.result_digest,
            effect_ref=effect_ref.effect_ref,
        )

    def completed_event_count(self, operation_key: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM workflow_publish_events "
                "WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def _validate_existing(row: sqlite3.Row, operation: WorkflowPublishOperation) -> None:
        if (
            row["payload_digest"] != operation.payload_digest
            or row["operation_json"] != operation.model_dump_json()
        ):
            raise WorkflowPublishConflict(
                "publish operation identity has conflicting owner or result facts"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


__all__ = [
    "PublishCommitRef",
    "PublishEffectRef",
    "SQLiteWorkflowPublishStore",
    "SQLiteWorkflowPublisher",
    "WorkflowPublishConflict",
    "WorkflowPublishOperation",
]
