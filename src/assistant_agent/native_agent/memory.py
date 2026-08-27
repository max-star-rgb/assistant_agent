"""Minimal long-term memory protocol and fixed native graph nodes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
import hashlib
import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.models import Mem0CompletedTurn
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.native_agent.providers import create_chat_model
from assistant_agent.native_agent.state import (
    AssistantRootState,
    MemoryExtractionState,
)


logger = logging.getLogger(__name__)


class MemoryBackendConfigurationError(RuntimeError):
    """The selected native memory backend cannot be constructed safely."""


_LANGMEM_MEMORY_INSTRUCTIONS = """\
你是长期记忆管理器，负责维护语义记忆、程序性记忆和情景记忆。

从本次交互中判断助理应长期记住哪些稳定、跨会话仍然有用的用户信息或响应方式：

1. 提取并补充上下文：识别稳定的用户事实、关系、偏好、长期目标、可复用流程和适用场景；对不确定的推断标注置信度并说明依据。
2. 比较并更新：关注偏离已有记忆的新信息；根据可靠性和时效性合并重复内容、修正错误，并保持内部一致。
3. 综合并推理：用演绎、归纳或溯因总结稳定模式，但不要把一次性请求、测试口令或低置信度猜测当作长期事实。
4. 严格排除时效性事实：不要保存天气、新闻、股价、日期时间、交通状态、搜索结果、网页内容或 Tool observation；
   即使用户随后可能再次询问，这些信息也必须在当次请求中重新获取。
5. 严格排除助理生成的内容：不要把助理的回答、自我描述、能力限制、知识截止日期、所谓“知识库”状态、
   引用来源或对当前运行环境的猜测保存为长期记忆。只有用户明确表达且具有长期价值的事实才可写入。

所有新增或更新的记忆正文必须使用简体中文。代码、协议字段、产品名和其他专有名词可保留原文；引用外语内容时，
应以中文说明其含义。记忆应当紧凑、完整、可独立理解，并保留必要的置信度与理由。
"""


@runtime_checkable
class MemoryBackend(Protocol):
    """Only the two operations owned by the parent graph memory nodes."""

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
        del messages, store
        bound_identity = self._identity(identity, thread_id=thread_id, run_id=run_id)
        values = await asyncio.to_thread(
            self._client.recall_long_term_memory,
            bound_identity,
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
                    identity,
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


class LangMemMemoryBackend:
    backend_id = "langmem"

    def __init__(self, *, manager: Any) -> None:
        self._manager = manager

    async def recall(
        self,
        *,
        identity: str,
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
            _langmem_namespace(identity),
            query=_last_human_text(messages) or None,
            limit=32,
        )
        return _bounded_texts(_store_item_text(value) for value in values)

    async def commit(
        self,
        *,
        identity: str,
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
        value = {"messages": [HumanMessage(content=user_text)]}
        config = {
            "configurable": {
                "langgraph_user_id": _langmem_namespace(identity)[-1],
            }
        }
        if hasattr(self._manager, "ainvoke"):
            await self._manager.ainvoke(value, config=config)
        else:
            await asyncio.to_thread(self._manager.invoke, value, config=config)


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
                *(f"[长期视觉记忆] {value}" for value in visual_result),
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
    config: ProviderConfig | None = None,
    *,
    custom_backend: MemoryBackend | None = None,
    mem0_client: Any | None = None,
    langmem_manager: Any | None = None,
    langmem_store: BaseStore | None = None,
    visual_memory_client: Any | None = None,
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
        backend: MemoryBackend = LangMemMemoryBackend(manager=manager)
        if resolved.remote_visual_memory_enabled:
            if visual_memory_client is None:
                from assistant_agent.memory.remote_service import (
                    RemoteMemoryServiceClient,
                )

                visual_memory_client = RemoteMemoryServiceClient(
                    base_url=resolved.remote_visual_memory_base_url or "",
                    timeout_seconds=(
                        resolved.remote_visual_memory_query_timeout_seconds
                    ),
                )
            backend = HybridMemoryBackend(
                text_backend=backend,
                visual_client=visual_memory_client,
                visual_timeout_seconds=(
                    resolved.remote_visual_memory_query_timeout_seconds
                ),
                visual_top_k=resolved.remote_visual_memory_query_top_k,
            )
        return backend
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

    if not runtime.context.enable_memory:
        return {"memory_context": (), "memory_status": "empty"}
    memories = await backend.recall(
        identity=authenticated_user_identity(runtime),
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
            instructions=_LANGMEM_MEMORY_INSTRUCTIONS,
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


def _langmem_namespace(identity: str) -> tuple[str, str]:
    identity = hashlib.sha256(
        f"assistant_agent:native_memory\0{identity}".encode()
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
    "HybridMemoryBackend",
    "MemoryBackend",
    "MemoryBackendConfigurationError",
    "create_memory_backend",
    "memory_extract_node",
    "memory_recall_node",
]
