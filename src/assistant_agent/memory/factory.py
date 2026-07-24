"""Construct the single long-term memory service."""

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.mem0.client import (
    Mem0Client,
    UnavailableMem0Client,
)
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.memory.session_snapshot import (
    SessionMemorySnapshotStore,
    get_default_session_memory_snapshot_store,
)


def create_long_term_memory_service(
    config: ProviderConfig | None = None,
    *,
    snapshot_store: SessionMemorySnapshotStore | None = None,
) -> LongTermMemoryService:
    """Create the runtime-facing service with no local memory fallback."""

    resolved = config or ProviderConfig.from_env()
    client = (
        Mem0Client(
            base_url=resolved.mem0_base_url,
            identity_namespace=resolved.mem0_identity_namespace,
            timeout_seconds=resolved.mem0_timeout_seconds,
            api_key=resolved.mem0_api_key,
        )
        if resolved.provider_mode == "real" and resolved.mem0_base_url
        else UnavailableMem0Client()
    )
    return LongTermMemoryService(
        client=client,
        snapshot_store=(
            snapshot_store
            or get_default_session_memory_snapshot_store(
                max_entries=resolved.memory_session_snapshot_max_entries
            )
        ),
        ingestion_queue=MemoryIngestionQueue(
            max_workers=resolved.memory_ingestion_max_workers,
            max_pending=resolved.memory_ingestion_max_pending,
            shutdown_timeout_seconds=(
                resolved.memory_ingestion_shutdown_timeout_seconds
            ),
        ),
    )
