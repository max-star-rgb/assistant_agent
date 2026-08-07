from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

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
    clock = _MutableClock(now)
    store = ManagedMemoryMediaStore(max_total_bytes=1024, clock=clock)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="audio",
        mime_type="audio/wav",
        payload=b"wave-sentinel",
        expires_at=now + timedelta(seconds=1),
    )

    clock.instant = now + timedelta(seconds=1)
    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.open_stream(ref, owner_scope="owner-sentinel", max_bytes=1024)

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


class _MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


class _LyingBytes(bytes):
    def __len__(self) -> int:
        return 1

    def __bytes__(self) -> bytes:
        return b"x"


def test_managed_media_expiry_uses_host_clock_without_reader_time_override() -> None:
    """A Plugin cannot rewind the Host clock to revive expired media."""
    opened_at = datetime(2026, 8, 7, tzinfo=timezone.utc)
    clock = _MutableClock(opened_at)
    store = ManagedMemoryMediaStore(max_total_bytes=1024, clock=clock)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
        expires_at=opened_at + timedelta(seconds=1),
    )
    clock.instant = opened_at + timedelta(seconds=1)

    with pytest.raises(MemoryMediaAccessError) as read_error:
        store.read(ref, owner_scope="owner-sentinel", max_bytes=1024)
    with pytest.raises(MemoryMediaAccessError) as stream_error:
        store.open_stream(ref, owner_scope="owner-sentinel", max_bytes=1024)
    with pytest.raises(TypeError):
        store.read(
            ref,
            owner_scope="owner-sentinel",
            max_bytes=1024,
            now=opened_at,
        )
    with pytest.raises(TypeError):
        store.open_stream(
            ref,
            owner_scope="owner-sentinel",
            max_bytes=1024,
            now=opened_at,
        )

    assert read_error.value.code == "memory_media_expired"
    assert stream_error.value.code == "memory_media_expired"


def test_managed_store_implements_artifact_writer_keyword_and_host_bytes_paths() -> None:
    """The same store accepts the writer Protocol's keyword and Host raw bytes."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    artifact = MemoryArtifactPayload(
        owner_scope="owner-sentinel",
        media_type="document",
        mime_type="text/plain",
        payload=b"writer-sentinel",
    )

    try:
        keyword_ref = store.register(payload=artifact)
    except TypeError as error:
        pytest.fail(f"writer Protocol keyword form failed: {error}")
    positional_ref = store.register(artifact)
    host_ref = store.register(
        owner_scope="owner-sentinel",
        media_type="document",
        mime_type="text/plain",
        payload=b"host-sentinel",
    )

    assert store.read(keyword_ref, owner_scope="owner-sentinel", max_bytes=1024) == b"writer-sentinel"
    assert store.read(positional_ref, owner_scope="owner-sentinel", max_bytes=1024) == b"writer-sentinel"
    assert store.read(host_ref, owner_scope="owner-sentinel", max_bytes=1024) == b"host-sentinel"


def test_media_payload_and_store_normalize_lying_bytes_before_budget_checks() -> None:
    """A bytes subclass cannot underreport its body size to retain readable media."""
    payload = _LyingBytes(b"123456")
    artifact = MemoryArtifactPayload(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=payload,
    )
    store = ManagedMemoryMediaStore(max_total_bytes=1024, max_item_bytes=5)

    assert type(artifact.payload) is bytes
    assert len(artifact.payload) == 6
    with pytest.raises(MemoryMediaRegistrationError) as exc_info:
        store.register(
            owner_scope="owner-sentinel",
            media_type="image",
            mime_type="image/jpeg",
            payload=payload,
        )

    assert exc_info.value.code == "memory_media_size_limit"


def test_tampered_returned_ref_cannot_widen_read_authority_or_shrink_bytes() -> None:
    """Mutating a returned ref must not mutate the store's canonical evidence."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    owner_ref = store.register(
        owner_scope="owner-a",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    object.__setattr__(owner_ref, "owner_scope", "owner-b")

    with pytest.raises(MemoryMediaAccessError) as owner_error:
        store.read(owner_ref, owner_scope="owner-b", max_bytes=1024)

    altered_ref = store.register(
        owner_scope="owner-a",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    altered_ref.__dict__.update(
        media_type="audio",
        mime_type="audio/mpeg",
        size_bytes=1,
    )

    with pytest.raises(MemoryMediaAccessError) as altered_error:
        store.read(
            altered_ref,
            owner_scope="owner-a",
            max_bytes=1,
            allowed_modalities={"audio"},
            allowed_mime_types={"audio/mpeg"},
        )

    assert owner_error.value.code == "memory_media_owner_mismatch"
    assert altered_error.value.code == "memory_media_ref_mismatch"


def test_resolve_returns_independent_canonical_copies_and_uses_payload_size_budget() -> None:
    """Returned resolution refs cannot poison future resolutions or their budgets."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    registered = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        image_ids=[registered.ref_id],
    )

    first_refs, first_issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"image"},
        max_items=1,
        max_total_bytes=1024,
    )
    first_refs[0].__dict__.update(
        owner_scope="forged-owner",
        mime_type="image/png",
        size_bytes=1,
    )
    second_refs, second_issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"image"},
        max_items=1,
        max_total_bytes=1024,
    )
    budget_refs, budget_issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"image"},
        max_items=1,
        max_total_bytes=1,
    )

    assert first_issues == []
    assert second_issues == []
    assert first_refs[0] is not second_refs[0]
    assert second_refs[0].owner_scope == "owner-sentinel"
    assert second_refs[0].mime_type == "image/jpeg"
    assert second_refs[0].size_bytes == 6
    assert budget_refs == []
    assert [issue.code for issue in budget_issues] == ["memory_media_total_limit"]


class _TrackingStr(str):
    def __new__(cls, value: str) -> _TrackingStr:
        instance = super().__new__(cls, value)
        instance.calls: list[str] = []
        return instance

    def __hash__(self) -> int:
        self.calls.append("hash")
        return str.__hash__(self)

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        return str.__eq__(self, other)

    def __str__(self) -> str:
        self.calls.append("str")
        return str.__str__(self)


class _TrackingDatetime(datetime):
    def __new__(cls, value: datetime) -> _TrackingDatetime:
        instance = datetime.__new__(
            cls,
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            value.tzinfo,
        )
        instance.calls: list[str] = []
        return instance

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        return datetime.__eq__(self, other)

    def __ge__(self, other: object) -> bool:
        self.calls.append("ge")
        return datetime.__ge__(self, other)


class _TrackingInt(int):
    def __new__(cls, value: int) -> _TrackingInt:
        instance = super().__new__(cls, value)
        instance.calls: list[str] = []
        return instance

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        return int.__eq__(self, other)


def test_read_and_stream_reject_injected_scalar_subclasses_before_hooks_run() -> None:
    """Injected scalar subclasses cannot run hash/equality/time hooks at access."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    canonical = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    injected_values = [
        ("ref_id", _TrackingStr(canonical.ref_id)),
        ("owner_scope", _TrackingStr(canonical.owner_scope)),
        ("mime_type", _TrackingStr(canonical.mime_type)),
        ("media_type", _TrackingStr(canonical.media_type)),
        ("created_at", _TrackingDatetime(canonical.created_at)),
        ("size_bytes", _TrackingInt(canonical.size_bytes)),
    ]

    for field, injected in injected_values:
        ref = canonical.model_copy(deep=True)
        ref.__dict__[field] = injected
        with pytest.raises(MemoryMediaAccessError) as read_error:
            store.read(ref, owner_scope="owner-sentinel", max_bytes=1024)
        with pytest.raises(MemoryMediaAccessError) as stream_error:
            store.open_stream(ref, owner_scope="owner-sentinel", max_bytes=1024)

        assert read_error.value.code == "memory_media_ref_mismatch"
        assert stream_error.value.code == "memory_media_ref_mismatch"
        assert injected.calls == []


def test_read_rejects_bool_size_before_using_it_as_an_integer() -> None:
    """A bool injected as size is invalid even though bool is an int subclass."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    ref.__dict__["size_bytes"] = True

    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.read(ref, owner_scope="owner-sentinel", max_bytes=1024)

    assert exc_info.value.code == "memory_media_ref_mismatch"


def test_resolve_rejects_subclassed_request_ref_id_before_dict_lookup() -> None:
    """resolve never hashes or compares a caller-controlled ID subclass."""
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    canonical = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    injected_id = _TrackingStr(canonical.ref_id)
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        image_ids=[canonical.ref_id],
    )
    request.image_ids[0] = injected_id

    refs, issues = store.resolve_request_refs(
        request,
        owner_scope="owner-sentinel",
        allowed_modalities={"image"},
        max_items=1,
        max_total_bytes=1024,
    )

    assert refs == []
    assert [issue.code for issue in issues] == ["memory_media_ref_invalid"]
    assert injected_id.calls == []


def test_registration_failure_does_not_consume_store_budget() -> None:
    """A failed public-ref validation leaves no hidden entry or retained bytes."""
    store = ManagedMemoryMediaStore(max_total_bytes=6)

    with pytest.raises(ValidationError):
        store.register(
            owner_scope="x" * 513,
            media_type="image",
            mime_type="image/jpeg",
            payload=b"123456",
        )

    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )

    assert store.read(ref, owner_scope="owner-sentinel", max_bytes=6) == b"123456"
