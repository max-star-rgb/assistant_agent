"""Gateway protocol, bridge, and session services for assistant_agent."""

from assistant_agent.gateway.bridge import GatewayBridge
from assistant_agent.gateway.capabilities import (
    AGENT_SERVICE_ENTRY_CAPABILITIES,
    GATEWAY_WEBSOCKET_CAPABILITIES,
    REALTIME_MEDIA_ENTRY_CAPABILITIES,
    EntryAdapterCapabilities,
)
from assistant_agent.gateway.observability import (
    GatewayLifecycleEvent,
    GatewayLifecycleSink,
    emit_gateway_lifecycle_event,
)
from assistant_agent.gateway.protocol import (
    CALL_HANGUP,
    CALL_HANGUP_ACK,
    CALL_INCOMING,
    CALL_READY,
    CONFIG_UPDATE,
    RUN_QUEUED,
    Frame,
    RunEndReason,
    frame,
)
from assistant_agent.gateway.queueing import (
    AdmissionSnapshot,
    GatewayQueuePolicy,
    GatewayRunAdmissionController,
    QueueOverflowError,
)
from assistant_agent.gateway.session import (
    ActiveRun,
    CancelToken,
    GatewayConfigUpdateResult,
    GatewaySessionHandle,
    GatewaySessionManager,
    GatewaySessionService,
)
from assistant_agent.gateway.transport import Closed, Endpoint, InMemoryDuplex
from assistant_agent.gateway.turn_arbitration import (
    GatewayTurnArbitrationController,
    GatewayTurnArbitrationOutcome,
    GatewayTurnArbitrationPolicy,
)
from assistant_agent.gateway.ws import WsEndpoint, dumps_frame, loads_frame

__all__ = [
    "CALL_HANGUP",
    "CALL_HANGUP_ACK",
    "CALL_INCOMING",
    "CALL_READY",
    "CONFIG_UPDATE",
    "RUN_QUEUED",
    "AdmissionSnapshot",
    "ActiveRun",
    "AGENT_SERVICE_ENTRY_CAPABILITIES",
    "CancelToken",
    "Closed",
    "Endpoint",
    "EntryAdapterCapabilities",
    "Frame",
    "GATEWAY_WEBSOCKET_CAPABILITIES",
    "GatewayBridge",
    "GatewayConfigUpdateResult",
    "GatewayLifecycleEvent",
    "GatewayLifecycleSink",
    "GatewayQueuePolicy",
    "GatewayRunAdmissionController",
    "GatewaySessionHandle",
    "GatewaySessionManager",
    "GatewaySessionService",
    "GatewayTurnArbitrationController",
    "GatewayTurnArbitrationOutcome",
    "GatewayTurnArbitrationPolicy",
    "InMemoryDuplex",
    "REALTIME_MEDIA_ENTRY_CAPABILITIES",
    "QueueOverflowError",
    "RunEndReason",
    "WsEndpoint",
    "dumps_frame",
    "emit_gateway_lifecycle_event",
    "frame",
    "loads_frame",
]
