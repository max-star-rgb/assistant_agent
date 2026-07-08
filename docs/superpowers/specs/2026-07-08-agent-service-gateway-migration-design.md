# Agent Service Gateway Migration Design

Date: 2026-07-08

## Goal

Move `/agent-service/v1` chat handling behind Gateway while preserving the
vendor compatibility WebSocket envelope.

Target internal path:

```text
/agent-service/v1
    -> vendor WebSocket compatibility adapter
    -> GatewayTurnFacade
    -> GatewaySessionManager / GatewaySessionService
    -> GatewayAgentAdapter
    -> AssistantRuntimeApp
    -> run_assistant_request
    -> AgentGraphRuntime
```

## Problem

`/agent-service/v1` currently parses vendor envelopes and returns mock
`assistantControlStartAck` / `chatResponse` payloads without entering Gateway or
the assistant runtime. That was acceptable while the route was only a protocol
skeleton, but it is now the last WebSocket-shaped product entry path that does
not exercise Gateway lifecycle.

Leaving it as a mock path makes the architecture ambiguous: WebSocket entries
mostly go through Gateway, while the vendor media-service compatibility route
does not. Observer integration would then miss one realtime-facing entry path.

## Approach

Keep the external vendor protocol stable and use Gateway only internally.

`assistantControlStart` remains an entry-layer handshake:

- validate `userInfo.number` and `agentInfo.agentNumber`;
- store the body in connection state;
- return the same `assistantControlStartAck` success envelope.

`chat` becomes Gateway-backed:

- validate `chatIndex`, `userNumber`, and `contents`;
- use the latest `speechContent` as the Gateway turn text;
- run one `GatewayTurnRequest` through a per-connection local
  `GatewaySessionManager(start_reaper=False)`;
- wrap `GatewayTurnResult.response_text` into the existing
  `chatResponse.body.message.content` field.

The route creates one Gateway manager/facade per WebSocket connection. This
keeps same-connection chats in the same Gateway service so session history is
available, and avoids leaking state into the process-global Gateway services
used by `/ws/gateway` and HTTP.

## Metadata and Identity

The vendor `userNumber` is used as `GatewayTurnRequest.user_id`. The envelope
`sessionId` remains the Gateway session id.

The route does not mark `source` as a trusted Gateway/Web source. Instead it
passes prompt-safe metadata:

- `transport="agent_service_websocket"`;
- `agent_service.chat_index`;
- `agent_service.user_number`;
- `agent_service.content_count`;
- `agent_service.control_started`;
- `gateway.suppress_realtime_backend_source=true`.

Gateway will still add `runtime.history` / `gateway.history` before the request
reaches the assistant runtime.

## Error Handling

- Unsupported versions keep the current `error` envelope and close code `1008`.
- Malformed envelopes, malformed stringified bodies, missing session ids, and
  missing required vendor fields keep returning the current failure envelope.
- Gateway timeout or backend error returns `chatResponse` with `code="FAIL"` and
  a sanitized message.
- The local Gateway manager is closed when the WebSocket disconnects or the
  route exits.

## Testing

- Add a WebSocket test with a recording runtime proving a `chat` message reaches
  runtime with `metadata["runtime"]["history"]`.
- Assert the external response is still a `chatResponse` envelope with
  `sessionId`, `number`, `message.chatIndex`, and `message.content`.
- Keep validation, malformed body, missing session id, and unsupported version
  tests green.
- Add the route to product entry boundary tests so it cannot directly import or
  construct `AgentGraphRuntime`.

## Stop Point

Stop this phase after `/agent-service/v1` chat is Gateway-backed and the vendor
wire contract remains stable. At that point HTTP, local CLI text/scenario,
legacy WebSocket, normalized Gateway WebSocket, media relay WebSocket, and the
vendor compatibility WebSocket all enter Gateway. The next phase should be an
observer readiness checkpoint, not more entry migration, unless a new product
entrypoint is discovered.
