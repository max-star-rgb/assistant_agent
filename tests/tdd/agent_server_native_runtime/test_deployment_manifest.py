from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_deployment_manifest_exposes_native_graph_and_authenticated_custom_app() -> None:
    manifest = json.loads((REPO_ROOT / "langgraph.json").read_text(encoding="utf-8"))

    assert manifest == {
        "dependencies": ["."],
        "graphs": {
            "assistant": "assistant_agent.agent_server.graph:assistant_graph",
        },
        "auth": {"path": "assistant_agent.agent_server.auth:auth"},
        "http": {
            "app": "assistant_agent.agent_server.media_app:app",
            "enable_custom_route_auth": True,
        },
    }


def test_run_context_is_strict_json_data_not_runtime_services() -> None:
    from assistant_agent.agent_server.context import AgentServerRunContext

    context = AgentServerRunContext.model_validate(
        {
            "user_id": "user-sentinel",
            "tenant_id": "tenant-sentinel",
            "media_capabilities": ["audio_ref"],
        }
    )

    assert context.model_dump(mode="json") == {
        "user_id": "user-sentinel",
        "tenant_id": "tenant-sentinel",
        "assistant_mode": "standard",
        "entry_profile": "agent_server",
        "media_capabilities": ["audio_ref"],
    }
    with pytest.raises(ValidationError):
        AgentServerRunContext.model_validate(
            {
                "user_id": "user-sentinel",
                "tenant_id": "tenant-sentinel",
                "tool_executor": object(),
            }
        )


def test_custom_app_has_media_compatibility_route_and_health_contract() -> None:
    from assistant_agent.agent_server.media_app import app

    routes = {(getattr(route, "path", None), type(route).__name__) for route in app.routes}

    assert ("/health/agent-server-adapter", "APIRoute") in routes
    assert ("/agent-service/{version}", "APIWebSocketRoute") in routes


def test_manifest_graph_symbol_is_importable_as_a_compiled_graph() -> None:
    from assistant_agent.agent_server.graph import assistant_graph

    node_names = set(assistant_graph.get_graph().nodes)

    assert {"memory_recall", "assistant", "publish_response", "memory_commit"} <= node_names
