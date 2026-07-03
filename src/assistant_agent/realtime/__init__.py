"""Neutral realtime backend interfaces for assistant_agent."""

from assistant_agent.realtime.agent_graph_backend import AgentGraphRealtimeBackend
from assistant_agent.realtime.backend import RealtimeAgentBackend, RealtimeEventSink
from assistant_agent.realtime.progress import ProgressPolicy, ProgressTracker
from assistant_agent.realtime.types import (
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
    RealtimeBackendCapabilities,
    RealtimeCancelToken,
)

GatewayAgentAdapter = AgentGraphRealtimeBackend
RealtimeAgentAdapter = AgentGraphRealtimeBackend

__all__ = [
    "AgentGraphRealtimeBackend",
    "GatewayAgentAdapter",
    "RealtimeAgentAdapter",
    "RealtimeAgentBackend",
    "RealtimeEventSink",
    "ProgressPolicy",
    "ProgressTracker",
    "RealtimeCancelToken",
    "RealtimeAgentRequest",
    "RealtimeAgentEvent",
    "RealtimeAgentResult",
    "RealtimeBackendCapabilities",
]
