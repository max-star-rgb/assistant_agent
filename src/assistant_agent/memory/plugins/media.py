"""Host-owned, in-memory media capabilities for Memory Plugins.

Only the Host may register bytes here.  Plugins receive opaque references and
must present the owner scope plus per-call limits for every read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from threading import RLock
from typing import BinaryIO
from uuid import uuid4

from assistant_agent.memory.plugins.contracts import (
    ManagedMediaRef,
    MemoryArtifactPayload,
    MemoryModality,
    MemoryPluginIssue,
)
from assistant_agent.runtime.requests import UserRequest


class MemoryMediaAccessError(PermissionError):
    """Stable denial returned when a Plugin cannot access managed media."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MemoryMediaRegistrationError(ValueError):
    """Stable Host registration failure for media resource limits."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _StoredMedia:
    ref: ManagedMediaRef
    payload: bytes
    expires_at: datetime | None


class ManagedMemoryMediaStore:
    """An opaque-ref store that never accepts a path, URL, or encoded string."""

    def __init__(
        self,
        *,
        max_total_bytes: int,
        max_item_bytes: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_total_bytes < 0:
            raise ValueError("max_total_bytes must be non-negative")
        if max_item_bytes is not None and max_item_bytes < 0:
            raise ValueError("max_item_bytes must be non-negative")
        self._max_total_bytes = max_total_bytes
        self._max_item_bytes = (
            max_total_bytes if max_item_bytes is None else max_item_bytes
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, _StoredMedia] = {}
        self._total_bytes = 0
        self._lock = RLock()

    def register(
        self,
        artifact: MemoryArtifactPayload | None = None,
        *,
        owner_scope: str | None = None,
        media_type: MemoryModality | None = None,
        mime_type: str | None = None,
        payload: bytes | None = None,
        expires_at: datetime | None = None,
    ) -> ManagedMediaRef:
        """Register Host-held bytes and return an unguessable, scoped reference.

        The positional form is the artifact-writer protocol.  Keyword fields
        are the Host's direct registration path; neither form accepts a
        filesystem path, URL, or Base64 string because ``payload`` must be
        actual ``bytes``.
        """

        if artifact is not None:
            if any(
                value is not None
                for value in (owner_scope, media_type, mime_type, payload, expires_at)
            ):
                raise TypeError("artifact registration cannot mix payload fields")
            owner_scope = artifact.owner_scope
            media_type = artifact.media_type
            mime_type = artifact.mime_type
            payload = artifact.payload
            expires_at = artifact.expires_at

        if (
            not isinstance(owner_scope, str)
            or not owner_scope
            or media_type not in {"image", "audio", "video", "document"}
            or not isinstance(mime_type, str)
            or not mime_type
            or not isinstance(payload, bytes)
        ):
            raise TypeError("managed media registration requires typed bytes metadata")

        created_at = self._ensure_aware(self._clock())
        if expires_at is not None:
            expires_at = self._ensure_aware(expires_at)
        size_bytes = len(payload)
        if size_bytes > self._max_item_bytes:
            raise MemoryMediaRegistrationError("memory_media_size_limit")

        with self._lock:
            if self._total_bytes + size_bytes > self._max_total_bytes:
                raise MemoryMediaRegistrationError("memory_media_total_limit")
            ref = ManagedMediaRef(
                ref_id=uuid4().hex,
                media_type=media_type,
                mime_type=mime_type,
                size_bytes=size_bytes,
                created_at=created_at,
                owner_scope=owner_scope,
            )
            self._entries[ref.ref_id] = _StoredMedia(
                ref=ref,
                payload=payload,
                expires_at=expires_at,
            )
            self._total_bytes += size_bytes
        return ref

    def read(
        self,
        ref: ManagedMediaRef,
        *,
        owner_scope: str,
        max_bytes: int,
        allowed_modalities: set[MemoryModality] | None = None,
        allowed_mime_types: set[str] | None = None,
        now: datetime | None = None,
    ) -> bytes:
        """Read media only after revalidating the reference on this call."""

        entry = self._validated_entry(
            ref,
            owner_scope=owner_scope,
            max_bytes=max_bytes,
            allowed_modalities=allowed_modalities,
            allowed_mime_types=allowed_mime_types,
            now=now,
        )
        return entry.payload

    def open_stream(
        self,
        ref: ManagedMediaRef,
        *,
        owner_scope: str,
        max_bytes: int,
        allowed_modalities: set[MemoryModality] | None = None,
        allowed_mime_types: set[str] | None = None,
        now: datetime | None = None,
    ) -> BinaryIO:
        """Open an independent in-memory stream after the same read checks."""

        return BytesIO(
            self.read(
                ref,
                owner_scope=owner_scope,
                max_bytes=max_bytes,
                allowed_modalities=allowed_modalities,
                allowed_mime_types=allowed_mime_types,
                now=now,
            )
        )

    def resolve_request_refs(
        self,
        request: UserRequest,
        *,
        owner_scope: str,
        allowed_modalities: set[MemoryModality],
        max_items: int,
        max_total_bytes: int,
    ) -> tuple[list[ManagedMediaRef], list[MemoryPluginIssue]]:
        """Resolve only already-registered request IDs without reading bytes."""

        if max_items < 0 or max_total_bytes < 0:
            raise ValueError("media request limits must be non-negative")
        now = self._ensure_aware(self._clock())
        resolved: list[ManagedMediaRef] = []
        issues: list[MemoryPluginIssue] = []
        total_bytes = 0
        seen_ids: set[str] = set()
        candidates = [
            *( (ref_id, "image") for ref_id in request.image_ids ),
            *( (ref_id, "video") for ref_id in request.video_ids ),
        ]
        if request.audio_id is not None:
            candidates.append((request.audio_id, "audio"))

        for ref_id, requested_modality in candidates:
            if ref_id in seen_ids:
                issues.append(self._issue("memory_media_ref_duplicate"))
                continue
            seen_ids.add(ref_id)
            with self._lock:
                entry = self._entries.get(ref_id)
            if entry is None:
                issues.append(self._issue("memory_media_ref_unknown"))
                continue
            ref = entry.ref
            if ref.owner_scope != owner_scope:
                issues.append(self._issue("memory_media_owner_mismatch"))
                continue
            if ref.media_type != requested_modality:
                issues.append(self._issue("memory_media_modality_mismatch"))
                continue
            if self._is_expired(entry, now):
                issues.append(self._issue("memory_media_expired"))
                continue
            if ref.media_type not in allowed_modalities:
                issues.append(self._issue("memory_media_modality_not_allowed"))
                continue
            if len(resolved) >= max_items:
                issues.append(self._issue("memory_media_item_limit"))
                continue
            if total_bytes + ref.size_bytes > max_total_bytes:
                issues.append(self._issue("memory_media_total_limit"))
                continue
            resolved.append(ref)
            total_bytes += ref.size_bytes
        return resolved, issues

    def _validated_entry(
        self,
        ref: ManagedMediaRef,
        *,
        owner_scope: str,
        max_bytes: int,
        allowed_modalities: set[MemoryModality] | None,
        allowed_mime_types: set[str] | None,
        now: datetime | None,
    ) -> _StoredMedia:
        if max_bytes < 0:
            raise MemoryMediaAccessError("memory_media_size_limit")
        with self._lock:
            entry = self._entries.get(ref.ref_id)
        if entry is None:
            raise MemoryMediaAccessError("memory_media_ref_unknown")
        if entry.ref.owner_scope != owner_scope:
            raise MemoryMediaAccessError("memory_media_owner_mismatch")
        if entry.ref != ref:
            raise MemoryMediaAccessError("memory_media_ref_mismatch")
        if self._is_expired(entry, self._ensure_aware(now or self._clock())):
            raise MemoryMediaAccessError("memory_media_expired")
        if allowed_modalities is not None and ref.media_type not in allowed_modalities:
            raise MemoryMediaAccessError("memory_media_modality_not_allowed")
        if allowed_mime_types is not None and ref.mime_type not in allowed_mime_types:
            raise MemoryMediaAccessError("memory_media_mime_not_allowed")
        if entry.ref.size_bytes > max_bytes:
            raise MemoryMediaAccessError("memory_media_size_limit")
        return entry

    @staticmethod
    def _is_expired(entry: _StoredMedia, now: datetime) -> bool:
        return entry.expires_at is not None and now >= entry.expires_at

    @staticmethod
    def _issue(code: str) -> MemoryPluginIssue:
        return MemoryPluginIssue(code=code, message=code, recoverable=False)

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("managed media timestamps must be timezone-aware")
        return value
