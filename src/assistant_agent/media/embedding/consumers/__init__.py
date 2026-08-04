"""Built-in consumers of canonical embedding outcomes."""

from assistant_agent.media.embedding.consumers.keyframe import KeyframeChangeConsumer
from assistant_agent.media.embedding.consumers.alignment import (
    CrossModalAlignment,
    CrossModalAlignmentConsumer,
)
from assistant_agent.media.embedding.consumers.attention import (
    VisualAttentionCandidate,
    VisualAttentionConsumer,
)
from assistant_agent.media.embedding.consumers.temporal_memory import (
    TemporalMemoryConsumer,
    TemporalVisualCandidate,
    TemporalVisualMemory,
    TemporalVisualRecord,
)

__all__ = [
    "KeyframeChangeConsumer",
    "CrossModalAlignment",
    "CrossModalAlignmentConsumer",
    "TemporalMemoryConsumer",
    "TemporalVisualCandidate",
    "TemporalVisualMemory",
    "TemporalVisualRecord",
    "VisualAttentionCandidate",
    "VisualAttentionConsumer",
]
