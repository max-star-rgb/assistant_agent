"""Lazy public exports for Gateway protocol, bridge, and session services."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_MODULES = {
    "CALL_HANGUP": "protocol",
    "CALL_HANGUP_ACK": "protocol",
    "CALL_INCOMING": "protocol",
    "CALL_READY": "protocol",
    "CONFIG_UPDATE": "protocol",
    "RUN_QUEUED": "protocol",
    "AdmissionSnapshot": "queueing",
    "ActiveRun": "session",
    "AGENT_SERVICE_ENTRY_CAPABILITIES": "capabilities",
    "CancelToken": "session",
    "Closed": "transport",
    "Endpoint": "transport",
    "EntryAdapterCapabilities": "capabilities",
    "Frame": "protocol",
    "GATEWAY_WEBSOCKET_CAPABILITIES": "capabilities",
    "HTTP_AGENT_ENTRY_CAPABILITIES": "capabilities",
    "GatewayBridge": "bridge",
    "GatewayConnectionPolicy": "bridge",
    "GatewayConfigUpdateResult": "session",
    "GatewayLifecycleEvent": "observability",
    "GatewayLifecycleSink": "observability",
    "GatewayQueuePolicy": "queueing",
    "GatewayRunAdmissionController": "queueing",
    "GatewayRuntimePool": "runtime_pool",
    "GatewaySessionHandle": "session",
    "GatewaySessionManager": "session",
    "GatewaySessionService": "session",
    "GatewayTurnArbitrationController": "turn_arbitration",
    "GatewayTurnArbitrationOutcome": "turn_arbitration",
    "GatewayTurnArbitrationPolicy": "turn_arbitration",
    "InMemoryDuplex": "transport",
    "QueueOverflowError": "queueing",
    "RunEndReason": "protocol",
    "WsEndpoint": "ws",
    "dumps_frame": "ws",
    "emit_gateway_lifecycle_event": "observability",
    "frame": "protocol",
    "shared_gateway_runtime_factory": "runtime_pool",
    "loads_frame": "ws",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(
        import_module(f"assistant_agent.gateway.{module_name}"),
        name,
    )
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
