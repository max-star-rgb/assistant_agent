"""Stable Agent Server graph identities for production adapters."""

import os

ASSISTANT_GRAPH_ID = "assistant-native-v4"
MEMORY_GRAPH_ID = "assistant-memory-v1"
WORKER_GRAPH_ID = "assistant-worker-v2"


def agent_server_url() -> str:
    raw_port = os.environ.get("ASSISTANT_AGENT_SERVER_PORT", "8089")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("ASSISTANT_AGENT_SERVER_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise ValueError("ASSISTANT_AGENT_SERVER_PORT must be between 1 and 65535")
    return f"http://127.0.0.1:{port}"


__all__ = [
    "ASSISTANT_GRAPH_ID",
    "MEMORY_GRAPH_ID",
    "WORKER_GRAPH_ID",
    "agent_server_url",
]
