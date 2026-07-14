"""Memory store factory helpers."""

from pathlib import Path

from assistant_agent.config import (
    DEFAULT_JSONL_MEMORY_PATH,
    DEFAULT_SQLITE_MEMORY_PATH,
    LocalMemoryBackend,
    ProviderConfig,
)
from assistant_agent.memory.jsonl_store import JsonlMemoryStore
from assistant_agent.memory.framework import (
    HindsightMemoryEngineAdapter,
    Mem0MemoryEngineAdapter,
    UnavailableMemoryEngineAdapter,
)
from assistant_agent.memory.framework.ledger import FrameworkGovernanceLedger
from assistant_agent.memory.framework.store import FrameworkMemoryStore
from assistant_agent.memory.remote import (
    HybridMemoryStore,
    HttpRemoteMemoryServiceAdapter,
    RemoteMemoryClient,
    RemoteServiceMemoryStore,
    UnavailableRemoteMemoryServiceAdapter,
)
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore
from assistant_agent.memory.store import InMemoryStore, MemoryStore


REPO_ROOT = Path(__file__).resolve().parents[3]


def create_memory_store(config: ProviderConfig | None = None) -> MemoryStore:
    """Create a memory store from runtime configuration."""

    resolved_config = config or ProviderConfig.from_env()
    if resolved_config.memory_backend == "jsonl":
        return _create_local_memory_store("jsonl", resolved_config.memory_path)
    if resolved_config.memory_backend == "sqlite":
        return _create_local_memory_store("sqlite", resolved_config.memory_path)
    if resolved_config.memory_backend in {"hybrid_remote", "dual_core"}:
        local_store = _create_local_memory_store(
            resolved_config.memory_local_backend,
            resolved_config.memory_path,
        )
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
    if resolved_config.memory_backend == "remote_service":
        return RemoteServiceMemoryStore(
            adapter=_create_remote_service_adapter(resolved_config),
        )
    if resolved_config.memory_backend == "framework":
        fallback = None
        if resolved_config.memory_framework_fallback_backend != "none":
            fallback = _create_local_memory_store(
                resolved_config.memory_framework_fallback_backend,
                resolved_config.memory_path,
            )
        return FrameworkMemoryStore(
            adapter=_create_framework_adapter(resolved_config),
            ledger=FrameworkGovernanceLedger(
                _repo_relative_path(resolved_config.memory_framework_ledger_path)
            ),
            identity_namespace=resolved_config.memory_framework_identity_namespace,
            read_fallback=fallback,
        )
    return InMemoryStore()


def _create_remote_service_adapter(config: ProviderConfig):
    if config.memory_remote_service_adapter == "http" and config.memory_server_base_url:
        return HttpRemoteMemoryServiceAdapter(
            base_url=config.memory_server_base_url,
            timeout_seconds=config.memory_server_timeout_seconds,
        )
    return UnavailableRemoteMemoryServiceAdapter(base_url=config.memory_server_base_url)


def _create_framework_adapter(config: ProviderConfig):
    if not config.memory_framework_base_url:
        return UnavailableMemoryEngineAdapter(name=config.memory_framework)
    if config.memory_framework == "mem0":
        return Mem0MemoryEngineAdapter(
            base_url=config.memory_framework_base_url,
            timeout_seconds=config.memory_framework_timeout_seconds,
            api_key=config.memory_framework_api_key,
        )
    return HindsightMemoryEngineAdapter(
        base_url=config.memory_framework_base_url,
        timeout_seconds=config.memory_framework_timeout_seconds,
    )


def _create_local_memory_store(backend: LocalMemoryBackend, path: str) -> MemoryStore:
    if backend == "jsonl":
        return JsonlMemoryStore(_repo_relative_path(path))
    if backend == "sqlite":
        return SQLiteMemoryStore(_repo_relative_path(_sqlite_memory_path(path)))
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
