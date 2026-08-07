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


_MEDIA_TYPES = frozenset({"image", "audio", "video", "document"})
_MANAGED_MEDIA_REF_FIELDS = frozenset(
    {
        "ref_id",
        "media_type",
        "mime_type",
        "size_bytes",
        "created_at",
        "owner_scope",
    }
)
_MEMORY_ARTIFACT_PAYLOAD_FIELDS = frozenset(
    {"owner_scope", "media_type", "mime_type", "payload", "expires_at"}
)
_USER_REQUEST_FIELDS = frozenset(UserRequest.model_fields)


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
    ref_id: str
    media_type: MemoryModality
    mime_type: str
    owner_scope: str
    created_at: datetime
    payload: bytes
    expires_at: datetime | None


@dataclass(frozen=True)
class _ExternalMediaRef:
    ref_id: str
    media_type: MemoryModality
    mime_type: str
    size_bytes: int
    created_at: datetime
    owner_scope: str


class ManagedMemoryMediaStore:
    """An opaque-ref store that never accepts a path, URL, or encoded string."""

    def __init__(
        self,
        *,
        max_total_bytes: int,
        max_item_bytes: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(max_total_bytes) is not int:
            raise TypeError("max_total_bytes must be an exact integer")
        if max_item_bytes is not None and type(max_item_bytes) is not int:
            raise TypeError("max_item_bytes must be an exact integer")
        if max_total_bytes < 0:
            raise ValueError("max_total_bytes must be non-negative")
        if max_item_bytes is not None and max_item_bytes < 0:
            raise ValueError("max_item_bytes must be non-negative")
        self._max_total_bytes = max_total_bytes
        self._max_item_bytes = (
            max_total_bytes if max_item_bytes is None else max_item_bytes
        )
        self._clock = (
            clock if clock is not None else lambda: datetime.now(timezone.utc)
        )
        self._entries: dict[str, _StoredMedia] = {}
        self._total_bytes = 0
        self._lock = RLock()

    def register(
        self,
        payload: MemoryArtifactPayload | bytes | None = None,
        *,
        owner_scope: str | None = None,
        media_type: MemoryModality | None = None,
        mime_type: str | None = None,
        expires_at: datetime | None = None,
    ) -> ManagedMediaRef:
        """Register Host-held bytes and return an unguessable, scoped reference.

        ``payload=MemoryArtifactPayload(...)`` and its positional equivalent
        implement the artifact-writer Protocol. Host direct registration uses
        the same keyword with raw ``bytes`` plus the scoped metadata. Neither
        path accepts a filesystem path, URL, or Base64 string.
        """

        if isinstance(payload, MemoryArtifactPayload):
            if any(
                value is not None
                for value in (owner_scope, media_type, mime_type, expires_at)
            ):
                raise TypeError("artifact registration cannot mix payload fields")
            artifact_fields = self._artifact_payload_fields(payload)
            if artifact_fields is None:
                raise TypeError("managed artifact payload must have exact fields")
            owner_scope, media_type, mime_type, payload, expires_at = artifact_fields

        if not self._is_registration_metadata(
            owner_scope,
            media_type,
            mime_type,
            payload,
        ):
            raise TypeError("managed media registration requires typed bytes metadata")

        normalized_payload = memoryview(payload).tobytes()
        created_at = self._ensure_aware(self._clock())
        if expires_at is not None:
            expires_at = self._ensure_aware(expires_at)
        size_bytes = len(normalized_payload)
        if size_bytes > self._max_item_bytes:
            raise MemoryMediaRegistrationError("memory_media_size_limit")

        entry = _StoredMedia(
            ref_id=uuid4().hex,
            media_type=media_type,
            mime_type=mime_type,
            created_at=created_at,
            owner_scope=owner_scope,
            payload=normalized_payload,
            expires_at=expires_at,
        )
        public_ref = self._ref_for(entry)
        with self._lock:
            if self._total_bytes + size_bytes > self._max_total_bytes:
                raise MemoryMediaRegistrationError("memory_media_total_limit")
            self._entries[entry.ref_id] = entry
            self._total_bytes += size_bytes
        return public_ref

    def read(
        self,
        ref: ManagedMediaRef,
        *,
        owner_scope: str,
        max_bytes: int,
        allowed_modalities: set[MemoryModality] | None = None,
        allowed_mime_types: set[str] | None = None,
    ) -> bytes:
        """Read media only after revalidating the reference on this call."""

        entry = self._validated_entry(
            ref,
            owner_scope=owner_scope,
            max_bytes=max_bytes,
            allowed_modalities=allowed_modalities,
            allowed_mime_types=allowed_mime_types,
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
    ) -> BinaryIO:
        """Open an independent in-memory stream after the same read checks."""

        return BytesIO(
            self.read(
                ref,
                owner_scope=owner_scope,
                max_bytes=max_bytes,
                allowed_modalities=allowed_modalities,
                allowed_mime_types=allowed_mime_types,
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

        if (
            type(owner_scope) is not str
            or type(max_items) is not int
            or type(max_total_bytes) is not int
            or max_items < 0
            or max_total_bytes < 0
        ):
            raise ValueError("media request limits must be non-negative")
        try:
            safe_allowed_modalities = self._copy_exact_string_set(
                allowed_modalities,
                allow_none=False,
                error_code="memory_media_allowed_modalities_invalid",
                valid_values=_MEDIA_TYPES,
            )
        except MemoryMediaAccessError as error:
            return [], [self._issue(error.code)]
        candidates = self._request_candidates(request)
        if candidates is None:
            return [], [self._issue("memory_media_request_invalid")]
        now = self._ensure_aware(self._clock())
        resolved: list[ManagedMediaRef] = []
        issues: list[MemoryPluginIssue] = []
        total_bytes = 0
        seen_ids: set[str] = set()

        for ref_id, requested_modality in candidates:
            if type(ref_id) is not str:
                issues.append(self._issue("memory_media_ref_invalid"))
                continue
            if ref_id in seen_ids:
                issues.append(self._issue("memory_media_ref_duplicate"))
                continue
            seen_ids.add(ref_id)
            with self._lock:
                entry = self._entries.get(ref_id)
            if entry is None:
                issues.append(self._issue("memory_media_ref_unknown"))
                continue
            if entry.owner_scope != owner_scope:
                issues.append(self._issue("memory_media_owner_mismatch"))
                continue
            if entry.media_type != requested_modality:
                issues.append(self._issue("memory_media_modality_mismatch"))
                continue
            if self._is_expired(entry, now):
                issues.append(self._issue("memory_media_expired"))
                continue
            if entry.media_type not in safe_allowed_modalities:
                issues.append(self._issue("memory_media_modality_not_allowed"))
                continue
            if len(resolved) >= max_items:
                issues.append(self._issue("memory_media_item_limit"))
                continue
            size_bytes = len(entry.payload)
            if total_bytes + size_bytes > max_total_bytes:
                issues.append(self._issue("memory_media_total_limit"))
                continue
            resolved.append(self._ref_for(entry))
            total_bytes += size_bytes
        return resolved, issues

    def _validated_entry(
        self,
        ref: ManagedMediaRef,
        *,
        owner_scope: str,
        max_bytes: int,
        allowed_modalities: set[MemoryModality] | None,
        allowed_mime_types: set[str] | None,
    ) -> _StoredMedia:
        external_ref = self._external_ref(ref)
        if external_ref is None:
            raise MemoryMediaAccessError("memory_media_ref_mismatch")
        if type(owner_scope) is not str:
            raise MemoryMediaAccessError("memory_media_owner_mismatch")
        if type(max_bytes) is not int or max_bytes < 0:
            raise MemoryMediaAccessError("memory_media_size_limit")
        safe_allowed_modalities = self._copy_exact_string_set(
            allowed_modalities,
            allow_none=True,
            error_code="memory_media_allowed_modalities_invalid",
            valid_values=_MEDIA_TYPES,
        )
        safe_allowed_mime_types = self._copy_exact_string_set(
            allowed_mime_types,
            allow_none=True,
            error_code="memory_media_allowed_mime_types_invalid",
        )
        with self._lock:
            entry = self._entries.get(external_ref.ref_id)
        if entry is None:
            raise MemoryMediaAccessError("memory_media_ref_unknown")
        if entry.owner_scope != owner_scope:
            raise MemoryMediaAccessError("memory_media_owner_mismatch")
        if not self._matches_entry(external_ref, entry):
            raise MemoryMediaAccessError("memory_media_ref_mismatch")
        if self._is_expired(entry, self._ensure_aware(self._clock())):
            raise MemoryMediaAccessError("memory_media_expired")
        if (
            safe_allowed_modalities is not None
            and entry.media_type not in safe_allowed_modalities
        ):
            raise MemoryMediaAccessError("memory_media_modality_not_allowed")
        if (
            safe_allowed_mime_types is not None
            and entry.mime_type not in safe_allowed_mime_types
        ):
            raise MemoryMediaAccessError("memory_media_mime_not_allowed")
        if len(entry.payload) > max_bytes:
            raise MemoryMediaAccessError("memory_media_size_limit")
        return entry

    @staticmethod
    def _ref_for(entry: _StoredMedia) -> ManagedMediaRef:
        return ManagedMediaRef(
            ref_id=entry.ref_id,
            media_type=entry.media_type,
            mime_type=entry.mime_type,
            size_bytes=len(entry.payload),
            created_at=entry.created_at,
            owner_scope=entry.owner_scope,
        )

    @staticmethod
    def _matches_entry(ref: _ExternalMediaRef, entry: _StoredMedia) -> bool:
        return (
            ref.ref_id == entry.ref_id
            and ref.media_type == entry.media_type
            and ref.mime_type == entry.mime_type
            and ref.size_bytes == len(entry.payload)
            and ref.created_at == entry.created_at
            and ref.owner_scope == entry.owner_scope
        )

    @staticmethod
    def _external_ref(ref: object) -> _ExternalMediaRef | None:
        if type(ref) is not ManagedMediaRef:
            return None
        values = object.__getattribute__(ref, "__dict__")
        if type(values) is not dict:
            return None
        field_names: set[str] = set()
        for field_name in values:
            if type(field_name) is not str:
                return None
            field_names.add(field_name)
        if field_names != _MANAGED_MEDIA_REF_FIELDS:
            return None
        ref_id = dict.__getitem__(values, "ref_id")
        media_type = dict.__getitem__(values, "media_type")
        mime_type = dict.__getitem__(values, "mime_type")
        size_bytes = dict.__getitem__(values, "size_bytes")
        created_at = dict.__getitem__(values, "created_at")
        owner_scope = dict.__getitem__(values, "owner_scope")
        if (
            type(ref_id) is not str
            or type(media_type) is not str
            or media_type not in _MEDIA_TYPES
            or type(mime_type) is not str
            or type(size_bytes) is not int
            or size_bytes < 0
            or not ManagedMemoryMediaStore._is_exact_aware_datetime(created_at)
            or type(owner_scope) is not str
        ):
            return None
        return _ExternalMediaRef(
            ref_id=ref_id,
            media_type=media_type,
            mime_type=mime_type,
            size_bytes=size_bytes,
            created_at=created_at,
            owner_scope=owner_scope,
        )

    @staticmethod
    def _is_registration_metadata(
        owner_scope: object,
        media_type: object,
        mime_type: object,
        payload: object,
    ) -> bool:
        return (
            type(owner_scope) is str
            and bool(owner_scope)
            and type(media_type) is str
            and media_type in _MEDIA_TYPES
            and type(mime_type) is str
            and bool(mime_type)
            and isinstance(payload, bytes)
        )

    @staticmethod
    def _copy_exact_string_set(
        value: object,
        *,
        allow_none: bool,
        error_code: str,
        valid_values: frozenset[str] | None = None,
    ) -> set[str] | None:
        if value is None:
            if allow_none:
                return None
            raise MemoryMediaAccessError(error_code)
        if type(value) is not set:
            raise MemoryMediaAccessError(error_code)
        safe_values: set[str] = set()
        for item in value:
            if type(item) is not str:
                raise MemoryMediaAccessError(error_code)
            if valid_values is not None and item not in valid_values:
                raise MemoryMediaAccessError(error_code)
            safe_values.add(item)
        return safe_values

    @staticmethod
    def _request_candidates(
        request: object,
    ) -> list[tuple[object, MemoryModality]] | None:
        if type(request) is not UserRequest:
            return None
        values = object.__getattribute__(request, "__dict__")
        if type(values) is not dict:
            return None
        field_names: set[str] = set()
        for field_name in values:
            if type(field_name) is not str:
                return None
            field_names.add(field_name)
        if field_names != _USER_REQUEST_FIELDS:
            return None
        image_ids = dict.__getitem__(values, "image_ids")
        video_ids = dict.__getitem__(values, "video_ids")
        audio_id = dict.__getitem__(values, "audio_id")
        if type(image_ids) is not list or type(video_ids) is not list:
            return None
        candidates: list[tuple[object, MemoryModality]] = []
        for ref_id in image_ids:
            candidates.append((ref_id, "image"))
        for ref_id in video_ids:
            candidates.append((ref_id, "video"))
        if audio_id is not None:
            candidates.append((audio_id, "audio"))
        return candidates

    @staticmethod
    def _artifact_payload_fields(
        artifact: object,
    ) -> tuple[object, object, object, object, object] | None:
        if type(artifact) is not MemoryArtifactPayload:
            return None
        values = object.__getattribute__(artifact, "__dict__")
        if type(values) is not dict:
            return None
        field_names: set[str] = set()
        for field_name in values:
            if type(field_name) is not str:
                return None
            field_names.add(field_name)
        if field_names != _MEMORY_ARTIFACT_PAYLOAD_FIELDS:
            return None
        return (
            dict.__getitem__(values, "owner_scope"),
            dict.__getitem__(values, "media_type"),
            dict.__getitem__(values, "mime_type"),
            dict.__getitem__(values, "payload"),
            dict.__getitem__(values, "expires_at"),
        )

    @staticmethod
    def _is_expired(entry: _StoredMedia, now: datetime) -> bool:
        return entry.expires_at is not None and now >= entry.expires_at

    @staticmethod
    def _issue(code: str) -> MemoryPluginIssue:
        return MemoryPluginIssue(code=code, message=code, recoverable=False)

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        if not ManagedMemoryMediaStore._is_exact_aware_datetime(value):
            raise ValueError("managed media timestamps must be timezone-aware")
        return value

    @staticmethod
    def _is_exact_aware_datetime(value: object) -> bool:
        return (
            type(value) is datetime
            and type(value.tzinfo) is timezone
            and value.utcoffset() is not None
        )
