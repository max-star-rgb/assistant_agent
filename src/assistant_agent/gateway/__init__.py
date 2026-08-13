"""Legacy wire-projection helpers retained outside the production runtime."""

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
    "Closed": "transport",
    "Endpoint": "transport",
    "EntryAdapterCapabilities": "capabilities",
    "Frame": "protocol",
    "GatewayLifecycleEvent": "observability",
    "GatewayLifecycleSink": "observability",
    "GatewayTurnArbitrationController": "turn_arbitration",
    "GatewayTurnArbitrationOutcome": "turn_arbitration",
    "GatewayTurnArbitrationPolicy": "turn_arbitration",
    "InMemoryDuplex": "transport",
    "RunEndReason": "protocol",
    "WsEndpoint": "ws",
    "dumps_frame": "ws",
    "emit_gateway_lifecycle_event": "observability",
    "frame": "protocol",
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
