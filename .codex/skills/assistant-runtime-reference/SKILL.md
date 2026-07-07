---
name: assistant-runtime-reference
description: Project-local workflow for assistant_agent Gateway work. Use when Codex needs to design, review, debug, migrate, or test assistant_agent.gateway, realtime phone/gateway protocol frames, session history, cancel, interrupt, multiturn behavior, WebSocket frame transport, or compare with the legacy /home/lenovo1/pycharm_project/runTime implementation.
---

# Assistant Runtime Reference

Use this skill when Gateway behavior in `assistant_agent` must be designed, reviewed, debugged, migrated, tested, or compared with the legacy compatibility project at `/home/lenovo1/pycharm_project/runTime`.

`assistant_agent` remains the authoritative project. `runTime` is reference material for protocol compatibility and regression behavior only.

## Primary Project Entry

Read `docs/gateway-architecture.md` first for current `assistant_agent` Gateway responsibilities, entry-layer boundaries, realtime backend contract, code map, OpenClaw reference boundary, and update rules.

Use the legacy `runTime` files only after that current project entry, and only when protocol compatibility or behavior comparison is needed.

## When To Read runTime

Read the legacy implementation when the task touches any of these:

- Gateway wire protocol frame names or payload semantics: `message.user`, `run.started`, `stream.chunk`, `run.end`, `run.cancel`, `ping`/`pong`, call frames, config frames, or unknown-frame errors.
- Gateway session lifecycle behavior: user session tracking, generated `turn_id`/`run_id`, active run registration, run end reasons, default `expects_reply`, backend error conversion, active-run cleanup, idle timeout, hangup grace, or config update behavior.
- Cancel and interrupt behavior: explicit `run.cancel`, new message interrupting an active run in the same session, cancellation by session/run id, or `run_not_found` behavior.
- Multiturn behavior: per-session user text history, history snapshots passed to the backend, and history assertions in compatibility tests.
- Transport behavior: in-memory endpoint pairs, WebSocket JSON frame encoding/decoding, bridge forwarding rules, or client disconnect cancellation.
- Compatibility tests copied or mirrored from `runTime/tests`.

Do not read `runTime` for normal assistant loop, tool calling, memory, provider, API, or docs work unless the user explicitly asks for runtime compatibility.

## Reference Files

Inspect only the files relevant to the behavior under change:

- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/protocol.py`: frame schema, frame helper, call/config constants, supported modalities.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/transport.py`: `InMemoryDuplex`, `Endpoint`, close and inject semantics.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/ws.py`: WebSocket frame JSON encoding/decoding and `WsEndpoint`.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/runtime/runtime.py`: runtime service lifecycle, session history, run registration, cancel, interrupt, `expects_reply`, and run.end behavior.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/runtime/assistant_agent_adapter.py`: previous bridge from runtime events to assistant realtime backend events.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/runtime/ws_server.py`: runtime WebSocket server shape.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/gateway/gateway.py`: client-to-runtime bridge, filtering, disconnect cancellation, unsupported modality handling.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/gateway/runtime_manager.py`: only when telephony/user runtime routing is involved.
- `/home/lenovo1/pycharm_project/runTime/src/openclaw_gateway_runtime/gateway/ws_server.py`: only when external gateway WebSocket serving is involved.

Compatibility tests:

- `/home/lenovo1/pycharm_project/runTime/tests/test_runtime_assistant_agent_smoke.py`
- `/home/lenovo1/pycharm_project/runTime/tests/test_cancel_interrupt.py`
- `/home/lenovo1/pycharm_project/runTime/tests/test_expects_reply.py`
- `/home/lenovo1/pycharm_project/runTime/tests/test_multiturn.py`

Avoid `runTime/src/openclaw_gateway_runtime/agent_runtime/**`, `skills/**`, and old Anthropic/OpenClaw adapters unless the user explicitly asks to inspect legacy non-assistant runtime behavior. The assistant project should not reintroduce that adapter-selection loop.

## Working Rules

- Treat `docs/gateway-architecture.md` as the current Gateway architecture authority.
- Preserve the assistant public backend interface: `RealtimeAgentRequest`, `RealtimeAgentEvent`, `RealtimeAgentResult`, `RealtimeAgentBackend`, and `RealtimeCancelToken`.
- Default new assistant Gateway session code to `AgentGraphRealtimeBackend`; do not add a second agent loop.
- Use `assistant_agent.gateway` for current Gateway code. Do not add removed runtime-compatibility entrypoints.
- Keep wire frame names stable unless the user explicitly requests a protocol break.
- Treat `runTime` as compatibility reference and regression-test source, not as code to import from `assistant_agent`.
- If `runTime` behavior conflicts with current `assistant_agent` docs or explicit user direction, follow `assistant_agent` and explain the compatibility difference.

## Validation

For assistant Gateway changes, run the smallest relevant subset:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_session.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py
```

For compatibility checks against the legacy project:

```bash
cd /home/lenovo1/pycharm_project/runTime && conda run -n hello_agent env PYTHONPATH=src python -m unittest tests.test_runtime_assistant_agent_smoke
cd /home/lenovo1/pycharm_project/runTime && conda run -n hello_agent env PYTHONPATH=src python -m unittest tests.test_cancel_interrupt tests.test_expects_reply tests.test_multiturn
```

For skill/doc-only changes:

```bash
git diff --check -- AGENTS.md docs/gateway-architecture.md .codex/skills/assistant-runtime-reference src/assistant_agent/gateway src/assistant_agent/api/gateway_runtime.py src/assistant_agent/api/gateway_websocket.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py scripts/run_gateway_client.py
```
