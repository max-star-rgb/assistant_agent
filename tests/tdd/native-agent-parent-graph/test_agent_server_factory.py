"""RED/GREEN coverage for the native Agent Server composition factory."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError
import pytest

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.agent_server.graph import native_assistant_graph
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.native_agent.state import AssistantRootInput


class FakeUser(dict):
    identity = "user-1"
    permissions = ("assistant:developer",)


def test_manifest_exposes_only_versioned_native_assistant() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))

    assert manifest["graphs"] == {
        "assistant-native-v1": (
            "assistant_agent.agent_server.graph:native_assistant_graph"
        )
    }


def test_server_context_contains_identity_not_execution_mode() -> None:
    context = AgentServerRunContext.model_validate(
        {
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "entry_profile": "agent_server",
            "media_capabilities": ["video"],
        }
    )

    assert context.media_capabilities == ("video",)
    assert "execution_mode" not in type(context).model_fields
    with pytest.raises(ValidationError):
        AgentServerRunContext.model_validate(
            {
                "user_id": "user-1",
                "tenant_id": "tenant-1",
                "assistant_mode": "standard",
            }
        )


def test_execution_owner_composes_only_native_model_tools_memory_and_graph(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    monkeypatch.delenv("MULTIMODAL_AGENT_MCP_ENABLED", raising=False)
    context = AgentServerRunContext(user_id="user-1", tenant_id="tenant-1")

    owner = asyncio.run(
        AgentServerExecutionOwner.open(
            context=context,
            store=InMemoryStore(),
            user=FakeUser(tenant_id="tenant-1"),
        )
    )
    try:
        assert isinstance(owner.model, BaseChatModel)
        assert owner.tools and all(isinstance(tool, BaseTool) for tool in owner.tools)
        assert owner.memory_backend.backend_id == "disabled"
        assert owner.graph.name == "AssistantRootGraph"
        assert not hasattr(owner, "proactive_delivery_store")
        assert not hasattr(owner, "worker")
        assert not hasattr(owner, "tool_executor")
        assert not hasattr(owner, "product_event_projector")

        result = asyncio.run(
            owner.graph.ainvoke(
                {
                    "messages": [HumanMessage(content="你好")],
                    "execution_mode": "fast",
                },
                context=context,
            )
        )
        assert result["messages"][-1].content == "已收到：你好"
    finally:
        asyncio.run(owner.aclose())


def test_agent_server_composition_does_not_import_legacy_runtime_facades() -> None:
    from assistant_agent.agent_server import graph, services

    source = inspect.getsource(graph) + inspect.getsource(services)
    forbidden = (
        "AgentGraphRuntime",
        "AssistantTurnState",
        "ToolExecutor",
        "ProductEventProjector",
        "WorkflowGraphHost",
    )

    assert all(name not in source for name in forbidden)
    assert native_assistant_graph.__name__ == "native_assistant_graph"


def test_new_public_input_rejects_legacy_request_wrapper() -> None:
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate(
            {
                "request_input": {
                    "turn_origin_id": "old-turn",
                    "text": "legacy",
                }
            }
        )
