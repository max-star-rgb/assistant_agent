"""Owner-bound immutable artifact workspace for durable workflows."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.models import utc_now


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactNotFound(ArtifactStoreError):
    pass


class ArtifactAccessDenied(ArtifactStoreError):
    pass


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=120)
    digest: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    producer_work_item_id: str = Field(min_length=1)


class LocalWorkflowArtifactStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.content_root = self.root / "content"
        self.content_root.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.root / "artifacts.sqlite3",
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  workflow_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  digest TEXT NOT NULL,
                  byte_size INTEGER NOT NULL,
                  producer_work_item_id TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(user_id, agent_id, workflow_id, kind, digest, producer_work_item_id)
                )
                """
            )

    def write_text(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        kind: str,
        text: str,
        producer_work_item_id: str,
    ) -> ArtifactRef:
        content = text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        stable_key = "\0".join(
            (
                identity.user_id,
                identity.agent_id,
                workflow_id,
                kind,
                digest,
                producer_work_item_id,
            )
        ).encode("utf-8")
        artifact_id = f"artifact_{hashlib.sha256(stable_key).hexdigest()[:32]}"
        relative_path = f"content/{artifact_id}.txt"
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                path = self.root / relative_path
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(content)
                temporary.replace(path)
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO workflow_artifacts (
                          artifact_id, user_id, agent_id, workflow_id, kind, digest,
                          byte_size, producer_work_item_id, relative_path, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            identity.user_id,
                            identity.agent_id,
                            workflow_id,
                            kind,
                            digest,
                            len(content),
                            producer_work_item_id,
                            relative_path,
                            utc_now().isoformat(),
                        ),
                    )
            return ArtifactRef(
                artifact_id=artifact_id,
                uri=f"workflow-artifact://{artifact_id}",
                workflow_id=workflow_id,
                kind=kind,
                digest=digest,
                byte_size=len(content),
                producer_work_item_id=producer_work_item_id,
            )

    def read_text(self, *, identity: RequestIdentity, artifact_ref: str) -> str:
        artifact_id = self._artifact_id(artifact_ref)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(artifact_id)
            if row["user_id"] != identity.user_id or row["agent_id"] != identity.agent_id:
                raise ArtifactAccessDenied(artifact_id)
            return (self.root / row["relative_path"]).read_text(encoding="utf-8")

    def get_ref(self, *, identity: RequestIdentity, artifact_ref: str) -> ArtifactRef:
        artifact_id = self._artifact_id(artifact_ref)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(artifact_id)
            if row["user_id"] != identity.user_id or row["agent_id"] != identity.agent_id:
                raise ArtifactAccessDenied(artifact_id)
            return ArtifactRef(
                artifact_id=artifact_id,
                uri=f"workflow-artifact://{artifact_id}",
                workflow_id=row["workflow_id"],
                kind=row["kind"],
                digest=row["digest"],
                byte_size=int(row["byte_size"]),
                producer_work_item_id=row["producer_work_item_id"],
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _artifact_id(artifact_ref: str) -> str:
        prefix = "workflow-artifact://"
        if not artifact_ref.startswith(prefix) or len(artifact_ref) == len(prefix):
            raise ArtifactNotFound(artifact_ref)
        return artifact_ref.removeprefix(prefix)
