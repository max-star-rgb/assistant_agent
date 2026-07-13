# Continuous Keyframe Video Memory Design

## Goal

Close the existing realtime-video loop for `/agent-service/v1`: continuously
select useful frames from the media H.264 stream, analyze selected keyframes in
the background, retain structured per-video semantic memory, and let the Agent
answer later visual questions from that memory without a second visual MLLM
call. If no healthy memory snapshot exists, retain the current recent-frame
`video_understanding` provider path as a fallback.

"No final MLLM call" means no second visual MLLM scan at query time when a
healthy rolling snapshot is available. The main chat LLM still autonomously
chooses the governed `video_understanding` tool and turns its structured
observation into the user-facing answer.

## Scope

This change covers the vendor WebSocket H.264 path, the existing adaptive
keyframe components under `assistant_agent.video_ai`, runtime-owned video
context, the governed `video_understanding` tool, connection cleanup,
observability, tests, and current protocol documentation.

It does not add a new media wire format, persist semantic memory across process
restarts, enable real providers in the default profile, or change the normalized
Gateway protocol.

## Architecture

Each `/agent-service/v1` connection owns one bounded realtime video observer.
The observer is initialized lazily after the first valid decoded video frame and
keeps independent state per opaque `video_id`.

```text
media H.264 frame
  -> H264VideoIngestionService: validate and decode JPEG
  -> local adaptive sampling
  -> pixel difference + SSIM + local histogram semantic difference
  -> retain selected keyframe artifact
  -> bounded, serial background observation queue
  -> governed keyframe visual analysis
  -> runtime-owned rolling semantic memory

later chat
  -> Gateway turn with video_id
  -> Agent autonomously calls video_understanding
  -> healthy semantic snapshot: return snapshot without visual provider call
  -> no healthy snapshot: use existing recent-frame provider fallback
  -> chat LLM produces final response
```

The WebSocket entry layer only ingests, schedules, acknowledges, and cleans up.
It does not synthesize visual answers or call a Provider adapter directly.

## Components

### Local Keyframe Selection

The existing `AdaptiveFrameSampler`, `FrameDifferenceDetector`,
`SSIMChangeDetector`, `SemanticKeyframeSelector`, and local histogram embedding
remain the selection primitives. The existing FFmpeg H.264 decode produces both
the JPEG artifact and a fixed low-resolution grayscale fingerprint in one
bounded subprocess call. URI text must not be treated as image content, and the
fingerprint must not be promoted into prompts or traces.

Selection has three bounded stages:

1. Adaptive rate control suppresses redundant candidates in static scenes.
2. Pixel difference and SSIM measure visual and structural change.
3. A local histogram embedding supplies the semantic-change signal without an
   external embedding call.

The first frame and the configured maximum interval force a candidate. Selected
keyframes are copied to observer-owned storage before the three-frame raw
context window may evict them.

### Background Observer

The observer serializes keyframe analysis for one video so semantic updates
cannot arrive out of order. Scheduling is bounded and latest-wins: one in-flight
keyframe and at most one pending keyframe are retained. A newer selected frame
replaces an older pending frame and the replaced artifact is deleted.

`videoResponse` waits for H.264 decode and local selection scheduling, but not
for the visual MLLM. The receive loop therefore remains available for later
video, audio, control, ACK, and chat messages.

Real keyframe analysis is allowed only when the runtime profile permits real
providers. Default mock/local/offline behavior remains deterministic and makes
no network calls.

### Governed Observation

Selected-keyframe analysis remains behind the tool governance boundary. The
observer submits an internal structured keyframe-observation request through
`ActionValidator`, `ToolExecutor`, and `ToolRegistry`; the registry-owned
`video_understanding` tool is the only component allowed to reach the
configured visual/video Provider adapter.

The internal observation result must use the stable structured capability
contract and is mapped into rolling memory only after successful validation and
execution. Provider errors are sanitized and stored as status metadata, not as
raw responses.

There is no second user-facing tool. The observer uses the same
`video_understanding` tool with an internal observation-mode marker injected by
`ToolContext`, not by tool input. Observation mode forces Provider analysis of
the selected keyframe and bypasses memory resolution. The Agent cannot request
that mode and continues to see and choose the ordinary `video_understanding`
contract.

### Rolling Video Memory

A runtime-owned, thread-safe store keeps one snapshot per `video_id`. Each
snapshot contains only prompt-safe structured values:

- current scene summary;
- objects, people, actions, and recent events;
- bounded event timeline with frame id and timestamp;
- up to eight retained keyframe references;
- last successful observation time and sequence;
- last observation status and sanitized error metadata;
- pending/in-flight state needed for freshness decisions.

The store exposes immutable snapshots. It does not contain raw H.264 bytes,
Provider raw responses, credentials, phone numbers, or unbounded history.

### Query-Time Resolution

`video_understanding` resolves the stable `video_ref` as follows:

1. If the rolling snapshot has at least one successful observation and the most
   recent observation status is successful, return a `VideoUnderstandingResult`
   derived from memory. Do not call the visual Provider.
2. If no successful observation exists, the snapshot is not ready, or the most
   recent selected-keyframe observation failed, call the existing recent-frame
   Provider fallback.
3. If fallback also fails, return the existing structured, recoverable tool
   failure so the Agent can explain the limitation.

An observation currently in flight does not block a query. A previously healthy
snapshot remains usable unless the latest completed observation failed. This
keeps query latency bounded while honoring the confirmed failure fallback.

The tool result metadata identifies `source=rolling_video_memory` or
`source=recent_frame_fallback`, snapshot sequence, observed timestamp, and
keyframe count for tracing and tests.

## Concurrency And Lifecycle

All state is isolated by opaque `video_id`. The WebSocket connection owns its
observer tasks while the assistant runtime owns the stores used by tools.

On disconnect:

1. stop accepting observer work;
2. cancel pending work and wait boundedly for in-flight work;
3. reject any late result after the observer is closed;
4. remove rolling memory and raw-frame context;
5. delete raw and retained keyframe artifacts.

Synchronous Provider SDK work runs outside the event loop. Late thread results
must be discarded after close and must not recreate deleted session state.

## Failure Handling

- Invalid H.264 keeps the existing prompt-safe `videoResponse` failure.
- Local fingerprint or selection failure does not close the WebSocket; the
  decoded frame remains available to the recent-frame fallback.
- Provider timeout or malformed output records a sanitized failed observation,
  preserves the last successful snapshot, and activates query-time fallback.
- Queue replacement and cleanup are idempotent.
- A failed background observation never becomes a healthy memory snapshot.
- Logging and traces contain opaque ids, counts, status, reason codes, and
  latency only; they do not contain media bytes or Provider raw payloads.

## Configuration And Safety

No new dependency is introduced. The already-required `/usr/bin/ffmpeg`
process produces the JPEG and local grayscale fingerprint together. Tests use
the decoder seam and do not require external media or Provider calls.

The default runtime profile remains mock/local/offline. Real continuous visual
analysis requires `provider_smoke` or `pilot` plus explicit Provider selection
and local credentials. Merely finding an API key does not enable the path.

Keyframe and queue limits are fixed conservative defaults for this slice:

- recent raw-frame window: 3;
- retained semantic keyframes: 8;
- pending selected keyframes: 1 plus one in flight;
- keyframe selection minimum interval: 0.5 seconds;
- forced maximum interval: 8 seconds.

These values may later become validated configuration, but runtime tuning is not
part of this change.

## Protocol Behavior

No new client message is required. A successful `videoResponse` continues to
mean the H.264 frame was validated, decoded, registered, and accepted for local
selection; it does not claim that background MLLM observation has completed.

Chat progress and `chatResponseAck` behavior remain unchanged. The final media
response continues to use the existing delivery audit and optional application
ACK contract.

## Verification

Automated tests must prove:

- JPEG pixels, rather than URI bytes, drive local differences;
- static frames are throttled while meaningful changes select a keyframe;
- selected artifacts survive raw three-frame eviction;
- video ACK is sent before a blocked background observation completes;
- the observer serializes analysis and replaces stale pending work;
- successful observations update only their video snapshot;
- healthy memory makes `video_understanding` return without Provider invocation;
- not-ready and latest-failed memory use the recent-frame fallback;
- background failures are sanitized and do not destroy prior successful state;
- disconnect removes tasks, raw frames, retained keyframes, and semantic state;
- two WebSocket sessions cannot read or overwrite each other's video memory;
- existing autonomous native tool-call and acknowledged-delivery tests remain
  green.

A real Provider smoke is opt-in. When explicitly run, it must demonstrate at
least one selected keyframe observation, a later visual question answered from
`rolling_video_memory`, zero query-time visual Provider calls, and a delivered
`chatResponse` that can reach `acked` when the media client supports it.
