from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from assistant_agent.agent_server import auth as auth_module
from assistant_agent.agent_server.auth import (
    allow_assistant_read,
    allow_assistant_search,
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
from assistant_agent.agent_server.config import (
    ASSISTANT_GRAPH_ID,
    MEMORY_GRAPH_ID,
    WORKER_GRAPH_ID,
)
from assistant_agent.agent_server.media_app import _native_graph_warmup_url
from assistant_agent.agent_server.media_protocol import (
    MediaProtocolError,
    parse_chat,
    parse_envelope,
)
from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_facts,
    assistant_runtime_metadata,
)


LEGACY_GRAPH_IDS = (
    "assistant-native-v1",
    "assistant-native-v2",
    "assistant-native-v3",
    "assistant-worker-v1",
)
SYSTEM_ASSISTANT_IDS = {
    ASSISTANT_GRAPH_ID: UUID("8d030b92-89be-5d58-918d-ff35e996429a"),
    WORKER_GRAPH_ID: UUID("ad895394-eb31-5aa1-a5ac-d24c4050ca05"),
    MEMORY_GRAPH_ID: UUID("b209df74-50ea-53ce-89ad-cc13d3c44e1b"),
}
LEGACY_ASSISTANT_IDS = (
    UUID("5d65b3ea-e849-5e47-afde-ed71e133b9da"),
    UUID("46ed656d-0f2d-5320-a380-0bea189fc304"),
    UUID("845db169-0dc1-5167-9e6c-f5b5f0aaf844"),
    UUID("0e81d29f-8729-5318-a864-e4334f8dd8b3"),
)


def _authenticated_context(*, internal_worker: bool = False) -> SimpleNamespace:
    headers = {"X-Assistant-User": "studio-user"}
    if internal_worker:
        headers = auth_module._internal_worker_headers("studio-user")
    user = asyncio.run(
        authenticate(
            None,
            {
                key.lower().encode(): value.encode()
                for key, value in headers.items()
            },
        )
    )
    return SimpleNamespace(user=SimpleNamespace(**user))


@pytest.mark.core_invariant("GATE-001")
def test_agent_server_registers_only_current_graph_identities() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))

    assert manifest["graphs"] == {
        "assistant-native-v4": (
            "assistant_agent.agent_server.graph:native_assistant_graph"
        ),
        "assistant-worker-v2": (
            "assistant_agent.agent_server.graph:native_worker_graph"
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
def test_custom_assistant_reads_and_searches_are_owner_scoped(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URI", "postgres://localhost/test")
    ctx = _authenticated_context()

    for assistant_id in SYSTEM_ASSISTANT_IDS.values():
        assert asyncio.run(
            allow_assistant_read(ctx, {"assistant_id": assistant_id})
        ) is True

    custom_assistant_id = UUID("11111111-1111-4111-8111-111111111111")
    assert asyncio.run(
        allow_assistant_read(ctx, {"assistant_id": custom_assistant_id})
    ) == {"owner": "studio-user"}
    assert asyncio.run(
        allow_assistant_search(
            ctx,
            {"graph_id": None, "metadata": {}, "limit": 10, "offset": 0},
        )
    ) == {"owner": "studio-user"}


@pytest.mark.core_invariant("GATE-001")
def test_current_clients_reject_legacy_and_unknown_checkpoint_and_thread_graphs(
    monkeypatch,
) -> None:
    assert ASSISTANT_GRAPH_ID == "assistant-native-v4"
    assert WORKER_GRAPH_ID == "assistant-worker-v2"
    assert MEMORY_GRAPH_ID == "assistant-memory-v1"
    monkeypatch.setenv("ASSISTANT_AGENT_SERVER_PORT", "8089")
    assert _native_graph_warmup_url() == (
        "http://127.0.0.1:8089/assistants/assistant-native-v4/graph"
    )
    assert (
        require_current_checkpoint_graph({"metadata": {"graph_id": ASSISTANT_GRAPH_ID}})
        == ASSISTANT_GRAPH_ID
    )
    for graph_id in (*LEGACY_GRAPH_IDS, None):
        checkpoint = {"metadata": {"graph_id": graph_id}} if graph_id else {}
        with pytest.raises(IncompatibleCheckpointGraphError):
            require_current_checkpoint_graph(checkpoint)

    class Threads:
        def __init__(self, graph_id: str | None) -> None:
            self.graph_id = graph_id

        async def get(self, _thread_id: str) -> dict[str, Any]:
            return {
                "thread_id": "legacy-thread",
                "metadata": {"graph_id": self.graph_id},
            }

    class Runs:
        called = False

        async def stream(self, *_args: Any, **_kwargs: Any):
            self.called = True
            if False:
                yield None

    for graph_id in (*LEGACY_GRAPH_IDS, None):
        sdk = SimpleNamespace(threads=Threads(graph_id), runs=Runs())
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
def test_sdk_client_uses_only_native_thread_graph_identity() -> None:
    class Threads:
        def __init__(self) -> None:
            self.request: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> dict[str, Any]:
            self.request = kwargs
            return {
                "thread_id": "thread-sentinel",
                "metadata": {"graph_id": kwargs["graph_id"]},
            }

    threads = Threads()
    client = object.__new__(SdkAgentServerClient)
    client._client = SimpleNamespace(threads=threads)

    thread_id = asyncio.run(
        client.create_thread(
            metadata={"label": "kept"},
            graph_id=ASSISTANT_GRAPH_ID,
        )
    )

    assert thread_id == "thread-sentinel"
    assert threads.request == {
        "metadata": {"label": "kept"},
        "thread_id": None,
        "if_exists": None,
        "graph_id": ASSISTANT_GRAPH_ID,
    }


@pytest.mark.core_invariant("GATE-001")
def test_agent_server_auth_accepts_only_current_graph_identities(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URI", "postgres://localhost/test")
    ctx = SimpleNamespace(
        user=SimpleNamespace(identity="studio-user", permissions=())
    )
    created = {"metadata": {}}
    assert asyncio.run(authorize_thread_create(ctx, created)) is None
    assert created["metadata"] == {
        "graph_id": ASSISTANT_GRAPH_ID,
        "owner": "studio-user",
    }
    for graph_id in (*LEGACY_GRAPH_IDS, "unknown-graph"):
        value = {"metadata": {"graph_id": graph_id}}
        assert asyncio.run(authorize_thread_create(ctx, value)) is False

    memory_create = {"metadata": {"graph_id": MEMORY_GRAPH_ID}}
    assert asyncio.run(authorize_thread_create(ctx, memory_create)) is None
    assert memory_create["metadata"]["graph_id"] == MEMORY_GRAPH_ID
    worker_metadata = assistant_runtime_metadata(
        AssistantRuntimeFacts(
            entry_profile="async_worker",
            repository_snapshot_sha="a" * 40,
        )
    )
    worker_create = {
        "metadata": {**worker_metadata, "graph_id": WORKER_GRAPH_ID},
    }
    assert asyncio.run(authorize_thread_create(ctx, worker_create)) is False
    internal_ctx = _authenticated_context(internal_worker=True)
    worker_create = {
        "metadata": {**worker_metadata, "graph_id": WORKER_GRAPH_ID},
    }
    assert asyncio.run(authorize_thread_create(internal_ctx, worker_create)) is None
    assert worker_create["metadata"]["graph_id"] == WORKER_GRAPH_ID

    updated = {
        "thread_id": "thread-v4",
        "metadata": {"graph_id": ASSISTANT_GRAPH_ID, "label": "kept"},
    }
    assert asyncio.run(authorize_thread_update(ctx, updated)) is False
    for state_update in (
        {"thread_id": "thread-v4"},
        {"thread_id": "thread-v4", "metadata": None},
        {"thread_id": "thread-v4", "metadata": {"label": "kept"}},
    ):
        assert asyncio.run(authorize_thread_update(ctx, state_update)) == {
            "owner": "studio-user"
        }
    for graph_id in (*LEGACY_GRAPH_IDS, "unknown-graph"):
        legacy_update = {
            "thread_id": "legacy-thread",
            "metadata": {"graph_id": graph_id},
        }
        assert asyncio.run(authorize_thread_update(ctx, legacy_update)) is False
    for action in ("interrupt", "rollback"):
        action_update = {"thread_id": "legacy-thread", "action": action}
        assert asyncio.run(authorize_thread_update(ctx, action_update)) == {
            "owner": "studio-user"
        }

    run = {"assistant_id": SYSTEM_ASSISTANT_IDS[ASSISTANT_GRAPH_ID], "metadata": {}}
    assert asyncio.run(authorize_run_create(ctx, run)) == {
        "owner": "studio-user",
        "graph_id": ASSISTANT_GRAPH_ID,
    }
    assert run["context"] == {}
    memory_run = {
        "assistant_id": SYSTEM_ASSISTANT_IDS[MEMORY_GRAPH_ID],
        "metadata": {},
    }
    assert asyncio.run(authorize_run_create(ctx, memory_run)) == {
        "owner": "studio-user",
        "graph_id": MEMORY_GRAPH_ID,
    }
    worker_run = {
        "assistant_id": SYSTEM_ASSISTANT_IDS[WORKER_GRAPH_ID],
        "metadata": dict(worker_metadata),
    }
    assert asyncio.run(authorize_run_create(ctx, worker_run)) is False
    worker_run = {
        "assistant_id": SYSTEM_ASSISTANT_IDS[WORKER_GRAPH_ID],
        "metadata": dict(worker_metadata),
    }
    assert asyncio.run(authorize_run_create(internal_ctx, worker_run)) == {
        "owner": "studio-user",
        "graph_id": WORKER_GRAPH_ID,
    }
    injected_snapshot = {
        ASSISTANT_RUNTIME_METADATA_KEY: {
            "entry_profile": "async_worker",
            "repository_snapshot_sha": "a" * 40,
        }
    }
    assert (
        asyncio.run(
            authorize_thread_create(
                ctx,
                {
                    "metadata": {
                        **injected_snapshot,
                        "graph_id": ASSISTANT_GRAPH_ID,
                    },
                },
            )
        )
        is False
    )
    assert (
        asyncio.run(
            authorize_run_create(
                ctx,
                {
                    "assistant_id": SYSTEM_ASSISTANT_IDS[MEMORY_GRAPH_ID],
                    "metadata": dict(injected_snapshot),
                },
            )
        )
        is False
    )
    custom_run = {
        "assistant_id": UUID("123e4567-e89b-12d3-a456-426614174000"),
        "metadata": {},
    }
    assert asyncio.run(authorize_run_create(ctx, custom_run)) == {
        "owner": "studio-user",
        "graph_id": ASSISTANT_GRAPH_ID,
    }
    for assistant_id in LEGACY_ASSISTANT_IDS:
        assert (
            asyncio.run(
                authorize_run_create(
                    ctx,
                    {"assistant_id": assistant_id, "metadata": {}},
                )
            )
            is False
        )


@pytest.mark.core_invariant("GATE-001")
@pytest.mark.core_invariant("IDENT-001")
@pytest.mark.parametrize(
    ("graph_id", "internal_worker"),
    [
        (ASSISTANT_GRAPH_ID, False),
        (MEMORY_GRAPH_ID, False),
        (WORKER_GRAPH_ID, False),
        (WORKER_GRAPH_ID, True),
    ],
)
def test_thread_update_rejects_changes_to_server_issued_runtime_facts(
    graph_id: str,
    internal_worker: bool,
) -> None:
    update = {
        "thread_id": "existing-thread",
        "metadata": {
            "graph_id": graph_id,
            ASSISTANT_RUNTIME_METADATA_KEY: {
                "entry_profile": "async_worker",
                "repository_snapshot_sha": "a" * 40,
            },
        },
    }
    ctx = _authenticated_context(internal_worker=internal_worker)

    assert asyncio.run(authorize_thread_update(ctx, update)) is False


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


def _chat_body(**extra: object) -> dict[str, object]:
    return {
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
        **extra,
    }


@pytest.mark.core_invariant("GATE-001")
def test_media_wire_parser_rejects_removed_assistant_mode() -> None:
    envelope = parse_envelope(
        {
            "message": "chat",
            "sessionId": "vendor-session",
            "body": json.dumps(_chat_body()),
        }
    )
    assert parse_chat(envelope).text == "hello"
    legacy = parse_envelope(
        {"message": "chat", "body": json.dumps(_chat_body(assistantMode="planning"))}
    )
    with pytest.raises(MediaProtocolError):
        parse_chat(legacy)
    with pytest.raises(MediaProtocolError):
        parse_envelope({"message": "chat", "body": "not-json"})


@pytest.mark.core_invariant("IDENT-001")
def test_assistant_context_is_public_configuration_not_private_run_facts() -> None:
    context = AssistantRunContext.model_validate({"enable_memory": False})
    assert set(type(context).model_fields) == {"enable_memory"}
    assert context.enable_memory is False
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate({"execution_mode": "planning"})

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
    assert facts.repository_snapshot_sha is None
    with pytest.raises(ValidationError):
        AssistantRuntimeFacts(
            entry_profile="agent_service",
            repository_snapshot_sha="a" * 40,
        )


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
