"""Minimal long-term memory protocol and native lifecycle operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import logging
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore

from assistant_agent.config import MediaConfig, MemoryConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.models import Mem0Conversation, Mem0UserMessage
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.native_agent.state import (
    AssistantAgentState,
    MemoryExtractionState,
)


logger = logging.getLogger(__name__)


class MemoryBackendConfigurationError(RuntimeError):
    """The selected native memory backend cannot be constructed safely."""


@runtime_checkable
class MemoryBackend(Protocol):
    """Only the two operations owned by Memory middleware and extraction graph."""

    backend_id: str

    async def recall(
        self,
        *,
        identity: str,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> tuple[str, ...]: ...

    async def commit(
        self,
        *,
        identity: str,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> None: ...


class DisabledMemoryBackend:
    backend_id = "disabled"

    async def recall(self, **_kwargs: Any) -> tuple[str, ...]:
        return ()

    async def commit(self, **_kwargs: Any) -> None:
        return None


class Mem0MemoryBackend:
    backend_id = "mem0"

    def __init__(self, *, client: Any, identity_namespace: str) -> None:
        self._client = client
        self._identity_namespace = identity_namespace

    async def recall(
        self,
        *,
        identity: str,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> tuple[str, ...]:
        del store
        query = _last_human_text(messages)
        if not query:
            return ()
        bound_identity = self._identity(identity, thread_id=thread_id, run_id=run_id)
        values = await asyncio.to_thread(
            self._client.recall_long_term_memory,
            bound_identity,
            query=query,
        )
        return _bounded_texts(getattr(value, "text", "") for value in values)

    async def commit(
        self,
        *,
        identity: str,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> None:
        del store
        user_messages = tuple(
            Mem0UserMessage(content=text)
            for message in messages
            if isinstance(message, HumanMessage) and (text := _message_text(message))
        )
        if not user_messages:
            return
        from datetime import datetime, timezone

        source_turn = run_id or thread_id
        if not source_turn:
            raise MemoryBackendConfigurationError(
                "Mem0 commit requires Agent Server run or thread identity."
            )
        result = await asyncio.to_thread(
            self._client.ingest_conversation,
            Mem0Conversation(
                identity=self._identity(
                    identity,
                    thread_id=thread_id,
                    run_id=run_id,
                ),
                messages=user_messages,
                occurred_at=datetime.now(timezone.utc),
                source_turn=source_turn,
            ),
        )
        if not result.accepted:
            raise RuntimeError("Mem0 rejected the completed turn.")

    def _identity(
        self,
        identity: str,
        *,
        thread_id: str | None,
        run_id: str | None,
    ):
        session_id = thread_id or run_id
        if not session_id:
            raise MemoryBackendConfigurationError(
                "Mem0 recall requires Agent Server thread or run identity."
            )
        return bind_mem0_identity(
            RequestIdentity.for_user(
                user_id=identity,
                session_id=session_id,
            ),
            namespace=self._identity_namespace,
        )


class HybridMemoryBackend:
    """Recall text and remote visual memories concurrently; commit text only."""

    backend_id = "hybrid"

    def __init__(
        self,
        *,
        text_backend: MemoryBackend,
        visual_client: Any,
        visual_timeout_seconds: float,
        visual_top_k: int,
    ) -> None:
        self._text_backend = text_backend
        self._visual_client = visual_client
        self._visual_timeout_seconds = visual_timeout_seconds
        self._visual_top_k = visual_top_k

    async def recall(
        self,
        *,
        identity: str,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> tuple[str, ...]:
        text_task = asyncio.create_task(
            self._text_backend.recall(
                identity=identity,
                thread_id=thread_id,
                run_id=run_id,
                messages=messages,
                store=store,
            )
        )
        visual_task = asyncio.create_task(
            self._visual_recall(
                identity=identity,
                query=_last_human_text(messages),
            )
        )
        text_result, visual_result = await asyncio.gather(text_task, visual_task)
        return _bounded_texts(
            (
                *text_result,
                *visual_result,
            )
        )

    async def _visual_recall(
        self,
        *,
        identity: str,
        query: str,
    ) -> tuple[str, ...]:
        if not query:
            return ()
        try:
            async with asyncio.timeout(self._visual_timeout_seconds):
                result = await self._visual_client.query_memories(
                    user_id=identity,
                    query=query,
                    top_k=self._visual_top_k,
                )
        except Exception as exc:  # noqa: BLE001 - optional dependency boundary.
            logger.warning(
                "visual_memory_recall_failed error_type=%s",
                type(exc).__name__,
            )
            return ()
        return _bounded_texts(result)

    async def commit(self, **kwargs: Any) -> None:
        await self._text_backend.commit(**kwargs)


def create_memory_backend(
    config: MemoryConfig,
    *,
    provider_mode: ProviderMode,
    media_config: MediaConfig,
    custom_backend: MemoryBackend | None = None,
    mem0_client: Any | None = None,
    visual_memory_client: Any | None = None,
) -> MemoryBackend:
    """Construct one backend without probing or silent fallback."""

    if custom_backend is not None:
        _validate_backend(custom_backend)
        return custom_backend
    if config.memory_backend == "disabled":
        return DisabledMemoryBackend()
    if provider_mode != "real":
        raise MemoryBackendConfigurationError(
            f"Memory backend '{config.memory_backend}' requires real provider mode."
        )
    if config.memory_backend == "mem0":
        if not config.mem0_base_url and mem0_client is None:
            raise MemoryBackendConfigurationError(
                "Memory backend 'mem0' requires MEM0_BASE_URL."
            )
        client = mem0_client or Mem0Client(
            base_url=config.mem0_base_url or "",
            api_key=config.mem0_api_key,
            timeout_seconds=config.mem0_timeout_seconds,
        )
        backend: MemoryBackend = Mem0MemoryBackend(
            client=client,
            identity_namespace=config.mem0_identity_namespace,
        )
        if media_config.remote_visual_memory_enabled:
            if visual_memory_client is None:
                from assistant_agent.memory.remote_service import (
                    RemoteMemoryServiceClient,
                )

                visual_memory_client = RemoteMemoryServiceClient(
                    base_url=media_config.remote_visual_memory_base_url or "",
                    timeout_seconds=(
                        media_config.remote_visual_memory_query_timeout_seconds
                    ),
                )
            backend = HybridMemoryBackend(
                text_backend=backend,
                visual_client=visual_memory_client,
                visual_timeout_seconds=(
                    media_config.remote_visual_memory_query_timeout_seconds
                ),
                visual_top_k=media_config.remote_visual_memory_query_top_k,
            )
        return backend
    raise MemoryBackendConfigurationError(
        f"Unsupported memory backend: {config.memory_backend!r}."
    )


async def recall_memory(
    state: AssistantAgentState,
    runtime: Runtime[AssistantRunContext],
    *,
    backend: MemoryBackend,
) -> dict[str, Any]:
    """Recall exactly once for a parent-graph attempt and freeze the result."""

    if not runtime.context.enable_memory:
        return {"memory_context": ()}
    memories = await backend.recall(
        identity=authenticated_user_identity(runtime),
        thread_id=_execution_value(runtime, "thread_id"),
        run_id=_execution_value(runtime, "run_id"),
        messages=tuple(state.get("messages", ())),
        store=runtime.store,
    )
    bounded = _bounded_texts(memories)
    return {"memory_context": bounded}


async def memory_extract_node(
    state: MemoryExtractionState,
    runtime: Runtime[AssistantRunContext],
    *,
    backend: MemoryBackend,
) -> dict[str, Any]:
    """Extract memories only during an explicit background Memory run."""

    await backend.commit(
        identity=authenticated_user_identity(runtime),
        thread_id=_execution_value(runtime, "thread_id"),
        run_id=_execution_value(runtime, "run_id"),
        messages=tuple(state.get("messages", ())),
        store=runtime.store,
    )
    return {}


def _validate_backend(backend: MemoryBackend) -> None:
    if not isinstance(getattr(backend, "backend_id", None), str):
        raise MemoryBackendConfigurationError("custom memory backend needs backend_id")
    if not callable(getattr(backend, "recall", None)) or not callable(
        getattr(backend, "commit", None)
    ):
        raise MemoryBackendConfigurationError(
            "custom memory backend must implement recall and commit"
        )


def _execution_value(runtime: Runtime[Any], field: str) -> str | None:
    execution = runtime.execution_info
    value = getattr(execution, field, None)
    return str(value) if value else None


def _bounded_texts(values) -> tuple[str, ...]:
    result: list[str] = []
    total = 0
    for value in values:
        text = str(value).strip()[:4_000]
        if not text or len(result) >= 32:
            continue
        remaining = 12_000 - total
        if remaining <= 0:
            break
        text = text[:remaining]
        result.append(text)
        total += len(text)
    return tuple(result)


def _last_human_text(messages: Sequence[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


def _message_text(message: AnyMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    return "\n".join(
        str(block.get("text", "")).strip()
        for block in message.content
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and str(block.get("text", "")).strip()
    )


__all__ = [
    "HybridMemoryBackend",
    "MemoryBackend",
    "MemoryBackendConfigurationError",
    "create_memory_backend",
    "memory_extract_node",
    "recall_memory",
]
