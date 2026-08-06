"""Qdrant-backed hybrid retrieval for timestamped single-frame VLM text."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from time import sleep
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.visual_memory_index import (
    VisualMemoryIndexDocument,
    VisualMemoryIndexError,
    VisualMemoryIndexHit,
    VisualMemoryIndexQuery,
    VisualMemoryIndexSearchResult,
    VisualMemoryIndexWriteResult,
    VisualMemoryTextIndex,
    UnavailableVisualMemoryTextIndex,
)


DENSE_VECTOR_NAME = "dense"
BM25_VECTOR_NAME = "bm25"
DENSE_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DENSE_VECTOR_SIZE = 512
BM25_MODEL_NAME = "qdrant/bm25"
BM25_OPTIONS: dict[str, str] = {
    "language": "none",
    "tokenizer": "multilingual",
}
BM25_PREFETCH_LIMIT = 32
DENSE_PREFETCH_LIMIT = 32
BM25_RRF_WEIGHT = 3.0
DENSE_RRF_WEIGHT = 1.0
COLLECTION_ADMIN_TIMEOUT_SECONDS = 60.0
VISIBILITY_POLL_INTERVAL_SECONDS = 0.025
VISIBILITY_POLL_ATTEMPTS = 11


class DenseTextEncoder(Protocol):
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class QdrantTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class QdrantTransportError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QdrantHttpTransport:
    """Small REST transport; ranking and fusion stay entirely inside Qdrant."""

    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        if not base_url.strip():
            raise ValueError("Qdrant base URL must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("Qdrant timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        encoded = None
        headers = {"Accept": "application/json"}
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=(
                    self.timeout_seconds if timeout_seconds is None else timeout_seconds
                ),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise QdrantTransportError(
                f"Qdrant returned HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise QdrantTransportError("Qdrant request failed") from exc
        if not isinstance(payload, dict):
            raise QdrantTransportError("Qdrant returned an invalid response")
        return payload

    def close(self) -> None:
        return None


class FastEmbedDenseTextEncoder:
    """One process-local BGE encoder that is forbidden from downloading models."""

    dimension = DENSE_VECTOR_SIZE

    def __init__(self, *, cache_dir: str) -> None:
        if not cache_dir.strip():
            raise ValueError("FastEmbed cache directory must be non-empty")
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError("fastembed_dependency_unavailable") from exc
        self._model = TextEmbedding(
            model_name=DENSE_MODEL_NAME,
            cache_dir=cache_dir,
            local_files_only=True,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        passage_embed = getattr(self._model, "passage_embed", None)
        values = (
            passage_embed(texts)
            if callable(passage_embed)
            else self._model.embed(texts)
        )
        return [_float_vector(item) for item in values]

    def embed_query(self, text: str) -> list[float]:
        query_embed = getattr(self._model, "query_embed", None)
        values = (
            query_embed(text) if callable(query_embed) else self._model.embed([text])
        )
        vectors = [_float_vector(item) for item in values]
        if len(vectors) != 1:
            raise RuntimeError("fastembed_query_vector_invalid")
        return vectors[0]


class QdrantVisualMemoryTextIndex:
    """Use Qdrant native BM25+dense Weighted RRF within trusted filters."""

    def __init__(
        self,
        *,
        transport: QdrantTransport,
        dense_encoder: DenseTextEncoder,
        collection_name: str,
        ensure_collection: bool = True,
        sleep_fn: Callable[[float], None] = sleep,
    ) -> None:
        if not collection_name.strip():
            raise ValueError("Qdrant collection name must be non-empty")
        if dense_encoder.dimension != DENSE_VECTOR_SIZE:
            raise ValueError("visual memory dense vector dimension mismatch")
        self.transport = transport
        self.dense_encoder = dense_encoder
        self.collection_name = collection_name
        self._sleep = sleep_fn
        self._collection_path = f"/collections/{quote(collection_name, safe='')}"
        self._startup_error: VisualMemoryIndexError | None = None
        if ensure_collection:
            try:
                self._ensure_collection()
            except Exception:
                self._startup_error = _unavailable_error()

    def upsert(
        self,
        document: VisualMemoryIndexDocument,
    ) -> VisualMemoryIndexWriteResult:
        if self._startup_error is not None:
            return VisualMemoryIndexWriteResult(
                status="unavailable",
                errors=[self._startup_error],
            )
        try:
            dense = self.dense_encoder.embed_documents([document.text])
            if len(dense) != 1:
                raise RuntimeError("visual_memory_dense_vector_invalid")
            self.transport.request(
                "PUT",
                f"{self._collection_path}/points?wait=false",
                {
                    "points": [
                        {
                            "id": _point_id(
                                user_id=document.user_id,
                                session_id=document.session_id,
                                record_id=document.record_id,
                            ),
                            "vector": {
                                DENSE_VECTOR_NAME: dense[0],
                                BM25_VECTOR_NAME: _bm25_document(document.text),
                            },
                            "payload": document.model_dump(mode="json"),
                        }
                    ]
                },
            )
        except Exception:
            return VisualMemoryIndexWriteResult(
                status="unavailable",
                errors=[_unavailable_error()],
            )
        return VisualMemoryIndexWriteResult(status="ready")

    def search(
        self,
        query: VisualMemoryIndexQuery,
    ) -> VisualMemoryIndexSearchResult:
        if self._startup_error is not None:
            return _unavailable_search(self._startup_error)
        coverage_complete = self._wait_for_freshness_record(query)
        try:
            dense = self.dense_encoder.embed_query(query.query)
            query_filter = _query_filter(query)
            response = self.transport.request(
                "POST",
                f"{self._collection_path}/points/query",
                {
                    "prefetch": [
                        {
                            "query": _bm25_document(query.query),
                            "using": BM25_VECTOR_NAME,
                            "filter": query_filter,
                            "limit": BM25_PREFETCH_LIMIT,
                        },
                        {
                            "query": dense,
                            "using": DENSE_VECTOR_NAME,
                            "filter": query_filter,
                            "limit": DENSE_PREFETCH_LIMIT,
                        },
                    ],
                    "query": {"rrf": {"weights": [BM25_RRF_WEIGHT, DENSE_RRF_WEIGHT]}},
                    "limit": query.limit,
                    "with_payload": True,
                    "with_vector": False,
                },
            )
            raw_result = response.get("result")
            raw_points = (
                raw_result.get("points", []) if isinstance(raw_result, dict) else []
            )
            hits = [_parse_hit(point) for point in raw_points]
        except Exception:
            return _unavailable_search(_unavailable_error())
        return VisualMemoryIndexSearchResult(
            status="records" if hits else "empty",
            hits=hits,
            coverage_complete=coverage_complete,
        )

    def delete_session(self, user_id: str, session_id: str) -> None:
        self._delete_by_filter(
            {
                "must": [
                    _match("user_id", user_id),
                    _match("session_id", session_id),
                ]
            }
        )

    def delete_user(self, user_id: str) -> None:
        self._delete_by_filter({"must": [_match("user_id", user_id)]})

    def close(self) -> None:
        self.transport.close()

    def _wait_for_freshness_record(self, query: VisualMemoryIndexQuery) -> bool:
        if query.freshness_record_id is None:
            return True
        point_id = _point_id(
            user_id=query.user_id,
            session_id=query.session_id,
            record_id=query.freshness_record_id,
        )
        for attempt in range(VISIBILITY_POLL_ATTEMPTS):
            try:
                response = self.transport.request(
                    "POST",
                    f"{self._collection_path}/points",
                    {
                        "ids": [point_id],
                        "with_payload": False,
                        "with_vector": False,
                    },
                )
            except Exception:
                return False
            result = response.get("result")
            if isinstance(result, list) and result:
                return True
            if attempt + 1 < VISIBILITY_POLL_ATTEMPTS:
                self._sleep(VISIBILITY_POLL_INTERVAL_SECONDS)
        return False

    def _ensure_collection(self) -> None:
        try:
            self.transport.request("GET", self._collection_path)
            return
        except QdrantTransportError as exc:
            if exc.status_code != 404:
                raise
        try:
            self.transport.request(
                "PUT",
                self._collection_path,
                {
                    "vectors": {
                        DENSE_VECTOR_NAME: {
                            "size": DENSE_VECTOR_SIZE,
                            "distance": "Cosine",
                        }
                    },
                    "sparse_vectors": {
                        BM25_VECTOR_NAME: {"modifier": "idf"},
                    },
                },
                timeout_seconds=COLLECTION_ADMIN_TIMEOUT_SECONDS,
            )
        except QdrantTransportError as exc:
            if exc.status_code != 409:
                raise
            self.transport.request("GET", self._collection_path)

    def _delete_by_filter(self, query_filter: dict[str, Any]) -> None:
        if self._startup_error is not None:
            return
        try:
            self.transport.request(
                "POST",
                f"{self._collection_path}/points/delete?wait=true",
                {"filter": query_filter},
            )
        except Exception:
            return


def create_visual_memory_text_index(config: ProviderConfig) -> VisualMemoryTextIndex:
    """Create the runtime backend without ever downloading a model at startup."""

    try:
        encoder = FastEmbedDenseTextEncoder(
            cache_dir=config.visual_memory_dense_model_cache_dir,
        )
        transport = QdrantHttpTransport(
            base_url=config.visual_memory_qdrant_url,
            timeout_seconds=config.visual_memory_qdrant_timeout_seconds,
        )
        return QdrantVisualMemoryTextIndex(
            transport=transport,
            dense_encoder=encoder,
            collection_name=config.visual_memory_qdrant_collection,
        )
    except Exception:
        return UnavailableVisualMemoryTextIndex(
            code="visual_memory_qdrant_unavailable",
            message="visual memory retrieval service is unavailable",
        )


def _point_id(*, user_id: str, session_id: str, record_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"assistant-agent:visual-memory:{user_id}:{session_id}:{record_id}",
        )
    )


def _bm25_document(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "model": BM25_MODEL_NAME,
        "options": dict(BM25_OPTIONS),
    }


def _match(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "match": {"value": value}}


def _query_filter(query: VisualMemoryIndexQuery) -> dict[str, Any]:
    must: list[dict[str, Any]] = [
        _match("user_id", query.user_id),
        _match("session_id", query.session_id),
    ]
    if query.as_of_sequence is not None:
        must.append({"key": "frame_sequence", "range": {"lte": query.as_of_sequence}})
    timestamp_range: dict[str, int] = {}
    if query.since_ms is not None:
        timestamp_range["gte"] = query.since_ms
    if query.until_ms is not None:
        timestamp_range["lte"] = query.until_ms
    if timestamp_range:
        must.append({"key": "captured_at_ms", "range": timestamp_range})
    return {"must": must}


def _parse_hit(value: object) -> VisualMemoryIndexHit:
    if not isinstance(value, dict):
        raise ValueError("Qdrant point must be an object")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Qdrant point payload is missing")
    score = value.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("Qdrant point score is invalid")
    return VisualMemoryIndexHit(
        document=VisualMemoryIndexDocument.model_validate(payload),
        score=float(score),
    )


def _float_vector(value: object) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("dense embedding must be a sequence")
    vector = [float(item) for item in value]
    if not vector:
        raise ValueError("dense embedding must be non-empty")
    return vector


def _unavailable_error() -> VisualMemoryIndexError:
    return VisualMemoryIndexError(
        code="visual_memory_qdrant_unavailable",
        message="visual memory retrieval service is unavailable",
        recoverable=True,
    )


def _unavailable_search(
    error: VisualMemoryIndexError,
) -> VisualMemoryIndexSearchResult:
    return VisualMemoryIndexSearchResult(
        status="unavailable",
        coverage_complete=False,
        errors=[error],
    )
