"""Composition root for the governed long-term Memory Plugin lifecycle."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.plugins.assembly import assemble_memory_plugins
from assistant_agent.memory.plugins.builtin.mem0 import (
    default_memory_plugin_factories,
)
from assistant_agent.memory.plugins.config import (
    MemoryPluginsConfig,
    load_memory_plugins_config,
)
from assistant_agent.memory.plugins.contracts import (
    MemoryPluginBuildContext,
    MemoryPluginExecutionPolicy,
)
from assistant_agent.memory.plugins.host import MemoryPluginHost
from assistant_agent.memory.plugins.media import ManagedMemoryMediaStore
from assistant_agent.memory.plugins.session_store import MemoryPluginSessionStore
from assistant_agent.memory.service import LongTermMemoryService


class _EnvironmentMemorySecretResolver:
    def __init__(self, source: Mapping[str, str] | None = None) -> None:
        self._source = os.environ if source is None else source

    def resolve(self, reference: str) -> str:
        value = self._source.get(reference)
        if value is None:
            raise KeyError("memory_plugin_secret_missing")
        return value


def create_long_term_memory_service(
    config: ProviderConfig | None = None,
    *,
    session_store: MemoryPluginSessionStore | None = None,
    media_store: ManagedMemoryMediaStore | None = None,
) -> LongTermMemoryService:
    """Assemble one exclusive Memory Plugin and its Host-owned resources."""

    resolved = config or ProviderConfig.from_env()
    plugin_config = (
        load_memory_plugins_config(resolved.memory_plugin_config_path)
        if resolved.memory_plugin_config_path
        else MemoryPluginsConfig(
            schema_version="assistant_memory_plugins_v1",
            slot="mem0",
            plugins={},
        )
    )
    execution_policy = MemoryPluginExecutionPolicy(
        open_session_timeout_seconds=(
            resolved.memory_plugin_open_timeout_seconds
        ),
        prepare_context_timeout_seconds=(
            resolved.memory_plugin_prepare_timeout_seconds
        ),
        ingest_turn_timeout_seconds=(
            resolved.memory_plugin_ingest_timeout_seconds
        ),
        close_session_timeout_seconds=(
            resolved.memory_plugin_close_timeout_seconds
        ),
    )
    resolved_media_store = media_store or ManagedMemoryMediaStore(
        max_total_bytes=execution_policy.max_media_bytes_per_turn
    )
    build_context = MemoryPluginBuildContext(
        provider_mode=resolved.provider_mode,
        media_reader=resolved_media_store,
        artifact_writer=resolved_media_store,
        secret_resolver=_EnvironmentMemorySecretResolver(),
        clock=lambda: datetime.now(timezone.utc),
    )
    builtin_factories = tuple(
        factory
        for factory in default_memory_plugin_factories(resolved)
        if factory.descriptor.plugin_id not in plugin_config.plugins
    )
    registry = assemble_memory_plugins(
        config=plugin_config,
        builtin_factories=builtin_factories,
        build_context=build_context,
    )
    host = MemoryPluginHost(
        registry=registry,
        session_store=(
            session_store
            or MemoryPluginSessionStore(
                max_entries=resolved.memory_session_snapshot_max_entries
            )
        ),
        media_store=resolved_media_store,
        ingestion_queue=MemoryIngestionQueue(
            max_workers=resolved.memory_ingestion_max_workers,
            max_pending=resolved.memory_ingestion_max_pending,
            shutdown_timeout_seconds=(
                resolved.memory_ingestion_shutdown_timeout_seconds
            ),
        ),
        execution_policy=execution_policy,
        identity_namespace=resolved.mem0_identity_namespace,
    )
    return LongTermMemoryService(host=host)
