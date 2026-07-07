"""Memory store factory helpers."""

from pathlib import Path

from assistant_agent.config import DEFAULT_JSONL_MEMORY_PATH, DEFAULT_SQLITE_MEMORY_PATH, ProviderConfig
from assistant_agent.memory.jsonl_store import JsonlMemoryStore
from assistant_agent.memory.remote import HybridMemoryStore, RemoteMemoryClient
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore
from assistant_agent.memory.store import InMemoryStore, MemoryStore


REPO_ROOT = Path(__file__).resolve().parents[3]


def create_memory_store(config: ProviderConfig | None = None) -> MemoryStore:
    """Create a memory store from runtime configuration."""

    resolved_config = config or ProviderConfig.from_env()
    if resolved_config.memory_backend == "jsonl":
        return JsonlMemoryStore(_repo_relative_path(resolved_config.memory_path))
    if resolved_config.memory_backend == "sqlite":
        return SQLiteMemoryStore(_repo_relative_path(_sqlite_memory_path(resolved_config.memory_path)))
    if resolved_config.memory_backend == "hybrid_remote":
        local_store = JsonlMemoryStore(_repo_relative_path(resolved_config.memory_path))
        if not resolved_config.memory_server_base_url:
            return local_store
        return HybridMemoryStore(
            local_store=local_store,
            remote_client=RemoteMemoryClient(
                base_url=resolved_config.memory_server_base_url,
                timeout_seconds=resolved_config.memory_server_timeout_seconds,
                query_strategy=resolved_config.memory_server_query_strategy,
                include_media_chunks=resolved_config.memory_server_include_media_chunks,
                direct_answer=resolved_config.memory_server_direct_answer,
            ),
        )
    return InMemoryStore()


def _repo_relative_path(path: str) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return REPO_ROOT / resolved


def _sqlite_memory_path(path: str) -> str:
    if path == DEFAULT_JSONL_MEMORY_PATH:
        return DEFAULT_SQLITE_MEMORY_PATH
    return path
