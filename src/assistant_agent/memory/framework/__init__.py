"""External framework memory adapters behind assistant_agent governance."""

from assistant_agent.memory.framework.adapters import (
    HindsightMemoryEngineAdapter,
    Mem0MemoryEngineAdapter,
    UnavailableMemoryEngineAdapter,
)
from assistant_agent.memory.framework.base import (
    FrameworkHttpRequest,
    MemoryEngineAdapter,
    bind_engine_identity,
)

__all__ = [
    "FrameworkHttpRequest",
    "HindsightMemoryEngineAdapter",
    "Mem0MemoryEngineAdapter",
    "MemoryEngineAdapter",
    "bind_engine_identity",
    "UnavailableMemoryEngineAdapter",
]
