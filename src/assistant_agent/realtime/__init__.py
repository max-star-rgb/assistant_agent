"""Neutral realtime backend interfaces for assistant_agent."""

from assistant_agent.realtime.agent_graph_backend import AgentGraphRealtimeBackend
from assistant_agent.realtime.audio_edge import gateway_frame_to_tts_event
from assistant_agent.realtime.backend import RealtimeAgentBackend, RealtimeEventSink
from assistant_agent.schemas.realtime_cancellation import (
    RealtimeTurnCancellationContract,
    build_realtime_turn_cancellation_metadata,
    realtime_turn_cancellation_from_metadata,
)
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
    "RealtimeTurnCancellationContract",
    "build_realtime_turn_cancellation_metadata",
    "realtime_turn_cancellation_from_metadata",
    "gateway_frame_to_tts_event",
]
