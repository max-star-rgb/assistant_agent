import ast
from pathlib import Path


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _function_source(path: str, function_name: str) -> str:
    source = _source(path)
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"function {function_name} not found in {path}")


def test_http_agent_run_remains_gateway_first_product_entry() -> None:
    source = _function_source("src/assistant_agent/api/routes_agent.py", "run_agent")

    assert "_run_agent_through_gateway(request)" in source
    assert "GatewayTurnFacade" not in source
    assert "get_assistant_runtime_app().run_request" not in source
    assert "AgentGraphRuntime" not in source


def test_gateway_runtime_is_the_only_http_gateway_backend_capture_boundary() -> None:
    source = _source("src/assistant_agent/api/gateway_runtime.py")

    assert "GatewayAgentAdapter(run_request=_run_assistant_request_with_http_runtime)" in source
    assert "get_assistant_runtime_app().run_request(request, **kwargs)" in source
    assert "GATEWAY_HTTP_RESPONSE_CAPTURE_ID" in source
    assert "AgentGraphRuntime" not in source


def test_gateway_and_realtime_media_websockets_bridge_to_gateway_only() -> None:
    source = _source("src/assistant_agent/api/gateway_websocket.py")

    assert '@router.websocket("/ws/gateway")' in source
    assert '@router.websocket("/ws/realtime/media")' in source
    assert "get_gateway_bridge().bridge(" in source
    assert "get_assistant_runtime_app" not in source
    assert "run_assistant_request" not in source
    assert "AgentGraphRuntime" not in source


def test_local_cli_text_path_remains_gateway_first() -> None:
    source = _source("scripts/run_assistant_cli.py")

    assert "GatewaySessionManager(" in source
    assert "GatewayTurnFacade(manager=manager)" in source
    assert "GatewayAgentAdapter(" in source
    assert "run_demo_flows(scenario_id=args.scenario)" in source
    assert "AgentGraphRuntime(" not in source


def test_legacy_ws_agent_is_gateway_first_internally_but_keeps_legacy_event_surface() -> None:
    source = _source("src/assistant_agent/api/websocket.py")

    assert '@router.websocket("/ws/agent/{session_id}")' in source
    assert "GatewaySessionManager(" in source
    assert "GatewayAgentAdapter(run_request=run_request)" in source
    assert "GatewayTurnFacade(manager=manager)" in source
    assert "facade.run_turn(" in source
    assert "MirroringWebSocketEventSink(" in source
    assert "get_assistant_runtime_app().run_request(gateway_request, **kwargs)" in source
    assert "AgentGraphRuntime" not in source


def test_vendor_agent_service_v1_is_gateway_first_internally_but_keeps_vendor_surface() -> None:
    source = _source("src/assistant_agent/api/agent_service_websocket.py")

    assert '@router.websocket("/agent-service/{version}")' in source
    assert 'message_type = "assistantControlStart"' in source
    assert 'response_message = "assistantControlStartAck"' in source
    assert 'message_type = "chat"' in source
    assert 'response_message = "chatResponse"' in source
    assert "GatewaySessionManager(" in source
    assert "GatewayTurnFacade(manager=gateway_manager)" in source
    assert "GatewayAgentAdapter(" in source
    assert "state.gateway_facade.run_turn(" in source
    assert "get_assistant_runtime_app().run_request(request, **kwargs)" in source
    assert "AgentGraphRuntime" not in source


def test_agents_run_stays_explicit_agent_router_entry_not_default_gateway_path() -> None:
    source = _function_source("src/assistant_agent/api/routes_agent.py", "run_agents")

    assert "get_agent_router().run(request)" in source
    assert "_run_agent_through_gateway" not in source
    assert "GatewayTurnFacade" not in source
    assert "get_assistant_runtime_app().run_request" not in source


def test_demo_scenarios_are_gateway_first_offline_adapter() -> None:
    source = _source("scripts/run_demo_flows.py")

    assert "GatewaySessionManager(" in source
    assert "GatewayAgentAdapter(" in source
    assert "GatewayTurnFacade(manager=manager)" in source
    assert "_gateway_request_from_scenario(scenario)" in source
    assert "facade.run_turn(" in source
    assert "AgentGraphRuntime(" not in source
