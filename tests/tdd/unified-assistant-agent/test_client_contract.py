from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from assistant_agent.agent_server.auth import (
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


def _auth_context() -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(identity="worker-user"))


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
    value = {
        "metadata": dict(metadata),
        **(
            {"graph_id": WORKER_GRAPH_ID}
            if operation == "thread"
            else {"assistant_id": WORKER_ASSISTANT_ID}
        ),
    }
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    assert asyncio.run(authorize(_auth_context(), value)) is False


@pytest.mark.parametrize("snapshot_length", [40, 64])
@pytest.mark.parametrize("operation", ["thread", "run"])
def test_worker_authorization_accepts_complete_snapshot(
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
    value = {
        "metadata": metadata,
        **(
            {"graph_id": WORKER_GRAPH_ID}
            if operation == "thread"
            else {"assistant_id": WORKER_ASSISTANT_ID}
        ),
    }
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
    assert asyncio.run(authorize(_auth_context(), value)) == expected
