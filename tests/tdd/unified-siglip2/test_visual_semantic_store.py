from __future__ import annotations

import errno
from pathlib import Path

import pytest

from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.embedding.observability import InMemoryEmbeddingObserver
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)


class _MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _record(
    tmp_path: Path,
    *,
    sequence: int,
    scene: str | None = "厨房",
    objects: list[str] | None = None,
    vector: list[float] | None = None,
    video_id: str = "video-1",
) -> VisualSemanticRecord:
    evidence = tmp_path / f"evidence-{video_id}-{sequence}.jpg"
    evidence.write_bytes(f"jpeg-{sequence}".encode())
    search_embedding = vector or [1.0, 0.0]
    return VisualSemanticRecord(
        record_id=f"record-{video_id}-{sequence}",
        session_id="session-1",
        video_id=video_id,
        frame_sequence=sequence,
        captured_at_ms=sequence * 1000,
        summary=f"第 {sequence} 帧",
        scene=scene,
        objects=objects or [],
        search_embedding=search_embedding,
        embedding_space_id="siglip2:text:test",
        index_status="ready",
        evidence_ref=str(evidence),
        evidence_bytes=evidence.stat().st_size,
        created_at_ms=sequence * 1000,
    )


def _query(vector: list[float] | None = None) -> EmbeddingEvent:
    embedding = vector or [1.0, 0.0]
    return EmbeddingEvent(
        event_id="query-event",
        modality="text",
        vector=embedding,
        embedding_space_id="siglip2:text:test",
        model_id="siglip2-test",
        model_revision="revision-test",
        dimension=len(embedding),
        normalized=True,
        session_id="session-1",
        source_observation_id="query-observation",
        text_source="visual_memory_search",
        latency_ms=1,
    )


def test_one_record_serves_latest_and_history(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")
    stored = store.record_success(
        _record(tmp_path, sequence=7, objects=["钥匙"])
    )

    assert store.latest("video-1") == stored
    assert store.at_or_before("video-1", sequence=7).objects == ["钥匙"]
    candidates = store.search(
        _query(),
        video_id="video-1",
        as_of_sequence=7,
    )
    assert [candidate.record.frame_sequence for candidate in candidates] == [7]
    assert candidates[0].score == pytest.approx(1.0)


def test_as_of_excludes_future_record(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")
    store.record_success(_record(tmp_path, sequence=7))
    store.record_success(_record(tmp_path, sequence=9))

    assert [
        item.record.frame_sequence
        for item in store.search(_query(), as_of_sequence=7)
    ] == [7]
    assert store.at_or_before("video-1", sequence=8).frame_sequence == 7


def test_eviction_deletes_owned_evidence(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(
        root=tmp_path / "visual",
        max_records=1,
    )
    first = store.record_success(_record(tmp_path, sequence=1))
    store.record_success(_record(tmp_path, sequence=2))

    assert Path(first.evidence_ref).exists() is False
    assert store.latest("video-1").frame_sequence == 2


def test_store_emits_content_safe_retention_and_eviction_events(
    tmp_path: Path,
) -> None:
    observer = InMemoryEmbeddingObserver()
    store = SessionVisualSemanticStore(
        root=tmp_path / "visual",
        max_records=1,
        observer=observer,
    )

    store.record_success(_record(tmp_path, sequence=1))
    store.record_success(_record(tmp_path, sequence=2))

    assert [event.event_name for event in observer.events] == [
        "visual_semantic.retained",
        "visual_semantic.retained",
        "visual_semantic.evicted",
    ]


def test_retention_failure_does_not_change_latest(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")
    first = store.record_success(_record(tmp_path, sequence=1))
    missing = _record(tmp_path, sequence=2).model_copy(
        update={"evidence_ref": str(tmp_path / "missing.jpg")}
    )

    with pytest.raises(OSError):
        store.record_success(missing)

    assert store.latest("video-1") == first


def test_retention_falls_back_to_copy_across_filesystems(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")

    def cross_device_link(_source, _destination) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(
        "assistant_agent.media.video.semantic_store.os.link",
        cross_device_link,
    )

    stored = store.record_success(_record(tmp_path, sequence=3))

    assert Path(stored.evidence_ref).read_bytes() == b"jpeg-3"


def test_failure_keeps_prior_success_and_updates_snapshot(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")
    first = store.record_success(_record(tmp_path, sequence=1))

    store.record_failure(
        "video-1",
        sequence=2,
        error={"code": "vision_failed", "message": "safe"},
    )

    assert store.latest("video-1") == first
    snapshot = store.snapshot("video-1")
    assert snapshot.last_observation_status == "failed"
    assert snapshot.last_error == {"code": "vision_failed", "message": "safe"}


def test_pool_isolates_same_session_id_between_users(tmp_path: Path) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")

    assert pool.resolve("user-a", "session-1") is not pool.resolve(
        "user-b",
        "session-1",
    )


def test_pool_ttl_eviction_closes_store_and_deletes_evidence(tmp_path: Path) -> None:
    clock = _MutableClock(0.0)
    pool = SessionVisualSemanticStorePool(
        root=tmp_path / "pool",
        ttl_seconds=30.0,
        clock=clock,
    )
    store = pool.resolve("user-1", "session-1")
    stored = store.record_success(_record(tmp_path, sequence=1))
    evidence = Path(stored.evidence_ref)

    clock.value = 31.0

    assert pool.peek("user-1", "session-1") is None
    assert evidence.exists() is False


def test_pool_does_not_evict_an_active_store_lease(tmp_path: Path) -> None:
    clock = _MutableClock(0.0)
    pool = SessionVisualSemanticStorePool(
        root=tmp_path / "pool",
        ttl_seconds=30.0,
        clock=clock,
    )
    lease = pool.acquire("user-1", "session-1")
    stored = lease.store.record_success(_record(tmp_path, sequence=1))
    evidence = Path(stored.evidence_ref)

    clock.value = 31.0
    pool.resolve("user-2", "session-2")

    assert lease.store.closed is False
    assert evidence.exists() is True

    lease.release()
    clock.value = 62.0

    assert pool.peek("user-1", "session-1") is None
    assert evidence.exists() is False
