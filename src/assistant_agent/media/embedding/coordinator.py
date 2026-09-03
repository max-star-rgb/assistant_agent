"""Session-scoped embedding inference and deduplication."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock
from time import perf_counter

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    EmbeddingOutcome,
    EmbeddingPriority,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.provider import MultimodalEmbeddingProvider
from assistant_agent.media.embedding.observability import (
    EmbeddingObserver,
    emit_embedding_observation,
)


Observation = ImageObservation | TextObservation


class SessionEmbeddingCoordinator:
    """Own one session's shared inference and success cache."""

    def __init__(
        self,
        session_id: str,
        provider: MultimodalEmbeddingProvider,
        *,
        success_cache_size: int = 128,
        observer: EmbeddingObserver | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session id must be non-empty")
        if success_cache_size <= 0:
            raise ValueError("success cache size must be positive")
        self.session_id = session_id
        self.provider = provider
        self.success_cache_size = success_cache_size
        self.observer = observer
        self._cache: OrderedDict[tuple[str, str], EmbeddingEvent] = OrderedDict()
        self._inflight: dict[tuple[str, str], Future[EmbeddingOutcome]] = {}
        self._lock = Lock()
        self._closed = False

    def embed_image(
        self,
        observation: ImageObservation,
        *,
        priority: EmbeddingPriority = "background",
    ) -> EmbeddingOutcome:
        return self._compute(observation, priority=priority)

    def embed_text(
        self,
        observation: TextObservation,
        *,
        priority: EmbeddingPriority = "interactive",
    ) -> EmbeddingOutcome:
        return self._compute(observation, priority=priority)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
        emit_embedding_observation(self.observer, "embedding.session_cleanup")

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _compute(
        self, observation: Observation, *, priority: EmbeddingPriority
    ) -> EmbeddingOutcome:
        if observation.session_id != self.session_id:
            raise ValueError("embedding_observation_session_mismatch")
        modality = "image" if isinstance(observation, ImageObservation) else "text"
        emit_embedding_observation(
            self.observer,
            "embedding.requested",
            observation=observation,
            priority=priority,
        )
        key = (modality, observation.observation_id)
        with self._lock:
            self._ensure_open()
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                emit_embedding_observation(
                    self.observer,
                    "embedding.deduplicated",
                    outcome=cached,
                    observation=observation,
                    priority=priority,
                    cache_hit=True,
                )
                return cached
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
        assert future is not None
        if not owner:
            emit_embedding_observation(
                self.observer,
                "embedding.deduplicated",
                observation=observation,
                priority=priority,
                cache_hit=False,
            )
            return future.result()

        started = perf_counter()
        emit_embedding_observation(
            self.observer,
            "embedding.started",
            observation=observation,
            priority=priority,
        )
        try:
            if modality == "image":
                outcome = self.provider.embed_image(observation)
            else:
                outcome = self.provider.embed_text(observation)
        except Exception:
            outcome = EmbeddingFailureEvent(
                modality=modality,
                session_id=observation.session_id,
                source_observation_id=observation.observation_id,
                code="embedding_provider_failed",
                safe_message="embedding provider execution failed",
                recoverable=True,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
            )

        with self._lock:
            self._inflight.pop(key, None)
            if isinstance(outcome, EmbeddingEvent):
                self._cache[key] = outcome
                self._cache.move_to_end(key)
                while len(self._cache) > self.success_cache_size:
                    self._cache.popitem(last=False)
            future.set_result(outcome)
        emit_embedding_observation(
            self.observer,
            "embedding.failed" if isinstance(outcome, EmbeddingFailureEvent) else "embedding.finished",
            outcome=outcome,
            observation=observation,
            priority=priority,
        )
        return outcome

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("embedding_coordinator_closed")
