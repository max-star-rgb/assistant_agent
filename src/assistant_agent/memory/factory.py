"""Composition root for the single active graph-native memory backend."""

from __future__ import annotations

import os

from langgraph.store.base import BaseStore

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.backends.disabled import build_disabled_memory_bundle
from assistant_agent.memory.backends.langmem import (
    LangMemConfigurationError,
    create_langmem_memory_bundle,
)
from assistant_agent.memory.backends.mem0 import build_mem0_memory_bundle
from assistant_agent.memory.commit_ledger import SQLiteMemoryCommitLedger
from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.node_bundle import MemoryNodeBundle


class MemoryBackendConfigurationError(RuntimeError):
    """Trusted backend configuration is incomplete or unavailable."""


def create_memory_node_bundle(
    config: ProviderConfig | None = None,
    *,
    langmem_store: BaseStore | None = None,
) -> MemoryNodeBundle:
    """Construct exactly one backend without probing keys or falling back."""

    resolved = config or ProviderConfig.from_env()
    if resolved.memory_backend == "disabled":
        return build_disabled_memory_bundle()
    if resolved.provider_mode != "real":
        raise MemoryBackendConfigurationError(
            f"Memory backend '{resolved.memory_backend}' requires real provider mode."
        )
    ledger = SQLiteMemoryCommitLedger(resolved.memory_commit_ledger_path)
    if resolved.memory_backend == "mem0":
        if not resolved.mem0_base_url:
            raise MemoryBackendConfigurationError(
                "Memory backend 'mem0' requires MEM0_BASE_URL."
            )
        return build_mem0_memory_bundle(
            client=Mem0Client(
                base_url=resolved.mem0_base_url,
                api_key=resolved.mem0_api_key,
                timeout_seconds=resolved.mem0_timeout_seconds,
            ),
            ledger=ledger,
            identity_namespace=resolved.mem0_identity_namespace,
        )
    if resolved.memory_backend == "langmem":
        if langmem_store is None:
            raise MemoryBackendConfigurationError(
                "Memory backend 'langmem' requires an explicit LangGraph BaseStore."
            )
        if not resolved.langmem_model:
            raise MemoryBackendConfigurationError(
                "Memory backend 'langmem' requires LANGMEM_MODEL."
            )
        try:
            model, close_model = _create_langmem_chat_model(resolved)
            return create_langmem_memory_bundle(
                model=model,
                store=langmem_store,
                ledger=ledger,
                aclose=close_model,
            )
        except LangMemConfigurationError as exc:
            raise MemoryBackendConfigurationError(str(exc)) from exc
    raise MemoryBackendConfigurationError(
        f"Unsupported memory backend: {resolved.memory_backend!r}."
    )


def _create_langmem_chat_model(config: ProviderConfig):
    """Bind LangMem to the same trusted OpenAI-compatible provider settings."""

    provider = config.resolved_chat_provider()
    if provider.adapter_kind != "openai_compatible":
        raise MemoryBackendConfigurationError(
            "Memory backend 'langmem' requires an OpenAI-compatible chat provider."
        )
    try:
        from langchain_openai import ChatOpenAI

        proxy_url = _langmem_proxy_url()
        sync_client = None
        async_client = None
        if proxy_url is not None:
            import httpx

            sync_client = httpx.Client(proxy=proxy_url, trust_env=False)
            async_client = httpx.AsyncClient(proxy=proxy_url, trust_env=False)
        model = ChatOpenAI(
            api_key=provider.api_key or "not-required",
            base_url=provider.base_url,
            model=config.langmem_model,
            timeout=config.chat_timeout_seconds,
            http_client=sync_client,
            http_async_client=async_client,
            http_socket_options=(),
        )

        if sync_client is None or async_client is None:
            return model, None

        async def close_model_clients() -> None:
            await async_client.aclose()
            sync_client.close()

        return model, close_model_clients
    except ImportError:
        raise MemoryBackendConfigurationError(
            "Memory backend 'langmem' requires the memory-langmem optional dependency."
        ) from None
    except Exception:
        raise MemoryBackendConfigurationError(
            "Memory backend 'langmem' could not initialize its configured chat provider."
        ) from None


def _langmem_proxy_url() -> str | None:
    for key in (
        "OPENAI_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        value = os.environ.get(key)
        if not value:
            continue
        if value.startswith("socks://"):
            return "socks5://" + value.removeprefix("socks://")
        return value
    return None


__all__ = ["MemoryBackendConfigurationError", "create_memory_node_bundle"]
