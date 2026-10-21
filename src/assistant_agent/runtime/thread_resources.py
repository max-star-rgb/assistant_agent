"""Native Store references to generated files; no thread directories or copies."""

from __future__ import annotations

import hashlib
from pathlib import Path

from langgraph.store.base import BaseStore


class GeneratedArtifactError(RuntimeError):
    pass


class GeneratedArtifactStore:
    def __init__(self, store: BaseStore | None) -> None:
        self.store = store

    @staticmethod
    def thread_ref(identity: str, thread_id: str) -> str:
        if not identity.strip() or not thread_id.strip():
            raise GeneratedArtifactError("artifact_identity_invalid")
        return hashlib.sha256(f"{identity}\0{thread_id}".encode()).hexdigest()[:32]

    def register(self, identity: str, thread_id: str, path: Path, *, filename: str | None = None) -> None:
        if self.store is None:
            raise GeneratedArtifactError("artifact_store_unavailable")
        path = path.resolve(strict=True)
        if not path.is_file():
            raise GeneratedArtifactError("artifact_not_found")
        filename = filename or path.name
        if Path(filename).name != filename:
            raise GeneratedArtifactError("artifact_name_invalid")
        self.store.put(
            ("assistant_generated_artifacts", self.thread_ref(identity, thread_id)),
            filename,
            {"owner": identity, "thread_id": thread_id, "path": str(path)},
            index=False,
        )

    def resolve(self, thread_ref: str, filename: str, *, identity: str) -> Path:
        if (self.store is None or len(thread_ref) != 32
                or any(c not in "0123456789abcdef" for c in thread_ref)
                or not filename or Path(filename).name != filename):
            raise GeneratedArtifactError("artifact_not_found")
        item = self.store.get(("assistant_generated_artifacts", thread_ref), filename)
        if item is None or item.value.get("owner") != identity:
            raise GeneratedArtifactError("artifact_not_found")
        path = Path(item.value["path"])
        try:
            if path.resolve(strict=True) != path or not path.is_file():
                raise GeneratedArtifactError("artifact_not_found")
        except OSError as exc:
            raise GeneratedArtifactError("artifact_not_found") from exc
        return path
