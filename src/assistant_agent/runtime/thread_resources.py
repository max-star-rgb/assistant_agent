"""Native Store references to generated files; no thread directories or copies."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from langgraph.store.base import BaseStore


class GeneratedArtifactError(RuntimeError):
    pass


def artifact_session_scope(config: Mapping) -> str | None:
    """Gateway correlation only; artifact ownership still comes from authenticated identity."""
    metadata = config.get("metadata")
    value = metadata.get("qwen_artifact_session") if isinstance(metadata, Mapping) else None
    return value if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value) else None


class GeneratedArtifactStore:
    def __init__(self, store: BaseStore | None) -> None:
        self.store = store

    @staticmethod
    def thread_ref(identity: str, thread_id: str) -> str:
        if not identity.strip() or not thread_id.strip():
            raise GeneratedArtifactError("artifact_identity_invalid")
        return hashlib.sha256(f"{identity}\0{thread_id}".encode()).hexdigest()[:32]

    def register(self, identity: str, thread_id: str, path: Path, *, filename: str | None = None,
                 session_scope: str | None = None) -> None:
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
            {"owner": identity, "thread_id": thread_id, "path": str(path), "session_scope": session_scope},
            index=False,
        )
        if session_scope:
            self.store.put(
                ("assistant_session_images", identity, session_scope), "latest",
                {"output_ref": f"/artifacts/{self.thread_ref(identity, thread_id)}/generated/{filename}"},
                index=False,
            )

    def latest_image(self, identity: str, session_scope: str) -> str | None:
        if self.store is None:
            raise GeneratedArtifactError("artifact_store_unavailable")
        item = self.store.get(("assistant_session_images", identity, session_scope), "latest")
        return item.value["output_ref"] if item else None

    def image_source_thread(self, identity: str, thread_id: str, output_ref: str,
                            *, session_scope: str | None = None) -> str:
        from assistant_agent.media.generated_artifacts import _thread_generated_artifact_ref

        parsed = _thread_generated_artifact_ref(output_ref)
        if parsed is None or self.store is None:
            raise GeneratedArtifactError("artifact_not_found")
        thread_ref, filename = parsed
        self.resolve(thread_ref, filename, identity=identity)
        item = self.store.get(("assistant_generated_artifacts", thread_ref), filename)
        if item is None or (item.value["thread_id"] != thread_id and
                            (not session_scope or item.value.get("session_scope") != session_scope)):
            raise GeneratedArtifactError("artifact_not_found")
        return item.value["thread_id"]

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
