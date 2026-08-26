from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_agent.agent_server.auth import (
    authenticate,
    authorize_run_create,
    authorize_thread_create,
    authorize_thread_update,
)
from assistant_agent.agent_server.client import (
    IncompatibleCheckpointGraphError,
    IncompatibleThreadGraphError,
    SdkAgentServerClient,
    require_current_checkpoint_graph,
)
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.agent_server.media_app import _native_graph_warmup_url
from assistant_agent.agent_server.media_protocol import MediaProtocolError, parse_chat, parse_envelope
from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_facts,
    assistant_runtime_metadata,
)


@pytest.mark.core_invariant("GATE-001")
def test_agent_server_owns_the_production_graph_and_authenticated_media_route() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))
    assert manifest["graphs"] == {
        "assistant-native-v3": (
            "assistant_agent.agent_server.graph:native_assistant_graph"
        ),
        "assistant-memory-v1": (
            "assistant_agent.agent_server.graph:native_memory_graph"
        ),
    }
    assert manifest["http"] == {
        "app": "assistant_agent.agent_server.media_app:app",
        "enable_custom_route_auth": True,
    }


@pytest.mark.core_invariant("GATE-001")
def test_current_clients_reject_v1_v2_checkpoint_and_thread_at_graph_id_boundary(
    monkeypatch,
) -> None:
    """Catches normal or replayed legacy state entering the incompatible v3 graph."""

    assert ASSISTANT_GRAPH_ID == "assistant-native-v3"
    monkeypatch.setenv("ASSISTANT_AGENT_SERVER_PORT", "8089")
    assert _native_graph_warmup_url() == (
        "http://127.0.0.1:8089/assistants/assistant-native-v3/graph"
    )
    assert require_current_checkpoint_graph(
        {"metadata": {"graph_id": "assistant-native-v3"}}
    ) == "assistant-native-v3"
    with pytest.raises(IncompatibleCheckpointGraphError):
        require_current_checkpoint_graph(
            {"metadata": {"graph_id": "assistant-native-v1"}}
        )
    with pytest.raises(IncompatibleCheckpointGraphError):
        require_current_checkpoint_graph(
            {"metadata": {"graph_id": "assistant-native-v2"}}
        )

    class Threads:
        async def get(self, _thread_id: str) -> dict[str, Any]:
            return {
                "thread_id": "legacy-thread",
                "metadata": {"assistant_graph_id": "assistant-native-v2"},
            }

    class Runs:
        called = False

        async def stream(self, *_args: Any, **_kwargs: Any):
            self.called = True
            if False:
                yield None

    sdk = SimpleNamespace(threads=Threads(), runs=Runs())
    client = object.__new__(SdkAgentServerClient)
    client._client = sdk

    async def consume() -> None:
        async for _part in client.stream_run(
            thread_id="legacy-thread",
            assistant_id=ASSISTANT_GRAPH_ID,
            input={"messages": [{"role": "user", "content": "hello"}]},
            context={},
            multitask_strategy="enqueue",
            on_run_created=lambda _run_id: None,
        ):
            pass

    with pytest.raises(IncompatibleThreadGraphError):
        asyncio.run(consume())
    assert sdk.runs.called is False


@pytest.mark.core_invariant("GATE-001")
def test_agent_server_auth_binds_studio_threads_and_runs_to_v3() -> None:
    ctx = SimpleNamespace(user=SimpleNamespace(identity="studio-user"))
    created = {"metadata": {}}
    assert asyncio.run(authorize_thread_create(ctx, created)) is None
    assert created["metadata"] == {
        "assistant_graph_id": ASSISTANT_GRAPH_ID,
        "owner": "studio-user",
    }
    legacy_create = {"metadata": {"assistant_graph_id": "assistant-native-v2"}}
    assert asyncio.run(authorize_thread_create(ctx, legacy_create)) is False
    memory_create = {"graph_id": "assistant-memory-v1", "metadata": {}}
    assert asyncio.run(authorize_thread_create(ctx, memory_create)) is None
    assert memory_create["metadata"]["assistant_graph_id"] == "assistant-memory-v1"

    updated = {
        "thread_id": "thread-v3",
        "metadata": {"assistant_graph_id": ASSISTANT_GRAPH_ID, "label": "kept"},
    }
    update_filter = asyncio.run(authorize_thread_update(ctx, updated))
    assert update_filter == {
        "owner": "studio-user",
        "assistant_graph_id": ASSISTANT_GRAPH_ID,
    }
    legacy_update = {
        "thread_id": "thread-v2",
        "metadata": {"assistant_graph_id": "assistant-native-v2"},
    }
    assert asyncio.run(authorize_thread_update(ctx, legacy_update)) is False
    rollback = {"thread_id": "thread-v2", "action": "rollback"}
    assert asyncio.run(authorize_thread_update(ctx, rollback)) == {
        "owner": "studio-user"
    }

    run = {"assistant_id": ASSISTANT_GRAPH_ID, "metadata": {}}
    run_filter = asyncio.run(authorize_run_create(ctx, run))
    assert run_filter == {
        "owner": "studio-user",
        "assistant_graph_id": ASSISTANT_GRAPH_ID,
    }
    memory_run = {"assistant_id": "assistant-memory-v1", "metadata": {}}
    assert asyncio.run(authorize_run_create(ctx, memory_run)) == {
        "owner": "studio-user"
    }


@pytest.mark.core_invariant("GATE-001")
def test_media_connection_only_correlates_native_thread_run_and_delivery_ids() -> None:
    session = MediaConnectionSession(connection_id="connection-1")
    session.bind_control(
        protocol_session_id="vendor-session",
        user_id="user-1",
        thread_id="thread-1",
    )
    session.bind_run(chat_index="chat-1", run_id="run-1")
    session.bind_delivery(delivery_id="delivery-1", chat_index="chat-1")
    assert session.active_run_targets() == (("thread-1", "run-1"),)
    session.acknowledge(delivery_id="delivery-1", chat_index="chat-1")
    assert session.deliveries == {}


@pytest.mark.core_invariant("GATE-001")
def test_media_wire_parser_is_strict_and_does_not_define_graph_lifecycle() -> None:
    envelope = parse_envelope(
        {
            "message": "chat",
            "sessionId": "vendor-session",
            "body": json.dumps(
                {
                    "chatIndex": "chat-1",
                    "userNumber": "user-1",
                    "contents": [
                        {
                            "speakerNumber": "user-1",
                            "time": "1",
                            "speechContent": "hello",
                        }
                    ],
                    "stream": True,
                }
            ),
        }
    )
    assert parse_chat(envelope).text == "hello"
    with pytest.raises(MediaProtocolError):
        parse_envelope({"message": "chat", "body": "not-json"})


@pytest.mark.core_invariant("IDENT-001")
def test_assistant_context_is_public_configuration_not_private_run_facts() -> None:
    context = AssistantRunContext.model_validate(
        {
            "system_prompt": "assistant-persona-sentinel",
            "assistant_execution_mode": "planning",
        }
    )
    assert set(type(context).model_fields) == {
        "system_prompt",
        "assistant_execution_mode",
    }
    assert context.system_prompt == "assistant-persona-sentinel"
    facts = assistant_runtime_facts(
        {
            "metadata": assistant_runtime_metadata(
                AssistantRuntimeFacts(
                    entry_profile="agent_service",
                    visual_capability_token="opaque-capability",
                )
            )
        }
    )
    assert facts.entry_profile == "agent_service"
    assert facts.visual_capability_token == "opaque-capability"


@pytest.mark.core_invariant("IDENT-001")
def test_agent_server_auth_is_tokenless_for_all_provider_modes(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "real")
    monkeypatch.setenv(
        "ASSISTANT_AGENT_SERVER_SERVICE_TOKEN",
        "configured-token-must-be-ignored",
    )

    user = asyncio.run(
        authenticate(
            "Bearer invalid-token",
            {b"x-assistant-user": b"user-sentinel"},
        )
    )

    assert user == {
        "identity": "user-sentinel",
        "permissions": ["assistant:developer"],
        "is_authenticated": True,
    }
