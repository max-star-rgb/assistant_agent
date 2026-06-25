"""Memory store factory helpers."""

from pathlib import Path

from multimodal_agent.config import ProviderConfig
from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.store import InMemoryStore, MemoryStore


REPO_ROOT = Path(__file__).resolve().parents[3]


def create_memory_store(config: ProviderConfig | None = None) -> MemoryStore:
    """Create a memory store from runtime configuration."""

    resolved_config = config or ProviderConfig.from_env()
    if resolved_config.memory_backend == "jsonl":
        return JsonlMemoryStore(_repo_relative_path(resolved_config.memory_path))
    return InMemoryStore()


def _repo_relative_path(path: str) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return REPO_ROOT / resolved
