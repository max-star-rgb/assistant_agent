"""Minimal long-term memory protocol and fixed native graph nodes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
import hashlib
import importlib
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.errors import NodeError
from langgraph.graph import END
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from langgraph.types import Command

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.models import Mem0CompletedTurn
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.state import AssistantRootState


class MemoryBackendConfigurationError(RuntimeError):
    """The selected native memory backend cannot be constructed safely."""


@runtime_checkable
class MemoryBackend(Protocol):
    """Only the two operations owned by the parent graph memory nodes."""

    backend_id: str

    async def recall(
        self,
        *,
        context: AssistantRunContext,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> tuple[str, ...]: ...

    async def commit(
        self,
        *,
        context: AssistantRunContext,
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
        context: AssistantRunContext,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> tuple[str, ...]:
        del messages, store
        identity = self._identity(context, thread_id=thread_id, run_id=run_id)
        values = await asyncio.to_thread(
            self._client.recall_long_term_memory,
            identity,
        )
        return _bounded_texts(getattr(value, "text", "") for value in values)

    async def commit(
        self,
        *,
        context: AssistantRunContext,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> None:
        del store
        user_text, assistant_text = _completed_turn(messages)
        if not user_text or not assistant_text:
            return
        from datetime import datetime, timezone

        source_turn = run_id or thread_id
        if not source_turn:
            raise MemoryBackendConfigurationError(
                "Mem0 commit requires Agent Server run or thread identity."
            )
        result = await asyncio.to_thread(
            self._client.ingest_completed_turn,
            Mem0CompletedTurn(
                identity=self._identity(
                    context,
                    thread_id=thread_id,
                    run_id=run_id,
                ),
                user_text=user_text,
                assistant_text=assistant_text,
                occurred_at=datetime.now(timezone.utc),
                source_turn=source_turn,
            ),
        )
        if not result.accepted:
            raise RuntimeError("Mem0 rejected the completed turn.")

    def _identity(
        self,
        context: AssistantRunContext,
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
                user_id=context.user_id,
                session_id=session_id,
            ),
            namespace=self._identity_namespace,
        )


class LangMemMemoryBackend:
    backend_id = "langmem"

    def __init__(self, *, manager: Any) -> None:
        self._manager = manager

    async def recall(
        self,
        *,
        context: AssistantRunContext,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> tuple[str, ...]:
        del thread_id, run_id
        if store is None:
            raise MemoryBackendConfigurationError(
                "LangMem requires the BaseStore compiled into the graph."
            )
        values = await store.asearch(
            _langmem_namespace(context),
            query=_last_human_text(messages) or None,
            limit=32,
        )
        return _bounded_texts(_store_item_text(value) for value in values)

    async def commit(
        self,
        *,
        context: AssistantRunContext,
        thread_id: str | None,
        run_id: str | None,
        messages: Sequence[AnyMessage],
        store: BaseStore | None,
    ) -> None:
        del thread_id, run_id
        if store is None:
            raise MemoryBackendConfigurationError(
                "LangMem requires the BaseStore compiled into the graph."
            )
        user_text, assistant_text = _completed_turn(messages)
        if not user_text or not assistant_text:
            return
        value = {
            "messages": [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        }
        config = {
            "configurable": {
                "langgraph_user_id": _langmem_namespace(context)[-1],
            }
        }
        if hasattr(self._manager, "ainvoke"):
            await self._manager.ainvoke(value, config=config)
        else:
            await asyncio.to_thread(self._manager.invoke, value, config=config)


def create_memory_backend(
    config: ProviderConfig | None = None,
    *,
    custom_backend: MemoryBackend | None = None,
    mem0_client: Any | None = None,
    langmem_manager: Any | None = None,
    langmem_store: BaseStore | None = None,
) -> MemoryBackend:
    """Construct one backend without probing or silent fallback."""

    if custom_backend is not None:
        _validate_backend(custom_backend)
        return custom_backend
    resolved = config or ProviderConfig.from_env()
    if resolved.memory_backend == "disabled":
        return DisabledMemoryBackend()
    if resolved.provider_mode != "real":
        raise MemoryBackendConfigurationError(
            f"Memory backend '{resolved.memory_backend}' requires real provider mode."
        )
    if resolved.memory_backend == "mem0":
        if not resolved.mem0_base_url and mem0_client is None:
            raise MemoryBackendConfigurationError(
                "Memory backend 'mem0' requires MEM0_BASE_URL."
            )
        client = mem0_client or Mem0Client(
            base_url=resolved.mem0_base_url or "",
            api_key=resolved.mem0_api_key,
            timeout_seconds=resolved.mem0_timeout_seconds,
        )
        return Mem0MemoryBackend(
            client=client,
            identity_namespace=resolved.mem0_identity_namespace,
        )
    if resolved.memory_backend == "langmem":
        manager = langmem_manager or _create_langmem_manager(
            resolved,
            store=langmem_store,
        )
        return LangMemMemoryBackend(manager=manager)
    raise MemoryBackendConfigurationError(
        f"Unsupported memory backend: {resolved.memory_backend!r}."
    )


async def memory_recall_node(
    state: AssistantRootState,
    runtime: Runtime[AssistantRunContext],
    *,
    backend: MemoryBackend,
) -> dict[str, Any]:
    """Recall exactly once for a parent-graph attempt and freeze the result."""

    memories = await backend.recall(
        context=runtime.context,
        thread_id=_execution_value(runtime, "thread_id"),
        run_id=_execution_value(runtime, "run_id"),
        messages=tuple(state.get("messages", ())),
        store=runtime.store,
    )
    bounded = _bounded_texts(memories)
    return {
        "memory_context": bounded,
        "memory_status": "ready" if bounded else "empty",
    }


def memory_recall_degraded(
    _state: AssistantRootState,
    error: NodeError,
) -> Command[str]:
    """Use LangGraph recovery to continue with an explicit degraded snapshot."""

    del error
    return Command(
        update={"memory_context": (), "memory_status": "degraded"},
        goto="execution_router",
    )


async def memory_commit_node(
    state: AssistantRootState,
    runtime: Runtime[AssistantRunContext],
    *,
    backend: MemoryBackend,
) -> dict[str, Any]:
    """Commit after branch convergence without changing the produced answer."""

    await backend.commit(
        context=runtime.context,
        thread_id=_execution_value(runtime, "thread_id"),
        run_id=_execution_value(runtime, "run_id"),
        messages=tuple(state.get("messages", ())),
        store=runtime.store,
    )
    return {}


def memory_commit_degraded(
    _state: AssistantRootState,
    error: NodeError,
) -> Command[str]:
    """Use LangGraph recovery to keep the answer when optional commit fails."""

    del error
    return Command(goto=END)


def _create_langmem_manager(config: ProviderConfig, *, store: BaseStore | None):
    if store is None:
        raise MemoryBackendConfigurationError(
            "Memory backend 'langmem' requires an explicit LangGraph BaseStore."
        )
    if not config.langmem_model:
        raise MemoryBackendConfigurationError(
            "Memory backend 'langmem' requires LANGMEM_MODEL."
        )
    try:
        create_manager = getattr(
            importlib.import_module("langmem"),
            "create_memory_store_manager",
        )
        model = create_chat_model(replace(config, chat_model=config.langmem_model))
        return create_manager(
            model,
            namespace=("assistant_agent", "{langgraph_user_id}"),
            store=store,
        )
    except (ImportError, AttributeError) as exc:
        raise MemoryBackendConfigurationError(
            "LangMem optional dependency is required for backend 'langmem'."
        ) from exc
    except MemoryBackendConfigurationError:
        raise
    except Exception as exc:
        raise MemoryBackendConfigurationError(
            "LangMem manager configuration is invalid."
        ) from exc


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


def _langmem_namespace(context: AssistantRunContext) -> tuple[str, str]:
    identity = hashlib.sha256(
        f"assistant_agent:native_memory\0{context.tenant_id}\0{context.user_id}".encode()
    ).hexdigest()[:40]
    return ("assistant_agent", f"memory_subject_{identity}")


def _store_item_text(item: Any) -> str:
    value = getattr(item, "value", None)
    for _ in range(3):
        if isinstance(value, str):
            return value
        if not isinstance(value, Mapping):
            return "" if value is None else str(value)
        value = value.get("content", value.get("memory", value))
    return str(value) if not isinstance(value, Mapping) else ""


def _completed_turn(messages: Sequence[AnyMessage]) -> tuple[str, str]:
    assistant_text = ""
    assistant_index = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage):
            assistant_text = _message_text(message)
            assistant_index = index
            if assistant_text:
                break
    if assistant_index < 0:
        return "", ""
    for index in range(assistant_index - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage):
            return _message_text(message), assistant_text
    return "", ""


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
    "MemoryBackend",
    "MemoryBackendConfigurationError",
    "create_memory_backend",
    "memory_commit_degraded",
    "memory_commit_node",
    "memory_recall_degraded",
    "memory_recall_node",
]
