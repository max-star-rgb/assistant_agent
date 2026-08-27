from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from assistant_agent.agent_server.auth import (
    authorize_run_create,
    authorize_thread_create,
)
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID, WORKER_GRAPH_ID
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
        }
    )

    assert stream.message_nodes == {
        "main-message": "model",
        "worker-message": "__internal_subgraph__",
    }


def test_obsolete_coding_attestation_route_is_removed() -> None:
    assert "/internal/evaluation/coding-attestation" not in {
        route.path for route in app.routes
    }


@pytest.mark.parametrize("operation", ["thread", "run"])
def test_worker_authorization_rejects_missing_snapshot(operation: str) -> None:
    metadata = {
        ASSISTANT_RUNTIME_METADATA_KEY: {"entry_profile": "async_worker"}
    }
    value = {
        "metadata": metadata,
        **(
            {"graph_id": WORKER_GRAPH_ID}
            if operation == "thread"
            else {"assistant_id": WORKER_GRAPH_ID}
        ),
    }
    authorize = (
        authorize_thread_create if operation == "thread" else authorize_run_create
    )

    assert asyncio.run(authorize(_auth_context(), value)) is False


@pytest.mark.parametrize("operation", ["thread", "run"])
def test_worker_authorization_accepts_complete_snapshot(operation: str) -> None:
    metadata = assistant_runtime_metadata(
        AssistantRuntimeFacts(
            entry_profile="async_worker",
            repository_snapshot_sha="a" * 40,
        )
    )
    value = {
        "metadata": metadata,
        **(
            {"graph_id": WORKER_GRAPH_ID}
            if operation == "thread"
            else {"assistant_id": WORKER_GRAPH_ID}
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
