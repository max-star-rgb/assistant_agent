"""Construct the single Mem0 memory dependency."""

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.mem0 import (
    Mem0RestAdapter,
    UnavailableMem0Adapter,
)
from assistant_agent.memory.mem0.store import Mem0MemoryStore


def create_memory_store(
    config: ProviderConfig | None = None,
) -> Mem0MemoryStore:
    """Create Mem0 or its offline-safe unavailable adapter.

    There is no backend registry and no local fallback.
    """

    resolved = config or ProviderConfig.from_env()
    adapter = (
        Mem0RestAdapter(
            base_url=resolved.mem0_base_url,
            timeout_seconds=resolved.mem0_timeout_seconds,
            api_key=resolved.mem0_api_key,
        )
        if resolved.provider_mode == "real" and resolved.mem0_base_url
        else UnavailableMem0Adapter()
    )
    return Mem0MemoryStore(
        adapter=adapter,
        identity_namespace=resolved.mem0_identity_namespace,
    )
