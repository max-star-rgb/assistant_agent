"""Helpers for prompt-safe memory core observability."""

from __future__ import annotations

from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.remote import HybridMemoryStore, RemoteServiceMemoryStore
from assistant_agent.memory.framework.store import FrameworkMemoryStore
from assistant_agent.schemas.memory_core import MemoryCoreStatus


def build_memory_core_status(
    *,
    config: ProviderConfig | None = None,
    memory_store: Any,
    remote_errors: list[dict[str, Any]] | None = None,
) -> MemoryCoreStatus:
    """Return a prompt-safe description of the active memory core topology."""

    active_store = type(memory_store).__name__
    backend = config.memory_backend if config is not None else _backend_from_store(memory_store)
    local_backend = config.memory_local_backend if config is not None else _local_backend_from_store(memory_store)
    local_store = _local_store_name(memory_store)
    mode = _mode(backend=backend, memory_store=memory_store)
    error_codes = _safe_error_codes(remote_errors)
    external_configured = _external_configured(
        mode=mode,
        backend=backend,
        config=config,
        memory_store=memory_store,
    )
    remote_query_enabled = (
        mode == "dual_core" and external_configured and isinstance(memory_store, HybridMemoryStore)
    ) or (mode == "framework" and external_configured)
    remote_query_degraded = bool(error_codes)
    remote_status = _remote_status(
        mode=mode,
        external_configured=external_configured,
        remote_query_enabled=remote_query_enabled,
        remote_query_degraded=remote_query_degraded,
    )
    return MemoryCoreStatus(
        mode=mode,
        memory_backend=backend,
        memory_local_backend=local_backend,
        active_store=active_store,
        local_core=local_backend if mode in {"local_only", "dual_core"} else None,
        local_store=local_store,
        external_core=(
            memory_store.adapter.name
            if mode == "framework" and isinstance(memory_store, FrameworkMemoryStore)
            else "memory_server" if mode in {"dual_core", "remote_service"} else None
        ),
        external_core_configured=external_configured,
        external_lifecycle_owner=mode in {"remote_service", "framework"},
        remote_query_enabled=remote_query_enabled,
        remote_query_degraded=remote_query_degraded,
        remote_status=remote_status,
        remote_error_codes=error_codes,
    )


def update_memory_core_status_errors(
    value: dict[str, Any],
    *,
    remote_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a status dict updated with safe remote query error codes."""

    updated = dict(value)
    error_codes = _safe_error_codes(remote_errors)
    updated["remote_error_codes"] = error_codes
    updated["remote_query_degraded"] = bool(error_codes)
    if error_codes:
        updated["remote_status"] = "degraded"
    return updated


def _mode(*, backend: str, memory_store: Any) -> str:
    if backend == "framework" or isinstance(memory_store, FrameworkMemoryStore):
        return "framework"
    if backend in {"dual_core", "hybrid_remote"} or isinstance(memory_store, HybridMemoryStore):
        return "dual_core"
    if backend == "remote_service" or isinstance(memory_store, RemoteServiceMemoryStore):
        return "remote_service"
    return "local_only"


def _backend_from_store(memory_store: Any) -> str:
    if isinstance(memory_store, FrameworkMemoryStore):
        return "framework"
    if isinstance(memory_store, HybridMemoryStore):
        return "dual_core"
    if isinstance(memory_store, RemoteServiceMemoryStore):
        return "remote_service"
    name = type(memory_store).__name__
    if name == "JsonlMemoryStore":
        return "jsonl"
    if name == "SQLiteMemoryStore":
        return "sqlite"
    return "memory"


def _local_backend_from_store(memory_store: Any) -> str:
    if isinstance(memory_store, FrameworkMemoryStore):
        fallback = memory_store.read_fallback
        if fallback is None:
            return "memory"
        memory_store = fallback
    local_store = getattr(memory_store, "local_store", memory_store)
    name = type(local_store).__name__
    if name == "JsonlMemoryStore":
        return "jsonl"
    if name == "SQLiteMemoryStore":
        return "sqlite"
    return "memory"


def _local_store_name(memory_store: Any) -> str | None:
    if isinstance(memory_store, FrameworkMemoryStore):
        return type(memory_store.read_fallback).__name__ if memory_store.read_fallback is not None else None
    local_store = getattr(memory_store, "local_store", memory_store)
    if isinstance(memory_store, RemoteServiceMemoryStore):
        return None
    return type(local_store).__name__


def _external_configured(
    *,
    mode: str,
    backend: str,
    config: ProviderConfig | None,
    memory_store: Any,
) -> bool:
    if mode == "dual_core":
        return isinstance(memory_store, HybridMemoryStore) and (
            config is None or bool(config.memory_server_base_url)
        )
    if mode == "remote_service":
        return isinstance(memory_store, RemoteServiceMemoryStore) and backend == "remote_service"
    if mode == "framework":
        return (
            isinstance(memory_store, FrameworkMemoryStore)
            and backend == "framework"
            and config is not None
            and bool(config.memory_framework_base_url)
        )
    return False


def _remote_status(
    *,
    mode: str,
    external_configured: bool,
    remote_query_enabled: bool,
    remote_query_degraded: bool,
) -> str:
    if mode == "local_only":
        return "not_applicable"
    if not external_configured:
        return "not_configured"
    if mode in {"remote_service", "framework"}:
        return "lifecycle_owner"
    if remote_query_degraded:
        return "degraded"
    if remote_query_enabled:
        return "configured"
    return "not_configured"


def _safe_error_codes(errors: list[dict[str, Any]] | None) -> list[str]:
    if not errors:
        return []
    codes: list[str] = []
    for error in errors:
        code = error.get("code") if isinstance(error, dict) else None
        if (
            isinstance(code, str)
            and code.startswith(("memory_server_", "memory_remote_service_", "memory_framework_"))
            and code not in codes
        ):
            codes.append(code)
    return codes
