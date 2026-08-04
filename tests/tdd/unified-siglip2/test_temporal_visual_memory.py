from pathlib import Path

from assistant_agent.media.embedding.consumers.temporal_memory import TemporalVisualMemory
from assistant_agent.media.embedding.models import EmbeddingEvent, ImageObservation


def _event(observation_id: str, sequence: int) -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id=f"event-{observation_id}",
        modality="image",
        vector=[1.0, 0.0],
        embedding_space_id="space",
        model_id="model",
        model_revision="revision",
        dimension=2,
        normalized=True,
        session_id="session-1",
        source_observation_id=observation_id,
        video_id="video-1",
        frame_sequence=sequence,
        captured_at_ms=sequence * 100,
        latency_ms=1,
    )


def _observation(source: Path, observation_id: str, sequence: int) -> ImageObservation:
    return ImageObservation(
        session_id="session-1",
        observation_id=observation_id,
        image_ref=str(source),
        video_id="video-1",
        frame_sequence=sequence,
        captured_at_ms=sequence * 100,
    )


def test_temporal_memory_keeps_probed_frame_not_selected_as_keyframe(tmp_path) -> None:
    source = tmp_path / "live.jpg"
    source.write_bytes(b"jpeg-probe")
    memory = TemporalVisualMemory(root=tmp_path / "evidence", max_records=4, max_bytes=4096)

    memory.accept(_event("probe-1", 1), _observation(source, "probe-1", 1))

    assert [item.frame_sequence for item in memory.records()] == [1]
    assert Path(memory.records()[0].evidence_ref).read_bytes() == b"jpeg-probe"


def test_clear_removes_index_and_owned_jpeg(tmp_path) -> None:
    source = tmp_path / "live.jpg"
    source.write_bytes(b"jpeg-probe")
    memory = TemporalVisualMemory(root=tmp_path / "evidence", max_records=4, max_bytes=4096)
    memory.accept(_event("probe-1", 1), _observation(source, "probe-1", 1))
    owned = Path(memory.records()[0].evidence_ref)

    memory.clear()

    assert memory.records() == []
    assert not owned.exists()
    assert source.exists()


def test_record_and_byte_limits_evict_oldest_owned_evidence(tmp_path) -> None:
    memory = TemporalVisualMemory(root=tmp_path / "evidence", max_records=2, max_bytes=12)
    owned: list[Path] = []
    for sequence in range(1, 4):
        source = tmp_path / f"live-{sequence}.jpg"
        source.write_bytes(b"123456")
        memory.accept(
            _event(f"probe-{sequence}", sequence),
            _observation(source, f"probe-{sequence}", sequence),
        )
        owned.append(Path(memory.records()[-1].evidence_ref))

    assert [item.frame_sequence for item in memory.records()] == [2, 3]
    assert not owned[0].exists()
    assert memory.total_bytes == 12


def test_search_candidates_orders_compatible_vectors_and_filters_time(tmp_path) -> None:
    memory = TemporalVisualMemory(root=tmp_path / "evidence", max_records=4, max_bytes=4096)
    for sequence, vector in [(1, [1.0, 0.0]), (2, [0.0, 1.0])]:
        source = tmp_path / f"live-{sequence}.jpg"
        source.write_bytes(b"jpeg")
        event = _event(f"probe-{sequence}", sequence).model_copy(update={"vector": vector})
        memory.accept(event, _observation(source, f"probe-{sequence}", sequence))
    query = _event("query", 9).model_copy(update={"modality": "text", "vector": [1.0, 0.0]})

    candidates = memory.search_candidates(query, top_k=2, since_ms=50, until_ms=250)

    assert [item.record.frame_sequence for item in candidates] == [1, 2]
    assert candidates[0].similarity == 1.0


def test_hard_link_failure_is_recorded_without_indexing(tmp_path, monkeypatch) -> None:
    source = tmp_path / "live.jpg"
    source.write_bytes(b"jpeg")
    memory = TemporalVisualMemory(root=tmp_path / "evidence")
    monkeypatch.setattr("assistant_agent.media.embedding.consumers.temporal_memory.os.link", lambda *_: (_ for _ in ()).throw(OSError("cross-device")))

    memory.accept(_event("probe-1", 1), _observation(source, "probe-1", 1))

    assert memory.records() == []
    assert memory.retention_failures == 1
