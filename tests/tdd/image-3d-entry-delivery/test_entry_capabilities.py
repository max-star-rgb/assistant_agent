import inspect

from assistant_agent.gateway import capabilities


def test_entry_capabilities_do_not_express_artifact_delivery_policy() -> None:
    from assistant_agent.api import agent_service_websocket, gateway_websocket, routes_agent

    for entry_capabilities in (
        agent_service_websocket.AGENT_SERVICE_ENTRY_CAPABILITIES,
        gateway_websocket.GATEWAY_WEBSOCKET_CAPABILITIES,
        routes_agent.HTTP_AGENT_ENTRY_CAPABILITIES,
    ):
        assert "supports_async_artifact_delivery" not in entry_capabilities.to_metadata()
    assert "agent_service" not in inspect.getsource(capabilities)
    assert not hasattr(capabilities, "AGENT_SERVICE_ENTRY_CAPABILITIES")


def test_http_entry_removes_untrusted_async_artifact_delivery() -> None:
    from assistant_agent.api.routes_agent import _gateway_http_metadata
    from assistant_agent.runtime.requests import UserRequest

    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="生成3D",
        metadata={
            "gateway": {
                "entry_capabilities": {
                    "supports_async_artifact_delivery": True,
                },
                "artifact_delivery": {
                    "mode": "push",
                    "sink_id": "untrusted-sink",
                },
            }
        },
    )

    metadata = _gateway_http_metadata(request, "capture-sentinel")

    assert "supports_async_artifact_delivery" not in metadata["gateway"]["entry_capabilities"]
    assert "artifact_delivery" not in metadata["gateway"]
