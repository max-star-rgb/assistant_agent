"""Session-scoped inference deduplication and isolated consumer dispatch."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Lock, Thread
from time import perf_counter
from typing import Literal, Protocol

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


OverflowPolicy = Literal["latest_wins", "drop_oldest", "reject_new"]
Observation = ImageObservation | TextObservation


class EmbeddingConsumer(Protocol):
    consumer_id: str

    def accept(self, outcome: EmbeddingOutcome, observation: Observation) -> None: ...

    def close(self) -> None: ...


_STOP = object()


@dataclass
class _ConsumerWorker:
    consumer: EmbeddingConsumer
    queue_size: int
    overflow_policy: OverflowPolicy

    def __post_init__(self) -> None:
        if self.queue_size <= 0:
            raise ValueError("consumer queue size must be positive")
        self.queue: Queue[object] = Queue(maxsize=self.queue_size)
        self.thread = Thread(
            target=self._run,
            name=f"embedding-consumer-{self.consumer.consumer_id}",
            daemon=True,
        )
        self._closed = False
        self._lock = Lock()
        self.thread.start()

    def enqueue(self, outcome: EmbeddingOutcome, observation: Observation) -> bool:
        with self._lock:
            if self._closed:
                return False
            item = (outcome, observation)
            try:
                self.queue.put_nowait(item)
                return True
            except Full:
                if self.overflow_policy == "reject_new":
                    return False
                if self.overflow_policy == "latest_wins":
                    self._discard_all()
                else:
                    self._discard_one()
                try:
                    self.queue.put_nowait(item)
                    return True
                except Full:
                    return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    self.queue.put_nowait(_STOP)
                    break
                except Full:
                    self._discard_one()
        self.thread.join(timeout=5)
        try:
            self.consumer.close()
        except Exception:
            pass

    def _discard_one(self) -> None:
        try:
            self.queue.get_nowait()
            self.queue.task_done()
        except Empty:
            pass

    def _discard_all(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Empty:
                return

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            try:
                if item is _STOP:
                    return
                outcome, observation = item
                try:
                    self.consumer.accept(outcome, observation)
                except Exception:
                    pass
            finally:
                self.queue.task_done()


class SessionEmbeddingCoordinator:
    """Own one session's shared inference, success cache, and consumer workers."""

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
        self._workers: dict[str, _ConsumerWorker] = {}
        self._lock = Lock()
        self._closed = False

    def embed_image(
        self,
        observation: ImageObservation,
        *,
        priority: EmbeddingPriority = "background",
    ) -> EmbeddingOutcome:
        return self._compute_and_dispatch(observation, priority=priority)

    def embed_text(
        self,
        observation: TextObservation,
        *,
        priority: EmbeddingPriority = "interactive",
    ) -> EmbeddingOutcome:
        return self._compute_and_dispatch(observation, priority=priority)

    def register_consumer(
        self,
        consumer: EmbeddingConsumer,
        *,
        queue_size: int = 64,
        overflow_policy: OverflowPolicy = "drop_oldest",
    ) -> None:
        if overflow_policy not in {"latest_wins", "drop_oldest", "reject_new"}:
            raise ValueError("unsupported consumer overflow policy")
        with self._lock:
            self._ensure_open()
            if consumer.consumer_id in self._workers:
                raise ValueError("embedding_consumer_already_registered")
            self._workers[consumer.consumer_id] = _ConsumerWorker(
                consumer=consumer,
                queue_size=queue_size,
                overflow_policy=overflow_policy,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers.values())
            self._workers.clear()
            self._cache.clear()
        for worker in workers:
            worker.close()
        emit_embedding_observation(self.observer, "embedding.session_cleanup")

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def has_consumer_for(self, modality: str) -> bool:
        """Report structured modality interest without inferring user intent."""

        if modality not in {"image", "text"}:
            raise ValueError("unsupported embedding modality")
        with self._lock:
            return any(
                modality in getattr(worker.consumer, "modalities", {"image", "text"})
                for worker in self._workers.values()
            )

    def _compute_and_dispatch(
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
            workers = list(self._workers.values())
        for worker in workers:
            if not worker.enqueue(outcome, observation):
                emit_embedding_observation(
                    self.observer,
                    "embedding.consumer_dropped",
                    outcome=outcome,
                    observation=observation,
                    consumer_id=worker.consumer.consumer_id,
                )
        emit_embedding_observation(
            self.observer,
            "embedding.failed" if isinstance(outcome, EmbeddingFailureEvent) else "embedding.finished",
            outcome=outcome,
            observation=observation,
            priority=priority,
        )
        emit_embedding_observation(
            self.observer,
            "embedding.dispatched",
            outcome=outcome,
            observation=observation,
            consumer_count=len(workers),
        )
        return outcome

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("embedding_coordinator_closed")
