"""LangGraph checkpointer factory."""

from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from multimodal_agent.config import ProviderConfig


def create_checkpointer(config: ProviderConfig | None = None) -> Any | None:
    """Create the configured LangGraph checkpointer.

    Only the official in-memory saver is enabled for now. Persistent
    checkpointers should be added behind this factory once a storage backend is
    selected.
    """

    resolved_config = config or ProviderConfig.from_env({})
    if resolved_config.langgraph_checkpointer_backend == "none":
        return None
    return MemorySaver()
