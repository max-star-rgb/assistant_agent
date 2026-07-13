# Agent Service H.264 Video Ingestion Design

## Goal

Make `/agent-service/v1` turn self-contained H.264 I-frame messages into a
bounded recent-frame context that the existing assistant runtime can use. A
later chat request such as `识别眼前物体` must carry the session video
reference into Gateway so the real LLM can autonomously request the governed
`video_understanding` tool.

This change extends the existing assistant loop. It does not add a second MLLM
connection or bypass `ActionValidator`, `ToolExecutor`, `ToolRegistry`, provider
policy, or tracing.

## Protocol Assumptions

Each `video` message contains one independently decodable frame:

- `contents[].videoContent` is lowercase hexadecimal H.264 data without a
  `0x` prefix.
- The decoded bytes contain Annex-B NAL start codes.
- Every message contains SPS, PPS, and an I-frame because the media service
  encodes each source frame independently with `-frames 1`.
- `videoIndex` is the frame sequence identifier.
- `videoConfig.codec` is `H264`; resolution and frame rate are diagnostic
  metadata and do not authorize resource allocation.

## Architecture

Add an entry-layer H.264 frame ingestion service with three responsibilities:

1. Validate the transport payload and resource limits.
2. Decode one independent H.264 frame to a JPEG by invoking the existing local
   FFmpeg binary with a timeout and fixed output constraints.
3. Append a `VideoFrame` to the current `AgentGraphRuntime.video_context_store`
   and maintain a bounded set of local JPEG artifacts.

`VideoHandler` remains a thin protocol adapter. It passes validated content to
the ingestion service and returns `videoResponse` only after the frame has been
decoded and registered. The service derives a stable video id from the bound
session rather than trusting a client path or filename.

`ChatHandler` checks whether the current connection has accepted video frames.
When it has, its `GatewayTurnRequest` carries the stable video id in
`video_ids`. Gateway and `GatewayAgentAdapter` then populate
`UserRequest.video_ids`. The LLM sees the governed video tool and decides
whether the user request requires it. `VideoUnderstandingTool` resolves the
video id through the same runtime's `VideoContextStore` and sends its recent
JPEG frame references to the configured video provider.

## Runtime Ownership

The global runtime created by `routes_agent.get_agent_runtime()` already owns
the `InMemoryVideoContextStore` used by its registry. The agent-service entry
must append to that exact store. It must not construct a second registry or
store.

Tests may inject a runtime or ingestion service, but production resolution goes
through `AssistantRuntimeApp.runtime`. A runtime lacking a compatible video
context store produces an explainable protocol failure.

## Artifact Lifecycle

Decoded JPEG files live under an untracked runtime directory, grouped by a
sanitized opaque session digest. Client-provided identifiers never become path
components.

The ingestion service keeps only the configured recent window, initially three
frames per video id. When a new frame exceeds the window, the service deletes
the evicted JPEG after updating the context store. Connection close removes
connection-owned decoded artifacts and context entries when supported. A
process restart naturally clears the in-memory index; stale runtime files are
removed during service initialization or explicit cleanup.

The existing `VideoContextStore` protocol will gain a bounded removal method so
artifact cleanup and context cleanup remain consistent. Existing callers keep
their current append/read behavior.

## Validation And Limits

Before FFmpeg execution, reject:

- non-H264 codec values;
- empty, odd-length, or non-hex content;
- content exceeding a configured byte limit;
- bytes without a three-byte or four-byte Annex-B start code;
- missing session/user binding.

FFmpeg runs without a shell, reads H.264 bytes from stdin, writes exactly one
JPEG frame, has a short timeout, and captures bounded diagnostic output. A
missing binary, timeout, non-zero exit, or empty JPEG becomes a structured
`videoResponse` failure. Raw frame bytes, complete Hex data, and provider raw
responses are never logged or placed in prompts/traces.

The WebSocket connection stays open after recoverable frame failures so later
valid frames can proceed.

## ACK Semantics And Capabilities

`{"code": 0, "message": "video received"}` now means the frame was validated,
decoded, and registered for assistant use. Invalid frames return the existing
`videoResponse` envelope with `code=FAIL` and a prompt-safe reason.

`AGENT_SERVICE_ENTRY_CAPABILITIES` advertises raw-media and video-reference
support after this path exists. It still does not claim audio-reference or
image-reference support.

## Testing

Tests are written before implementation and cover:

- valid H.264 Hex is decoded and registered as a JPEG frame;
- invalid Hex, unsupported codec, missing start code, oversized input, decoder
  error, and decoder timeout return recoverable failures;
- a sliding window evicts old frame records and artifacts;
- `video` followed by `chat` passes the stable `video_id` through Gateway into
  `UserRequest.video_ids`;
- a scripted real-chat adapter requests `video_understanding`, proving the call
  traverses validator, executor, registry, and the configured mock video adapter
  without an external network call;
- existing agent-service audio/chat/control compatibility remains unchanged.

The focused validation suite includes agent-service WebSocket, video context,
video-understanding tool, Gateway adapter, and architecture-boundary tests.

## Documentation

Update `docs/media-agent-service-websocket.md` and
`docs/gateway-architecture.md` so raw `video` is no longer documented as ACK
only. Document the self-contained I-frame requirement, bounded frame window,
new ACK meaning, and the fact that tool selection remains LLM-first.

## Non-Goals

- Continuous H.264 streams with inter-frame dependencies.
- Audio decoding, ASR, VAD, or TTS changes.
- Direct media-provider calls from the WebSocket handler.
- A new external MLLM WebSocket client.
- Persisting raw H.264 payloads or complete video recordings.
- Installing PyAV, OpenCV, NumPy, or other new Python dependencies.

## Acceptance Criteria

With the server running in `provider_smoke` and video provider `ark`, a media
client can send valid self-contained H.264 frame messages, receive successful
ACKs, then send a chat request asking what is visible. The resulting run contains
the session video reference, a provider-native `video_understanding` tool call,
a successful governed tool observation, and a final answer grounded in recent
decoded frames. Offline tests prove the same routing with scripted/mock
providers and no network access.
