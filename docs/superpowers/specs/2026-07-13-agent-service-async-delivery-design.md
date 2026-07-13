# Agent Service Async Chat Delivery Design

## Goal

Prevent long video-understanding turns from blocking `/agent-service/v1`
inbound media processing, and make final response delivery observable and
acknowledgeable without breaking existing media clients.

## Root Cause

The current WebSocket route awaits every handler before reading the next
message. A video-understanding chat can take 50 seconds, including more than
40 seconds in the video provider. During that wait, new video messages collect
in the TCP receive queue. The observed production connection accumulated about
3.3 MB before disconnecting. The server does not actively close a valid v1
connection, so the likely triggers are media-side response timeout, heartbeat
timeout, or backpressure.

## Compatibility Contract

Existing envelopes remain valid. Clients that do not advertise delivery
extensions receive the same single terminal `chatResponse` shape they receive
today. They benefit from concurrent inbound processing but do not receive new
progress messages.

`assistantControl` may optionally include:

```json
{
  "clientCapabilities": {
    "chatProgress": true,
    "chatResponseAck": true
  }
}
```

When `chatProgress=true`, the Agent sends a `chatProgress` response immediately
after accepting a chat and repeats it every 15 seconds while the run remains
active. Progress frames contain `chatIndex`, `deliveryId`, and
`status="PROCESSING"`; they contain no model output.

Every final media-style `chatResponse` includes a top-level `deliveryId` in its
body. Clients declaring `chatResponseAck=true` send:

```json
{
  "message": "chatResponseAck",
  "body": "{\"deliveryId\":\"...\",\"chatIndex\":\"...\"}"
}
```

The Agent replies with `chatResponseAck` and `code=0` after recording the ACK.
Unknown, duplicate, or mismatched ACKs return `code="FAIL"` without closing the
connection.

## Concurrent Connection Runtime

The receive loop continues to parse messages while chat tasks run. Non-chat
handlers remain awaited inline so `videoResponse`, audio ACK, control ACK, and
interrupt ACK preserve their current ordering. Chat messages create tracked
background tasks and return control to the receive loop immediately.

One connection-scoped send lock serializes every outbound frame. Handler tasks
do not call `WebSocket.send_text` concurrently. Chat correlation uses the
original `chatIndex` and a server-generated opaque `deliveryId`.

Multiple chat messages may be accepted. Gateway remains authoritative for
same-session serialization; the entry layer only tracks response correlation.

On disconnect, the route records the close code/reason when available, cancels
progress tasks, requests cancellation of pending chat tasks through connection
task cancellation and Gateway manager close, waits for them with a bounded
grace period, then cleans video artifacts. No task may send after the connection
is marked closed.

## Delivery State And Audit

Delivery states are:

```text
accepted -> processing -> sent -> acked
                    \-> failed
                    \-> disconnected_before_send
sent -> disconnected_before_ack
```

A small process-local delivery registry owns active state. A JSONL audit sink
under `.data/agent_service_delivery.jsonl` records prompt-safe events so an
operator can determine whether a response was accepted, sent to the WebSocket
API, acknowledged by the media application, or interrupted by disconnect.

Audit records include schema version, delivery id, session id digest, chat
index digest, event type, timestamp, close code/reason category, and run/trace
ids when available. They never include raw video/audio, response text,
credentials, provider responses, phone numbers, or unredacted client payloads.

`sent` means `WebSocket.send_text()` returned successfully. It does not claim
the media application consumed the message. Only `acked` provides application
delivery confirmation.

## Media-Side Requirement

The media repository is not available in this workspace. Its implementation
must be updated separately to advertise the two capabilities, accept
`chatProgress`, keep the connection open for at least 90 seconds, and send
`chatResponseAck` after processing the final response. Until then, concurrent
inbound processing removes Agent-side backpressure, but a hard media-side
30-second response timer can still disconnect before a slow provider finishes.

## Testing

Tests cover:

- a blocked chat task does not prevent a later video message from receiving an
  ACK;
- all outbound sends are serialized;
- legacy clients receive only the final `chatResponse`;
- negotiated clients receive immediate and periodic `chatProgress`;
- final responses contain stable delivery correlation;
- valid, duplicate, unknown, and mismatched ACK behavior;
- audit transitions for sent, acked, failed, disconnect-before-send, and
  disconnect-before-ack;
- close code/reason capture and pending-task cancellation;
- no response body or raw media appears in audit records;
- existing H.264 ingestion and governed video-tool tests remain green.

## Non-Goals

- Changing Gateway's same-session ordering or adding a second agent loop.
- Claiming end-to-end delivery without a media ACK.
- Persisting response content in delivery audit.
- Modifying the unavailable media-service repository.
- Replacing WebSocket transport-level ping/pong.

## Acceptance Criteria

During a 50-second video-understanding turn, the Agent continues decoding and
ACKing incoming video frames, the TCP receive queue does not grow because of
the chat handler, and negotiated clients receive progress. The terminal
`chatResponse` is recorded as `sent`; after media returns
`chatResponseAck`, the same delivery is recorded as `acked`. On disconnect,
the audit explicitly distinguishes before-send from after-send/before-ack.
