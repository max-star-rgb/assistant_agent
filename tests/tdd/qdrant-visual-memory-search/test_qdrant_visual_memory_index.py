from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assistant_agent.media.video.qdrant_visual_memory_index import (
    FastEmbedDenseTextEncoder,
    QdrantTransportError,
    QdrantVisualMemoryTextIndex,
)
from assistant_agent.media.video.visual_memory_index import (
    VisualMemoryIndexDocument,
    VisualMemoryIndexQuery,
)


@dataclass
class RecordingTransport:
    responses: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    request_timeouts: list[float | None] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.requests.append((method, path, body))
        self.request_timeouts.append(timeout_seconds)
        if self.responses:
            return self.responses.pop(0)
        return {"status": "ok", "result": {}}

    def close(self) -> None:
        return None


class FixedDenseEncoder:
    dimension = 512

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        assert texts
        return [[0.1] * self.dimension for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        assert text == "鼠标"
        return [0.4] * self.dimension


class PrefixAwareFastEmbedModel:
    def passage_embed(self, texts):
        assert list(texts) == ["一只鼠标"]
        return [[0.1] * 512]

    def query_embed(self, text):
        assert text == "鼠标"
        return [[0.2] * 512]

    def embed(self, texts):
        raise AssertionError(f"generic embed loses retrieval role: {texts}")


class CollectionCreateRaceTransport(RecordingTransport):
    def request(self, method, path, body=None, *, timeout_seconds=None):
        self.requests.append((method, path, body))
        self.request_timeouts.append(timeout_seconds)
        if len(self.requests) == 1:
            raise QdrantTransportError("not found", status_code=404)
        if len(self.requests) == 2:
            raise QdrantTransportError("already exists", status_code=409)
        return {"status": "ok", "result": {}}


class FreshnessPollingTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.point_reads = 0

    def request(self, method, path, body=None, *, timeout_seconds=None):
        self.requests.append((method, path, body))
        self.request_timeouts.append(timeout_seconds)
        if path.endswith("/points?wait=false"):
            return {
                "status": "ok",
                "result": {"operation_id": 42, "status": "acknowledged"},
            }
        if method == "POST" and path.endswith("/points"):
            self.point_reads += 1
            return {"status": "ok", "result": [] if self.point_reads == 1 else [{}]}
        if path.endswith("/points/query"):
            return {
                "status": "ok",
                "result": {
                    "points": [
                        {
                            "id": "visual-81",
                            "score": 0.75,
                            "payload": _document().model_dump(),
                        }
                    ]
                },
            }
        return {"status": "ok", "result": {}}


def _document(sequence: int = 81) -> VisualMemoryIndexDocument:
    return VisualMemoryIndexDocument(
        record_id=f"visual-{sequence}",
        user_id="user-a",
        session_id="session-a",
        video_id="video-a",
        frame_sequence=sequence,
        captured_at_ms=1_754_469_465_000 + sequence,
        text="画面中可见一只罗技鼠标，位于键盘右侧。",
    )


def test_fastembed_uses_passage_and_query_specific_encoders() -> None:
    encoder = object.__new__(FastEmbedDenseTextEncoder)
    encoder._model = PrefixAwareFastEmbedModel()

    assert encoder.embed_documents(["一只鼠标"]) == [[0.1] * 512]
    assert encoder.embed_query("鼠标") == [0.2] * 512


def test_parallel_collection_creation_race_keeps_index_available() -> None:
    transport = CollectionCreateRaceTransport()
    index = QdrantVisualMemoryTextIndex(
        transport=transport,
        dense_encoder=FixedDenseEncoder(),
        collection_name="visual-memory",
    )

    outcome = index.upsert(_document())

    assert outcome.status == "ready"
    assert [(method, path) for method, path, _body in transport.requests[:3]] == [
        ("GET", "/collections/visual-memory"),
        ("PUT", "/collections/visual-memory"),
        ("GET", "/collections/visual-memory"),
    ]


def test_collection_admin_timeout_does_not_relax_frame_publish_timeout() -> None:
    transport = CollectionCreateRaceTransport()
    index = QdrantVisualMemoryTextIndex(
        transport=transport,
        dense_encoder=FixedDenseEncoder(),
        collection_name="visual-memory",
    )

    outcome = index.upsert(_document())

    assert outcome.status == "ready"
    assert transport.request_timeouts == [None, 60.0, None, None]


def test_upsert_uses_server_multilingual_bm25_and_local_dense_vector() -> None:
    transport = RecordingTransport()
    index = QdrantVisualMemoryTextIndex(
        transport=transport,
        dense_encoder=FixedDenseEncoder(),
        collection_name="visual-memory",
        ensure_collection=False,
    )

    outcome = index.upsert(_document())

    assert outcome.status == "ready"
    method, path, body = transport.requests[-1]
    assert (method, path) == (
        "PUT",
        "/collections/visual-memory/points?wait=false",
    )
    assert body is not None
    point = body["points"][0]
    assert point["vector"]["dense"] == [0.1] * 512
    assert point["vector"]["bm25"] == {
        "text": "画面中可见一只罗技鼠标，位于键盘右侧。",
        "model": "qdrant/bm25",
        "options": {"language": "none", "tokenizer": "multilingual"},
    }
    assert point["payload"]["user_id"] == "user-a"
    assert point["payload"]["session_id"] == "session-a"
    assert point["payload"]["frame_sequence"] == 81


def test_search_waits_for_latest_local_record_to_become_query_visible() -> None:
    transport = FreshnessPollingTransport()
    sleeps: list[float] = []
    index = QdrantVisualMemoryTextIndex(
        transport=transport,
        dense_encoder=FixedDenseEncoder(),
        collection_name="visual-memory",
        ensure_collection=False,
        sleep_fn=sleeps.append,
    )

    assert index.upsert(_document()).status == "ready"
    result = index.search(
        VisualMemoryIndexQuery(
            user_id="user-a",
            session_id="session-a",
            query="鼠标",
            freshness_record_id="visual-81",
        )
    )

    assert result.status == "records"
    assert result.coverage_complete is True
    assert sleeps == [0.025]
    assert [path for _method, path, _body in transport.requests] == [
        "/collections/visual-memory/points?wait=false",
        "/collections/visual-memory/points",
        "/collections/visual-memory/points",
        "/collections/visual-memory/points/query",
    ]


def test_search_uses_bm25_first_weighted_rrf_and_strict_filters() -> None:
    transport = RecordingTransport(
        responses=[
            {
                "status": "ok",
                "result": {
                    "points": [
                        {
                            "id": "visual-81",
                            "score": 0.75,
                            "payload": _document().model_dump(),
                        }
                    ]
                },
            }
        ]
    )
    index = QdrantVisualMemoryTextIndex(
        transport=transport,
        dense_encoder=FixedDenseEncoder(),
        collection_name="visual-memory",
        ensure_collection=False,
    )

    result = index.search(
        VisualMemoryIndexQuery(
            user_id="user-a",
            session_id="session-a",
            query="鼠标",
            as_of_sequence=85,
            since_ms=1_754_469_400_000,
            until_ms=1_754_469_500_000,
            limit=12,
        )
    )

    assert result.status == "records"
    assert result.hits[0].document.frame_sequence == 81
    method, path, body = transport.requests[-1]
    assert (method, path) == ("POST", "/collections/visual-memory/points/query")
    assert body is not None
    assert body["query"] == {"rrf": {"weights": [3.0, 1.0]}}
    assert body["limit"] == 12
    assert [item["using"] for item in body["prefetch"]] == ["bm25", "dense"]
    assert [item["limit"] for item in body["prefetch"]] == [32, 32]
    assert body["prefetch"][0]["query"] == {
        "text": "鼠标",
        "model": "qdrant/bm25",
        "options": {"language": "none", "tokenizer": "multilingual"},
    }
    assert body["prefetch"][1]["query"] == [0.4] * 512
    expected_filter = {
        "must": [
            {"key": "user_id", "match": {"value": "user-a"}},
            {"key": "session_id", "match": {"value": "session-a"}},
            {"key": "frame_sequence", "range": {"lte": 85}},
            {
                "key": "captured_at_ms",
                "range": {
                    "gte": 1_754_469_400_000,
                    "lte": 1_754_469_500_000,
                },
            },
        ]
    }
    assert body["prefetch"][0]["filter"] == expected_filter
    assert body["prefetch"][1]["filter"] == expected_filter
