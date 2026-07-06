# Agent Service WebSocket Compatibility Design

Date: 2026-07-06

## Goal

Implement a FastAPI WebSocket compatibility entry for the media-service protocol at:

```text
ws://{host}:{port}/agent-service/v1
```

The entry must accept media-service envelopes using `message`, `sessionId`, and stringified JSON `body`, then return mock protocol-compatible responses. It must not replace the existing Gateway protocol or change `/ws/gateway` and `/ws/realtime/media` behavior.

## Decision

Add a thin media compatibility adapter at the API entry layer. The adapter owns transport parsing, media envelope validation, handler dispatch, logging, and mock responses. It does not call the real assistant runtime in this phase.

This keeps the media contract isolated from the existing Gateway frame contract:

```text
Media service
  -> /agent-service/v1
  -> media compatibility handlers
  -> mock ack/response
```

## Route And Connection Behavior

- Add a WebSocket route for `/agent-service/{version}`.
- Accept only `version == "v1"`.
- Parse query parameters on connection and record `sessionId` when present.
- Maintain lightweight per-connection state:
  - current `session_id`
  - last `assistantControlStart` body
  - received chat bodies
  - raw query parameters for diagnostics
- Log connection opened, every received raw message, every outbound response, protocol errors, and disconnect.

If a message envelope contains `sessionId`, use it for that message. If the connection did not have a session id yet, store it. Every handled message must have a `sessionId` either from the envelope or from the connection query. Missing `sessionId` returns a `FAIL` response and does not dispatch to a handler.

For non-`v1` paths, accept the WebSocket long enough to send a media-compatible `FAIL` response, then close with policy violation code `1008`.

## Envelope Format

Inbound messages are JSON objects:

```json
{
  "message": "assistantControlStart",
  "sessionId": "session-1",
  "body": "{\"userInfo\":{\"number\":\"10086\"},\"agentInfo\":{\"agentNumber\":\"9001\"}}"
}
```

`body` must be a JSON-serialized string. The adapter parses it into a dict before dispatching to a handler.

Outbound messages keep the same media envelope shape:

```json
{
  "message": "assistantControlStartAck",
  "sessionId": "session-1",
  "body": "{\"code\":\"OK\"}"
}
```

## Handler Model

Create one handler class per media `message` type. Each handler inherits from `BaseHandler`.

`BaseHandler` responsibilities:

- Define the inbound `message_type`.
- Parse and validate the stringified `body`.
- Provide helper methods for required nested fields.
- Convert exceptions into protocol responses with `code="FAIL"` and a readable `message`.

`AssistantControlStartHandler`:

- Handles `assistantControlStart`.
- Requires `body.userInfo.number`.
- Requires `body.agentInfo.agentNumber`.
- Stores the full parsed body on connection state.
- Returns `assistantControlStartAck` with `body.code == "OK"` on success.

`ChatHandler`:

- Handles `chat`.
- Requires `body.chatIndex`.
- Requires `body.userNumber`.
- Requires `body.contents` as a non-empty list.
- Requires each content item to include `speakerNumber`, `speechContent`, and `time`.
- Stores the full parsed body on connection state.
- Returns `chatResponse` with:
  - `number`: `body.userNumber`
  - `message.chatIndex`: `body.chatIndex`
  - `message.content`: mock text derived from the latest user speech content.

Unknown `message` values return an error envelope with `code="FAIL"`.

## Error Handling

All protocol and handler errors return media-compatible response bodies:

```json
{
  "message": "error",
  "sessionId": "session-1",
  "body": "{\"code\":\"FAIL\",\"message\":\"unknown message type: xxx\"}"
}
```

For handler-specific failures, use that handler's response message when it is known:

- `assistantControlStart` failure returns `assistantControlStartAck`.
- `chat` failure returns `chatResponse`.
- malformed envelope or unknown message returns `error`.

The WebSocket should remain open after recoverable message errors so the media service can retry. Connection-level failures and client disconnects are logged.

## Non Goals

- No real LLM call.
- No direct tool execution.
- No changes to existing Gateway session semantics.
- No auth or production identity policy beyond the existing local API style unless added in a later task.
- No raw audio or video streaming.

## Tests

Add focused FastAPI TestClient WebSocket tests:

- `/agent-service/v1?sessionId=s1` accepts a connection.
- non-`v1` route sends `FAIL` and closes with code `1008`.
- valid `assistantControlStart` returns `assistantControlStartAck` with `OK`.
- missing `userInfo.number` or `agentInfo.agentNumber` returns `FAIL`.
- valid `chat` returns `chatResponse` with `number` and `message.chatIndex`.
- malformed envelope or non-JSON-string `body` returns `FAIL`.
- unknown `message` returns `FAIL`.

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -q
```

If adjacent route registration changes affect API startup, also run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py tests/test_websocket_graph_runtime.py -q
```
