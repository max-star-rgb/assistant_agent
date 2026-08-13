"""Composition root for the single active graph-native memory backend."""

from __future__ import annotations

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
            return create_langmem_memory_bundle(
                model=resolved.langmem_model,
                store=langmem_store,
                ledger=ledger,
            )
        except LangMemConfigurationError as exc:
            raise MemoryBackendConfigurationError(str(exc)) from exc
    raise MemoryBackendConfigurationError(
        f"Unsupported memory backend: {resolved.memory_backend!r}."
    )


__all__ = ["MemoryBackendConfigurationError", "create_memory_node_bundle"]
