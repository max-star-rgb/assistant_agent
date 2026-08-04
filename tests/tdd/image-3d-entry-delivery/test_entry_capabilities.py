from assistant_agent.gateway import capabilities


def test_only_agent_service_declares_generated_media_delivery() -> None:
    assert hasattr(capabilities, "HTTP_AGENT_ENTRY_CAPABILITIES")
    assert capabilities.AGENT_SERVICE_ENTRY_CAPABILITIES.to_metadata()[
        "supports_generated_media_delivery"
    ] is True
    assert capabilities.GATEWAY_WEBSOCKET_CAPABILITIES.to_metadata()[
        "supports_generated_media_delivery"
    ] is False
    assert capabilities.HTTP_AGENT_ENTRY_CAPABILITIES.to_metadata()[
        "supports_generated_media_delivery"
    ] is False


def test_http_entry_overwrites_untrusted_generated_media_capability() -> None:
    from assistant_agent.api.routes_agent import _gateway_http_metadata
    from assistant_agent.runtime.requests import UserRequest

    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="生成3D",
        metadata={
            "gateway": {
                "entry_capabilities": {
                    "supports_generated_media_delivery": True,
                }
            }
        },
    )

    metadata = _gateway_http_metadata(request, "capture-sentinel")

    assert metadata["gateway"]["entry_capabilities"][
        "supports_generated_media_delivery"
    ] is False
