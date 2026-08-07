from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.memory.plugins.contracts import MemoryArtifactPayload
from assistant_agent.memory.plugins.media import (
    ManagedMemoryMediaStore,
    MemoryMediaAccessError,
    MemoryMediaRegistrationError,
)
from assistant_agent.runtime.requests import UserRequest


def test_managed_media_read_requires_matching_owner() -> None:
    """A different owner must not read bytes registered for this session."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        owner_scope="user-a:agent-a:session-a",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )

    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.read(ref, owner_scope="user-b:agent-a:session-a", max_bytes=1024)

    assert exc_info.value.code == "memory_media_owner_mismatch"


def test_managed_media_read_enforces_call_limit() -> None:
    """A caller cannot bypass its per-read byte budget."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )

    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.read(ref, owner_scope="owner-sentinel", max_bytes=5)

    assert exc_info.value.code == "memory_media_size_limit"


def test_managed_media_read_rejects_a_ref_with_changed_mime_type() -> None:
    """A Plugin cannot relabel an opaque reference to widen MIME access."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )
    relabelled_ref = ref.model_copy(update={"mime_type": "image/png"})

    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.read(relabelled_ref, owner_scope="owner-sentinel", max_bytes=1024)

    assert exc_info.value.code == "memory_media_ref_mismatch"


def test_resolve_request_refs_returns_only_registered_owner_bound_media() -> None:
    """A request ID is not itself authority to resolve a media reference."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    registered = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        image_ids=[registered.ref_id, "forged-ref-sentinel"],
    )

    refs, issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"image"},
        max_items=2,
        max_total_bytes=1024,
    )

    assert refs == [registered]
    assert [issue.code for issue in issues] == ["memory_media_ref_unknown"]


def test_resolve_request_refs_enforces_modality_and_total_budget() -> None:
    """A valid ref is still excluded when this Plugin lacks its modality or budget."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    first = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    second = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"abcdef",
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        image_ids=[first.ref_id, second.ref_id],
    )

    refs, issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"image"},
        max_items=2,
        max_total_bytes=10,
    )

    assert refs == [first]
    assert [issue.code for issue in issues] == ["memory_media_total_limit"]

    blocked_refs, blocked_issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"audio"},
        max_items=2,
        max_total_bytes=1024,
    )

    assert blocked_refs == []
    assert [issue.code for issue in blocked_issues] == [
        "memory_media_modality_not_allowed",
        "memory_media_modality_not_allowed",
    ]


def test_managed_media_stream_rechecks_expiry() -> None:
    """An expired reference cannot be read through the streaming escape hatch."""
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    store = ManagedMemoryMediaStore(max_total_bytes=1024, clock=lambda: now)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="audio",
        mime_type="audio/wav",
        payload=b"wave-sentinel",
        expires_at=now + timedelta(seconds=1),
    )

    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.open_stream(
            ref,
            owner_scope="owner-sentinel",
            max_bytes=1024,
            now=now + timedelta(seconds=1),
        )

    assert exc_info.value.code == "memory_media_expired"


@pytest.mark.parametrize(
    "payload",
    ["https://example.invalid/media.jpg", "/tmp/media.jpg", "aGVsbG8="],
)
def test_managed_media_store_rejects_non_bytes_payloads(payload: str) -> None:
    """Registration must never turn a URL, path, or Base64-looking string into media."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)

    with pytest.raises(TypeError):
        store.register(
            owner_scope="owner-sentinel",
            media_type="image",
            mime_type="image/jpeg",
            payload=payload,
        )


def test_artifact_writer_registers_bytes_in_the_same_managed_store() -> None:
    """Plugin-produced evidence receives the same opaque owner-bound treatment."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        MemoryArtifactPayload(
            owner_scope="owner-sentinel",
            media_type="document",
            mime_type="text/plain",
            payload=b"artifact-sentinel",
        )
    )

    assert ref.ref_id != "artifact-sentinel"
    assert store.read(ref, owner_scope="owner-sentinel", max_bytes=1024) == b"artifact-sentinel"


def test_managed_media_store_enforces_total_registration_budget() -> None:
    """Host registration cannot exceed the store's aggregate in-memory limit."""
    store = ManagedMemoryMediaStore(max_total_bytes=5, max_item_bytes=6)

    with pytest.raises(MemoryMediaRegistrationError) as exc_info:
        store.register(
            owner_scope="owner-sentinel",
            media_type="image",
            mime_type="image/jpeg",
            payload=b"123456",
        )

    assert exc_info.value.code == "memory_media_total_limit"
