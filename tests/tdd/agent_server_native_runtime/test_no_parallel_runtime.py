from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_production_entrypoints_do_not_own_a_parallel_graph_runtime() -> None:
    inspected = [
        REPO_ROOT / "scripts" / "run_server.py",
        REPO_ROOT / "scripts" / "agent_cli.py",
        REPO_ROOT / "src" / "assistant_agent" / "agent_server" / "media_app.py",
    ]
    forbidden = {
        "AgentGraphRuntime",
        "GatewaySessionManager",
        "GatewayRuntimePool",
        "GatewayRuntimeAdapter",
        "GatewayTurnFacade",
    }
    for path in inspected:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert names.isdisjoint(forbidden), (path, names & forbidden)


def test_parallel_gateway_runtime_modules_are_deleted() -> None:
    for relative in (
        "src/assistant_agent/api/gateway_runtime.py",
        "src/assistant_agent/api/gateway_websocket.py",
        "src/assistant_agent/api/agent_service_websocket.py",
        "src/assistant_agent/gateway/runtime_pool.py",
        "src/assistant_agent/gateway/runtime_adapter.py",
        "src/assistant_agent/gateway/runtime_backend.py",
        "src/assistant_agent/gateway/turn_facade.py",
        "src/assistant_agent/gateway/session.py",
        "src/assistant_agent/gateway/queueing.py",
        "src/assistant_agent/gateway/bridge.py",
    ):
        assert not (REPO_ROOT / relative).exists(), relative


def test_langgraph_manifest_is_the_only_production_graph_server() -> None:
    run_server = (REPO_ROOT / "scripts" / "run_server.py").read_text(encoding="utf-8")
    assert "langgraph" in run_server
    assert "assistant_agent.api.app" not in run_server
