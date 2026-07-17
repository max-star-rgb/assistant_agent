"""Memory store factory helpers."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
_BACKEND_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class MemoryStoreBackendContext:
    """Context passed to memory store backend plugins."""

    config: ProviderConfig
    repo_root: Path

    def repo_relative_path(self, path: str) -> Path:
        """Resolve repository-relative memory paths without exposing globals."""

        return _repo_relative_path(path, repo_root=self.repo_root)

    def sqlite_memory_path(self, path: str) -> str:
        """Return the SQLite path equivalent for a memory path."""

        return _sqlite_memory_path(path)

    def create_local_store(
        self,
        backend: LocalMemoryBackend | None = None,
        path: str | None = None,
    ) -> MemoryStore:
        """Create one of the built-in local stores for plugin composition."""

        return _create_local_memory_store(
            backend or self.config.memory_local_backend,
            path or self.config.memory_path,
            repo_root=self.repo_root,
        )


class MemoryStoreBackendFactory(Protocol):
    """Factory contract for pluggable memory store backends."""

    def __call__(self, context: MemoryStoreBackendContext) -> MemoryStore:
        """Create a memory store for the supplied runtime context."""


_BUILTIN_MEMORY_STORE_BACKENDS: dict[str, MemoryStoreBackendFactory]
_MEMORY_STORE_BACKENDS: dict[str, MemoryStoreBackendFactory]


def create_memory_store(config: ProviderConfig | None = None) -> MemoryStore:
    """Create a memory store from runtime configuration."""

    resolved_config = config or ProviderConfig.from_env()
    backend_name = _normalize_backend_name(str(resolved_config.memory_backend))
    backend_factory = _MEMORY_STORE_BACKENDS.get(backend_name)
    if backend_factory is None:
        raise ValueError(f"unregistered memory backend: {backend_name}")
    return backend_factory(MemoryStoreBackendContext(config=resolved_config, repo_root=REPO_ROOT))


def register_memory_store_backend(
    name: str,
    factory: MemoryStoreBackendFactory,
    *,
    replace: bool = False,
) -> None:
    """Register a memory store backend factory.

    Built-in backend names may be replaced only with ``replace=True`` so tests
    and local plugins cannot accidentally shadow production defaults.
    """

    backend_name = _normalize_backend_name(name)
    if backend_name in _MEMORY_STORE_BACKENDS and not replace:
        raise ValueError(f"memory backend already registered: {backend_name}")
    _MEMORY_STORE_BACKENDS[backend_name] = factory


def unregister_memory_store_backend(name: str) -> None:
    """Unregister a plugin backend or restore a replaced built-in backend."""

    backend_name = _normalize_backend_name(name)
    if backend_name in _BUILTIN_MEMORY_STORE_BACKENDS:
        _MEMORY_STORE_BACKENDS[backend_name] = _BUILTIN_MEMORY_STORE_BACKENDS[backend_name]
        return
    _MEMORY_STORE_BACKENDS.pop(backend_name, None)


def list_memory_store_backends() -> tuple[str, ...]:
    """Return registered memory backend names."""

    return tuple(sorted(_MEMORY_STORE_BACKENDS))


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


def _create_memory_backend(context: MemoryStoreBackendContext) -> MemoryStore:
    return InMemoryStore()


def _create_jsonl_backend(context: MemoryStoreBackendContext) -> MemoryStore:
    return context.create_local_store("jsonl", context.config.memory_path)


def _create_sqlite_backend(context: MemoryStoreBackendContext) -> MemoryStore:
    return context.create_local_store("sqlite", context.config.memory_path)


def _create_dual_core_backend(context: MemoryStoreBackendContext) -> MemoryStore:
    resolved_config = context.config
    local_store = context.create_local_store(
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


def _create_remote_service_backend(context: MemoryStoreBackendContext) -> MemoryStore:
    return RemoteServiceMemoryStore(
        adapter=_create_remote_service_adapter(context.config),
    )


def _create_framework_backend(context: MemoryStoreBackendContext) -> MemoryStore:
    resolved_config = context.config
    fallback = None
    if resolved_config.memory_framework_fallback_backend != "none":
        fallback = context.create_local_store(
            resolved_config.memory_framework_fallback_backend,
            resolved_config.memory_path,
        )
    return FrameworkMemoryStore(
        adapter=_create_framework_adapter(resolved_config),
        ledger=FrameworkGovernanceLedger(
            context.repo_relative_path(resolved_config.memory_framework_ledger_path)
        ),
        identity_namespace=resolved_config.memory_framework_identity_namespace,
        read_fallback=fallback,
    )


def _create_local_memory_store(
    backend: LocalMemoryBackend,
    path: str,
    *,
    repo_root: Path | None = None,
) -> MemoryStore:
    if backend == "jsonl":
        return JsonlMemoryStore(_repo_relative_path(path, repo_root=repo_root))
    if backend == "sqlite":
        return SQLiteMemoryStore(
            _repo_relative_path(_sqlite_memory_path(path), repo_root=repo_root)
        )
    return InMemoryStore()


def _repo_relative_path(path: str, *, repo_root: Path | None = None) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return (repo_root or REPO_ROOT) / resolved


def _sqlite_memory_path(path: str) -> str:
    if path == DEFAULT_JSONL_MEMORY_PATH:
        return DEFAULT_SQLITE_MEMORY_PATH
    return path


def _normalize_backend_name(name: str) -> str:
    backend_name = name.strip().lower()
    if not _BACKEND_NAME_PATTERN.fullmatch(backend_name):
        raise ValueError(f"invalid memory backend name: {name!r}")
    return backend_name


_BUILTIN_MEMORY_STORE_BACKENDS = {
    "memory": _create_memory_backend,
    "jsonl": _create_jsonl_backend,
    "sqlite": _create_sqlite_backend,
    "hybrid_remote": _create_dual_core_backend,
    "dual_core": _create_dual_core_backend,
    "remote_service": _create_remote_service_backend,
    "framework": _create_framework_backend,
}
_MEMORY_STORE_BACKENDS = dict(_BUILTIN_MEMORY_STORE_BACKENDS)
