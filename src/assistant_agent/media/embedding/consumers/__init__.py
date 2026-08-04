"""Built-in consumers of canonical embedding outcomes."""

from assistant_agent.media.embedding.consumers.keyframe import KeyframeChangeConsumer
from assistant_agent.media.embedding.consumers.temporal_memory import (
    TemporalMemoryConsumer,
    TemporalVisualCandidate,
    TemporalVisualMemory,
    TemporalVisualRecord,
)

__all__ = [
    "KeyframeChangeConsumer",
    "TemporalMemoryConsumer",
    "TemporalVisualCandidate",
    "TemporalVisualMemory",
    "TemporalVisualRecord",
]
