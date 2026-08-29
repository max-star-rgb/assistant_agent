"""Thread-scoped scratch, upload, and artifact directories."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ThreadResourceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ThreadResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)

    @field_validator("root")
    @classmethod
    def _absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("thread resource root must be absolute")
        return value.resolve()


class ThreadResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    root: Path
    scratch_root: Path
    upload_root: Path
    artifact_root: Path


class ThreadResourceManager:
    def __init__(self, config: ThreadResourceConfig) -> None:
        self.config = config

    def resolve(self, identity: str, thread_id: str) -> ThreadResources:
        if not identity.strip() or not thread_id.strip():
            raise ThreadResourceError("thread_resource_identity_invalid")
        self.config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        thread_ref = hashlib.sha256(
            f"{identity}\0{thread_id}".encode("utf-8")
        ).hexdigest()[:32]
        root = self.config.root / thread_ref
        scratch_root = root / "scratch"
        upload_root = root / "uploads"
        artifact_root = root / "artifacts"
        for directory in (scratch_root, upload_root, artifact_root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.utime(root)
        return ThreadResources(
            thread_ref=thread_ref,
            root=root,
            scratch_root=scratch_root,
            upload_root=upload_root,
            artifact_root=artifact_root,
        )

    def resolve_artifact_root(self, thread_ref: str) -> Path:
        if not self._valid_ref(thread_ref):
            raise ThreadResourceError("thread_artifact_not_found")
        root = self.config.root / thread_ref
        try:
            artifact_root = (root / "artifacts").resolve(strict=True)
        except OSError as exc:
            raise ThreadResourceError("thread_artifact_not_found") from exc
        if (
            artifact_root != (root / "artifacts").absolute()
            or not artifact_root.is_dir()
        ):
            raise ThreadResourceError("thread_artifact_not_found")
        os.utime(root)
        return artifact_root

    def expired_thread_refs(self) -> tuple[str, ...]:
        if not self.config.root.is_dir():
            return ()
        cutoff = time.time() - self.config.ttl_seconds
        return tuple(
            sorted(
                directory.name
                for directory in self.config.root.iterdir()
                if directory.is_dir()
                and self._valid_ref(directory.name)
                and directory.stat().st_mtime <= cutoff
            )
        )

    def remove_expired(self, thread_ref: str) -> bool:
        if not self._valid_ref(thread_ref):
            return False
        directory = self.config.root / thread_ref
        try:
            expired = (
                directory.is_dir()
                and directory.stat().st_mtime <= time.time() - self.config.ttl_seconds
            )
        except OSError:
            return False
        if not expired:
            return False
        shutil.rmtree(directory)
        return True

    @staticmethod
    def _valid_ref(value: str) -> bool:
        return len(value) == 32 and all(
            character in "0123456789abcdef" for character in value
        )


__all__ = [
    "ThreadResourceConfig",
    "ThreadResourceError",
    "ThreadResourceManager",
    "ThreadResources",
]
