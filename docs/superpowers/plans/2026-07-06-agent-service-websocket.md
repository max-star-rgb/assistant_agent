# Agent Service WebSocket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a FastAPI WebSocket compatibility endpoint for the media-service `/agent-service/v1` protocol.

**Architecture:** Add a new API entry adapter that parses the media envelope (`message`, `sessionId`, stringified `body`) and dispatches to one handler class per media message type. The adapter returns mock media-compatible responses and does not call the existing Gateway backend or assistant runtime.

**Tech Stack:** Python 3.10+, FastAPI WebSocket, async/await, pytest with FastAPI TestClient.

---

## File Structure

- Create `src/assistant_agent/api/agent_service_websocket.py`: media protocol WebSocket route, envelope parsing, connection state, handler classes, logging, and response serialization.
- Modify `src/assistant_agent/api/app.py`: include the new router in the FastAPI app.
- Create `tests/test_agent_service_websocket.py`: focused WebSocket contract tests for connection, ack, chat response, validation failures, unknown messages, and version rejection.

### Task 1: Tests For The Media Protocol Contract

**Files:**
- Create: `tests/test_agent_service_websocket.py`

- [x] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant_agent.api.app import create_app


def test_agent_service_start_ack_accepts_media_envelope() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControlStart",
                "s1",
                {
                    "userInfo": {"number": "10086"},
                    "agentInfo": {"agentNumber": "9001"},
                    "optional": {"kept": True},
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert response["sessionId"] == "s1"
    assert _body(response) == {"code": "OK"}


def test_agent_service_chat_returns_mock_chat_response() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 2,
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-06T10:00:00+08:00",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"
    assert response["sessionId"] == "s1"
    body = _body(response)
    assert body["number"] == "10086"
    assert body["message"]["chatIndex"] == 2
    assert "你好" in body["message"]["content"]


def test_agent_service_validates_required_start_fields() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControlStart",
                "s1",
                {"userInfo": {}, "agentInfo": {"agentNumber": "9001"}},
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert _body(response)["code"] == "FAIL"
    assert "userInfo.number" in _body(response)["message"]


def test_agent_service_validates_chat_contents() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 1,
                    "userNumber": "10086",
                    "contents": [{"speakerNumber": "10086", "speechContent": "hello"}],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"
    assert _body(response)["code"] == "FAIL"
    assert "contents[0].time" in _body(response)["message"]


def test_agent_service_rejects_malformed_body_and_unknown_message() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json({"message": "chat", "sessionId": "s1", "body": {"not": "a string"}})
        malformed = websocket.receive_json()

        websocket.send_json(_envelope("notSupported", "s1", {}))
        unknown = websocket.receive_json()

    assert malformed["message"] == "chatResponse"
    assert _body(malformed)["code"] == "FAIL"
    assert "body must be a JSON string" in _body(malformed)["message"]
    assert unknown["message"] == "error"
    assert _body(unknown)["code"] == "FAIL"
    assert "unknown message type" in _body(unknown)["message"]


def test_agent_service_requires_session_id() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            {
                "message": "assistantControlStart",
                "body": json.dumps(
                    {"userInfo": {"number": "10086"}, "agentInfo": {"agentNumber": "9001"}},
                    ensure_ascii=False,
                ),
            }
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert _body(response)["code"] == "FAIL"
    assert "sessionId" in _body(response)["message"]


def test_agent_service_rejects_non_v1_path() -> None:
    client = TestClient(create_app())

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/agent-service/v2?sessionId=s1") as websocket:
            response = websocket.receive_json()
            assert response["message"] == "error"
            assert _body(response)["code"] == "FAIL"
            assert "unsupported agent service version" in _body(response)["message"]
            websocket.receive_json()

    assert exc_info.value.code == 1008


def _envelope(message: str, session_id: str, body: dict) -> dict:
    return {
        "message": message,
        "sessionId": session_id,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _body(response: dict) -> dict:
    return json.loads(response["body"])
```

- [x] **Step 2: Run tests to verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -q
```

Expected: fails because `/agent-service/v1` is not registered.

### Task 2: Implement The Agent Service WebSocket Adapter

**Files:**
- Create: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/api/app.py`
- Test: `tests/test_agent_service_websocket.py`

- [x] **Step 1: Create `agent_service_websocket.py`**

Implement:

- `router = APIRouter()`
- `@router.websocket("/agent-service/{version}")`
- `AgentServiceConnectionState`
- `AgentServiceProtocolError`
- `BaseHandler`
- `AssistantControlStartHandler`
- `ChatHandler`
- envelope helpers and logging

The route must accept the socket, reject non-v1 with `FAIL` then close `1008`, otherwise loop on `receive_text()`, parse, dispatch, and `send_text()` responses.

- [x] **Step 2: Register the router**

In `src/assistant_agent/api/app.py`, import the new router:

```python
from assistant_agent.api.agent_service_websocket import router as agent_service_websocket_router
```

Then include it next to the other WebSocket/API routers:

```python
app.include_router(agent_service_websocket_router)
```

- [x] **Step 3: Run tests to verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -q
```

Expected: all tests pass.

### Task 3: Adjacent Regression Validation

**Files:**
- Read-only validation for existing WebSocket routes.

- [x] **Step 1: Run adjacent WebSocket regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py tests/test_websocket_graph_runtime.py -q
```

Expected: existing Gateway and agent WebSocket tests pass.

- [x] **Step 2: Run focused fast marker if time permits**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: fast test suite passes. If it fails outside this route's scope, record the failing tests and reason instead of editing unrelated code.
