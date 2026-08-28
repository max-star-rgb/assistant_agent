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
    authenticate,
    authorize_run_create,
    authorize_thread_create,
)
from assistant_agent.agent_server.config import (
    ASSISTANT_GRAPH_ID,
    MEMORY_GRAPH_ID,
    WORKER_GRAPH_ID,
)
from assistant_agent.agent_server.media_app import _NativeAssistantTextStream, app
from assistant_agent.agent_server.media_protocol import (
    MediaEnvelope,
    MediaProtocolError,
    parse_chat,
)
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRunContext,
    AssistantRuntimeFacts,
    assistant_runtime_metadata,
)
from scripts.media_simulator import chat_body


WORKER_ASSISTANT_ID = UUID("ad895394-eb31-5aa1-a5ac-d24c4050ca05")
MAIN_ASSISTANT_ID = UUID("8d030b92-89be-5d58-918d-ff35e996429a")
MEMORY_ASSISTANT_ID = UUID("b209df74-50ea-53ce-89ad-cc13d3c44e1b")
LEGACY_SYSTEM_ASSISTANT_IDS = (
    UUID("5d65b3ea-e849-5e47-afde-ed71e133b9da"),
    UUID("46ed656d-0f2d-5320-a380-0bea189fc304"),
    UUID("845db169-0dc1-5167-9e6c-f5b5f0aaf844"),
    UUID("0e81d29f-8729-5318-a864-e4334f8dd8b3"),
)


def _chat_envelope() -> MediaEnvelope:
    return MediaEnvelope(
        message="chat",
        session_id="session-1",
        body={
            "chatIndex": "chat-1",
            "userNumber": "u",
            "contents": [
                {
                    "speakerNumber": "s",
                    "time": "1",
                    "speechContent": "hello",
                }
            ],
            "stream": True,
        },
    )


def _auth_context(*, internal_worker: bool = False) -> SimpleNamespace:
    headers = {"X-Assistant-User": "worker-user"}
    if internal_worker:
        headers = auth_module._internal_worker_headers("worker-user")
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


def test_graph_ids_and_public_context_are_v4_only() -> None:
    assert ASSISTANT_GRAPH_ID == "assistant-native-v4"
    assert WORKER_GRAPH_ID == "assistant-worker-v2"
    assert MEMORY_GRAPH_ID == "assistant-memory-v1"
    config = json.loads(
        (Path(__file__).resolve().parents[3] / "langgraph.json").read_text()
    )
    assert set(config["graphs"]) == {
        "assistant-native-v4",
        "assistant-worker-v2",
        "assistant-memory-v1",
    }
    assert set(AssistantRunContext.model_fields) == {"enable_memory"}
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate({"execution_mode": "fast"})


def test_media_rejects_removed_assistant_mode() -> None:
    envelope = _chat_envelope()
    envelope.body["assistantMode"] = "planning"
    with pytest.raises(MediaProtocolError, match="assistantMode is not supported"):
        parse_chat(envelope)


def test_simulator_chat_body_has_no_assistant_mode() -> None:
    assert "assistantMode" not in chat_body(
        text="hello",
        chat_index="1",
        user_number="u",
        speaker_number="s",
        stream=True,
    )


def test_media_stream_keeps_main_model_and_hides_worker_model() -> None:
    stream = _NativeAssistantTextStream()
    stream._record_metadata(
        {
            "main-message": {
                "metadata": {
                    "langgraph_node": "model",
                    "lc_agent_name": "AssistantAgent",
                }
            },
            "worker-message": {
                "metadata": {
                    "langgraph_node": "model",
                    "lc_agent_name": "general-purpose",
                }
            },
            "read-only-worker-message": {
                "metadata": {
                    "langgraph_node": "model",
                    "lc_agent_name": "AssistantReadOnlyWorker",
                }
            },
            "top-level-message": {"metadata": {"langgraph_node": "model"}},
        }
    )

    assert stream.message_nodes == {
        "main-message": "model",
        "worker-message": "__internal_subgraph__",
        "read-only-worker-message": "__internal_subgraph__",
        "top-level-message": "model",
    }


def test_obsolete_coding_attestation_route_is_removed() -> None:
    assert "/internal/evaluation/coding-attestation" not in {
        route.path for route in app.routes
    }


@pytest.mark.parametrize(
    ("metadata", "case"),
    [
        ({}, "missing namespace"),
        ({ASSISTANT_RUNTIME_METADATA_KEY: {}}, "empty payload"),
        ({ASSISTANT_RUNTIME_METADATA_KEY: None}, "null payload"),
        ({ASSISTANT_RUNTIME_METADATA_KEY: False}, "false payload"),
        (
            {ASSISTANT_RUNTIME_METADATA_KEY: {"entry_profile": "async_worker"}},
            "missing sha",
        ),
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "",
                }
            },
            "empty sha",
        ),
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "a" * 41,
                }
            },
            "41-char sha",
        ),
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "a" * 63,
                }
            },
            "63-char sha",
        ),
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "cli",
                    "repository_snapshot_sha": "a" * 40,
                }
            },
            "wrong profile",
        ),
        ({ASSISTANT_RUNTIME_METADATA_KEY: "malformed"}, "malformed payload"),
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "a" * 40,
                    "unexpected": True,
                }
            },
            "extra field",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
@pytest.mark.parametrize("operation", ["thread", "run"])
def test_worker_authorization_rejects_invalid_runtime_facts(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    metadata: dict[str, Any],
    case: str,
) -> None:
    del case
    monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URI", "postgres://localhost/test")
    value = {"metadata": dict(metadata)}
    if operation == "thread":
        value["metadata"]["graph_id"] = WORKER_GRAPH_ID
    else:
        value["assistant_id"] = WORKER_ASSISTANT_ID
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    assert asyncio.run(authorize(_auth_context(internal_worker=True), value)) is False


@pytest.mark.parametrize("snapshot_length", [40, 64])
@pytest.mark.parametrize("operation", ["thread", "run"])
def test_worker_authorization_rejects_shape_only_external_caller(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    snapshot_length: int,
) -> None:
    monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URI", "postgres://localhost/test")
    metadata = assistant_runtime_metadata(
        AssistantRuntimeFacts(
            entry_profile="async_worker",
            repository_snapshot_sha="a" * snapshot_length,
        )
    )
    value = {"metadata": metadata}
    if operation == "thread":
        value["metadata"]["graph_id"] = WORKER_GRAPH_ID
    else:
        value["assistant_id"] = WORKER_ASSISTANT_ID
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    assert asyncio.run(authorize(_auth_context(), value)) is False


@pytest.mark.parametrize("snapshot_length", [40, 64])
@pytest.mark.parametrize("operation", ["thread", "run"])
def test_worker_authorization_accepts_internal_capability(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    snapshot_length: int,
) -> None:
    monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URI", "postgres://localhost/test")
    metadata = assistant_runtime_metadata(
        AssistantRuntimeFacts(
            entry_profile="async_worker",
            repository_snapshot_sha="a" * snapshot_length,
        )
    )
    value = {"metadata": metadata}
    if operation == "thread":
        value["metadata"]["graph_id"] = WORKER_GRAPH_ID
    else:
        value["assistant_id"] = WORKER_ASSISTANT_ID
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    expected = (
        None
        if operation == "thread"
        else {
            "owner": "worker-user",
            "assistant_graph_id": WORKER_GRAPH_ID,
        }
    )
    assert asyncio.run(authorize(_auth_context(internal_worker=True), value)) == expected


@pytest.mark.parametrize("graph_id", [ASSISTANT_GRAPH_ID, MEMORY_GRAPH_ID])
@pytest.mark.parametrize("operation", ["thread", "run"])
@pytest.mark.parametrize(
    ("metadata", "internal_worker"),
    [
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker",
                    "repository_snapshot_sha": "a" * 40,
                }
            },
            False,
        ),
        (
            {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "system_eval",
                    "repository_snapshot_sha": "a" * 40,
                }
            },
            False,
        ),
        (
            assistant_runtime_metadata(
                AssistantRuntimeFacts(entry_profile="system_eval")
            ),
            True,
        ),
    ],
)
def test_non_worker_authorization_rejects_worker_only_facts_and_capability(
    graph_id: str,
    operation: str,
    metadata: dict[str, Any],
    internal_worker: bool,
) -> None:
    assistant_ids = {
        ASSISTANT_GRAPH_ID: MAIN_ASSISTANT_ID,
        MEMORY_GRAPH_ID: MEMORY_ASSISTANT_ID,
    }
    value = {"metadata": dict(metadata)}
    if operation == "thread":
        value["metadata"]["graph_id"] = graph_id
    else:
        value["assistant_id"] = assistant_ids[graph_id]
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    assert asyncio.run(
        authorize(_auth_context(internal_worker=internal_worker), value)
    ) is False


@pytest.mark.parametrize(
    "facts",
    [
        AssistantRuntimeFacts(
            entry_profile="agent_service",
            visual_capability_token="media-capability",
        ),
        AssistantRuntimeFacts(entry_profile="system_eval"),
    ],
)
@pytest.mark.parametrize("operation", ["thread", "run"])
def test_main_authorization_keeps_legal_non_snapshot_runtime_facts(
    facts: AssistantRuntimeFacts,
    operation: str,
) -> None:
    value = {"metadata": assistant_runtime_metadata(facts)}
    if operation == "thread":
        value["metadata"]["graph_id"] = ASSISTANT_GRAPH_ID
    else:
        value["assistant_id"] = MAIN_ASSISTANT_ID
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    assert asyncio.run(authorize(_auth_context(), value)) is not False


@pytest.mark.parametrize("assistant_id", LEGACY_SYSTEM_ASSISTANT_IDS)
def test_run_authorization_rejects_legacy_system_assistants(
    monkeypatch: pytest.MonkeyPatch,
    assistant_id: UUID,
) -> None:
    monkeypatch.setenv("REDIS_URI", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URI", "postgres://localhost/test")

    assert (
        asyncio.run(
            authorize_run_create(
                _auth_context(),
                {"assistant_id": assistant_id, "metadata": {}},
            )
        )
        is False
    )
