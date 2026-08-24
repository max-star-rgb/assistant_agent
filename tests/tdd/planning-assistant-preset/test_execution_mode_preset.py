"""Temporary RED/GREEN coverage for assistant-scoped execution presets."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from langgraph.runtime import Runtime
from pydantic import ValidationError

from assistant_agent.agent_server import auth as auth_module
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.root_graph import (
    execution_router_node,
    route_execution_mode,
)


def test_planning_assistant_preset_overrides_messages_only_fast_default() -> None:
    state = {"messages": [], "execution_mode": "fast"}

    update = execution_router_node(
        state,
        Runtime(
            context=AssistantRunContext(
                assistant_execution_mode="planning",
            )
        ),
    )

    assert update == {"execution_mode": "planning"}
    assert route_execution_mode({**state, **update}) == "planning"


def test_standard_assistant_keeps_structured_input_mode() -> None:
    state = {"messages": [], "execution_mode": "coding"}

    update = execution_router_node(
        state,
        Runtime(context=AssistantRunContext()),
    )

    assert update == {"execution_mode": "coding"}


def test_unknown_assistant_execution_preset_fails_closed() -> None:
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate(
            {"assistant_execution_mode": "unknown"}
        )


def test_auth_normalizes_only_the_fixed_planning_assistant_definition() -> None:
    authorize_create = getattr(
        auth_module,
        "authorize_planning_assistant_create",
    )
    value = {
        "assistant_id": UUID("4cf38057-6071-50ca-a565-98b7854d763e"),
        "graph_id": "malicious-graph",
        "config": {"configurable": {"assistant_agent_execution_mode": "fast"}},
        "context": {},
        "metadata": {},
        "if_exists": "raise",
        "name": "malicious-name",
    }

    result = asyncio.run(authorize_create(object(), value))

    assert result is None
    assert value["graph_id"] == "assistant-native-v2"
    assert value["name"] == "assistant-native-v2-planning"
    assert value["config"] == {}
    assert value["context"] == {"assistant_execution_mode": "planning"}
    assert value["metadata"] == {
        "assistant_agent_preset": "planning",
        "managed_by": "assistant_agent",
    }


def test_auth_rejects_arbitrary_assistant_creation() -> None:
    authorize_create = getattr(
        auth_module,
        "authorize_planning_assistant_create",
    )
    value = {
        "assistant_id": UUID("00000000-0000-0000-0000-000000000001"),
        "graph_id": "assistant-native-v2",
        "config": {},
        "context": {},
        "metadata": {},
        "if_exists": "raise",
        "name": "arbitrary-assistant",
    }

    assert asyncio.run(authorize_create(object(), value)) is False


def test_auth_normalizes_updates_to_the_fixed_planning_assistant() -> None:
    authorize_update = getattr(
        auth_module,
        "authorize_planning_assistant_update",
    )
    value = {
        "assistant_id": UUID("4cf38057-6071-50ca-a565-98b7854d763e"),
        "graph_id": "malicious-graph",
        "config": {"configurable": {"untrusted": True}},
        "context": {"assistant_execution_mode": "unknown"},
        "metadata": {},
        "name": "malicious-name",
        "version": None,
    }

    result = asyncio.run(authorize_update(object(), value))

    assert result is None
    assert value["graph_id"] == "assistant-native-v2"
    assert value["name"] == "assistant-native-v2-planning"
    assert value["config"] == {}
    assert value["context"] == {"assistant_execution_mode": "planning"}
