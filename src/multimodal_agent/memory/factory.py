"""Memory store factory helpers."""

from pathlib import Path

from multimodal_agent.config import ProviderConfig
from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.store import InMemoryStore, MemoryStore


def create_memory_store(config: ProviderConfig | None = None) -> MemoryStore:
    """Create a memory store from runtime configuration."""

    resolved_config = config or ProviderConfig.from_env()
    if resolved_config.memory_backend == "jsonl":
        return JsonlMemoryStore(Path(resolved_config.memory_path))
    return InMemoryStore()
