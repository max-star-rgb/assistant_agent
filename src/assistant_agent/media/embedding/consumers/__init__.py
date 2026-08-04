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
from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemoryMatch,
    VisualMemorySearchRequest,
    VisualMemorySearchResult,
    VisualMemorySearchService,
)

__all__ = [
    "KeyframeChangeConsumer",
    "CrossModalAlignment",
    "CrossModalAlignmentConsumer",
    "VisualAttentionCandidate",
    "VisualAttentionConsumer",
    "VisualMemoryMatch",
    "VisualMemorySearchRequest",
    "VisualMemorySearchResult",
    "VisualMemorySearchService",
]
