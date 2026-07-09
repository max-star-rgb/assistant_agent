# Realtime Runtime Operator Runbook

This runbook validates the text-only Personal Realtime Assistant Runtime. The
media service owns ASR, TTS, VAD, telephony SDK state, audio transport, and
playback. This repository validates finalized text events, Gateway lifecycle,
Agent runtime execution, tool governance, memory boundaries, stream frames,
interrupt/cancel/hangup, and trace visibility.

## Scope

Use this runbook when checking the first production-shaped runtime loop:

```text
Media Service text event
  -> /ws/realtime/media
  -> Gateway session/run lifecycle
  -> GatewayAgentAdapter
  -> AgentGraphRuntime
  -> ActionValidator -> ToolExecutor -> ToolRegistry
  -> stream.chunk / event.progress / event.tool / run.end
  -> trace query
```

Non-goals:

- Do not test raw audio in this repository.
- Do not add another agent loop.
- Do not bypass `ActionValidator`, `ToolExecutor`, or `ToolRegistry`.
- Do not treat browser chat as the primary product path.

## Local Server

Start the backend in mock/local mode:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --provider mock --image-provider mock
```

Expected operator hints should point to realtime and Gateway smoke clients.

## In-Process Gate

Run the complete text-only lifecycle gate without a server:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario all --quiet
```

The gate must cover:

- `basic`: completed run, text response, inactive hangup does not cancel.
- `interrupt`: active run is cancelled, new turn completes.
- `hangup`: active run is cancelled and hangup is acknowledged.
- `cancel`: explicit run cancel returns prompt-safe cancel metadata.
- `tool_interrupt`: stale tool output is suppressed and the new turn completes.

## Server-Backed Media Smoke

With the server running, validate the Media Relay protocol surface:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/realtime_media_client.py --server http://127.0.0.1:8000 --scenario all
```

Use `--strict-cancel` only when the selected backend reliably returns terminal
cancel status before the test timeout.

## Gateway Debug Smoke

Use the normalized Gateway frame entry for low-level debugging:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_gateway_client.py --server http://127.0.0.1:8000 "你好"
```

This is a debug smoke, not a separate product runtime.

## Trace Check

Every completed realtime run should return a `trace_id` in `run.end`. Inspect it
through the HTTP trace API:

```text
GET /traces/{trace_id}
```

The trace should include `realtime.backend.finished`; tool runs should include
`tool.started` and `tool.finished`; context-enabled runs should include
`context.report` or `context.build.finished`.

## Memory Safety

Realtime cancellation is a safety boundary:

- cancelled/interrupted turns must not write durable memory
- stale tool outputs must not become assistant-visible final text
- stale tool outputs must not create memory candidates
- fresh completed turns may write memory only through the normal governed tool
  path

The focused gate is:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_personal_realtime_assistant_runtime_gate.py -q
```

## Acceptance Commands

Run these before declaring the realtime runtime loop healthy:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py tests/test_gateway_api.py tests/test_realtime_call_simulator.py tests/test_personal_realtime_assistant_runtime_gate.py tests/test_realtime_runtime_operator_surface.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario all --quiet
```

For broader regression:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
```
