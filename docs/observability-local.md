# Local Observability

This project exposes minimal local observability for demo and debugging. It does not include a production monitoring stack.

## Healthcheck

```text
GET /health
```

Expected response:

```json
{"status": "ok"}
```

CLI check:

```bash
python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health')))"
```

## Agent Run IDs

Every `POST /agent/run` response includes:

```text
run_id
trace_id
status
intent
response_text
tool_calls
tool_results
errors
```

Keep `run_id` and `trace_id` when debugging a demo run.

## Run Summary

```text
GET /runs/{run_id}
```

Use this to inspect:

- graph node path
- tools used
- providers used
- error count
- budget exceeded flag
- retry count
- event count

## Trace Summary

```text
GET /traces/{trace_id}
```

Use this to inspect redacted graph execution events. Trace payloads are sanitized and must not include API keys, authorization headers, complete base64 payloads, raw Provider responses, or sensitive local paths.

## Tool Calls

```text
GET /runs/{run_id}/tool-calls
```

Use this to inspect:

- tool name
- capability
- provider
- model
- status
- latency
- error code
- redacted input summary
- redacted output summary

## Provider Errors

Use this section when debugging provider errors in local demo runs.

Provider errors are returned through the stable API error shape:

```text
code
message
detail
recoverable
```

Common local debug codes include:

- `PROVIDER_UNCONFIGURED`
- `PROVIDER_TIMEOUT`
- `PROVIDER_UNAVAILABLE`
- `PROVIDER_BUDGET_EXCEEDED`
- `TASK_FAILED`

## Budget Errors

Budget and call-limit failures are visible through:

- `errors` in `POST /agent/run`
- `budget_exceeded` in `GET /runs/{run_id}`
- `budget_exceeded` in `GET /traces/{trace_id}`

## Memory Operations

Memory-related behavior can be checked through:

- `memory_retrieval` tool calls
- `memory_save` / `memory` tool results when present
- run and trace summaries for memory-capability execution

Local memory configuration is documented in:

```text
docs/configuration.md
```

## Boundaries

- No Prometheus or Grafana stack.
- No cloud logging service.
- No raw Provider response dumps.
- No real Provider calls by default.
- No sensitive file paths in public API outputs.
