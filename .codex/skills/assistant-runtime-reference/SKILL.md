---
name: assistant-runtime-reference
description: Project-local workflow for assistant_agent Agent Server and media protocol work. Use when Codex needs to design, review, debug, migrate, or test LangGraph Agent Server deployment, media custom routes, realtime wire compatibility, cancel/interrupt/reconnect behavior, or compare with the legacy /home/lenovo1/pycharm_project/runTime implementation.
---

# Agent Server 与旧 Runtime 参考

Use this skill when Agent Server deployment or Media-Agent compatibility in `assistant_agent` must be designed, reviewed, debugged, migrated, tested, or compared with the legacy project at `/home/lenovo1/pycharm_project/runTime`.

`assistant_agent` remains the authoritative project. `runTime` is reference material for protocol compatibility and regression behavior only.

## Primary Project Entry

Read `docs/agent-server-architecture.md` first for the current production entry, native resource lifecycle, custom-route boundary and code map.

When the task touches `/agent-service/v1`, media `assistantControl` / `chat` / `audio` / `video` / `interrupt`, `chatResponse`, `chatResponseAck`, H.264 Hex video, Media-Agent streaming semantics, or media-side compatibility examples, also read `docs/media-agent-service-websocket.md`. That file is the single authority for the Media-Agent wire protocol and replaces ad hoc temporary protocol notes.

IMAGE、TD_MODEL、VIDEO 渲染投递、`/torender`、图片转 3D 及其回调也统一以 `docs/media-agent-service-websocket.md` 为准。媒体服务是转发代理；Agent 与渲染服务没有直连，不要虚构 Agent 到渲染服务的 HTTP 或 WebSocket 接口。

Use the legacy `runTime` files only after that current project entry, and only when protocol compatibility or behavior comparison is needed.

## When To Read runTime

Only read the legacy implementation when the task explicitly requires compatibility with one of these removed or historical surfaces:

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

- Treat `docs/agent-server-architecture.md` as the current production deployment authority.
- Preserve the public Media-Agent wire in `docs/media-agent-service-websocket.md`; map it mechanically to Agent Server thread/run/stream/cancel through the public SDK.
- Do not add a project-owned session manager, realtime backend, checkpoint facade or second agent loop.
- Treat `assistant_agent.gateway` as legacy-neutral wire/event helpers for peripheral compatibility only; new production lifecycle code belongs under `assistant_agent.agent_server`.
- Keep wire frame names stable unless the user explicitly requests a protocol break.
- Treat `runTime` as compatibility reference and regression-test source, not as code to import from `assistant_agent`.
- If `runTime` behavior conflicts with current `assistant_agent` docs or explicit user direction, follow `assistant_agent` and explain the compatibility difference.

## Validation

For Agent Server or media custom-route changes, run the smallest relevant subset:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

For compatibility checks against the legacy project:

```bash
cd /home/lenovo1/pycharm_project/runTime && conda run -n hello_agent env PYTHONPATH=src python -m unittest tests.test_runtime_assistant_agent_smoke
cd /home/lenovo1/pycharm_project/runTime && conda run -n hello_agent env PYTHONPATH=src python -m unittest tests.test_cancel_interrupt tests.test_expects_reply tests.test_multiturn
```

For skill/doc-only changes:

```bash
git diff --check -- AGENTS.md docs/agent-server-architecture.md docs/media-agent-service-websocket.md .codex/skills/assistant-runtime-reference src/assistant_agent/agent_server src/assistant_agent/gateway tests/core/contract/test_gateway_contract.py scripts/agent_cli.py scripts/media_simulator.py
```
