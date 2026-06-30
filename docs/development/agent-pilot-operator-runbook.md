# Agent Pilot Operator Runbook

Last updated: 2026-06-30

This runbook covers a constrained local/pilot operation of the Agent Control
Plane. It does not enable public remote agent fabric, automatic Agent Card
discovery, LLM target-agent selection, or default real provider calls.

## Scope

Use this runbook when validating:

- `/agent/run` as the stable single-agent path.
- `/agents/run` as the explicit local multi-agent gateway.
- `/.well-known/agent-card.json` and `/a2a/rpc` as inbound A2A-compatible adapters.
- `/control-plane/...` as the redacted observability, audit, budget, and replay surface.

All commands are designed to run from the repository root and avoid writing
secrets to tracked files.

## Preflight

```bash
PY=/home/lenovo1/miniconda3/envs/hello_agent/bin/python

$PY scripts/check_env.py
$PY scripts/check_pilot_readiness.py
```

Expected local default:

- `status` is `ready_with_warnings`.
- `operator_context.auth_mode` is `anonymous`.
- `operator_context.runtime_profile` is `local_demo`.
- The auth check warns because request-derived identity is only acceptable for local/offline use.

## Local Control-Plane Check

Before changing any provider or auth settings, confirm the local path is still
offline and mock-first:

```bash
$PY scripts/check_pilot_readiness.py --strict --auth-user-id pilot_01
```

This should return `ready` because the command simulates auth-bound identity but
does not enable real providers or remote agents.

## Pilot Environment

For a controlled pilot shell, export settings only in the current terminal:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=pilot
export MULTIMODAL_AGENT_AUTH_MODE=header_pilot
export MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY=true
export MULTIMODAL_AGENT_CHAT_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
```

Then run readiness with explicit identity and budget gates:

```bash
$PY scripts/check_pilot_readiness.py \
  --strict \
  --auth-user-id pilot_01 \
  --require-auth-bound-identity \
  --max-provider-calls-per-run 10 \
  --max-estimated-cost-per-run 1.00 \
  --max-input-bytes-per-run 2000000
```

If a remote A2A pilot agent is configured, validate it explicitly. This only
checks configuration; it does not call the remote endpoint:

```bash
$PY scripts/check_pilot_readiness.py \
  --auth-user-id pilot_01 \
  --require-auth-bound-identity \
  --remote-agent agent.remote=https://remote.example.test/a2a/rpc \
  --allowlisted-host remote.example.test
```

The check must be `blocked` when a remote A2A agent is present without an
allowlisted host.

## Start Server

Local mock server:

```bash
$PY scripts/run_server.py --provider mock --image-provider mock
```

Pilot server in the current shell:

```bash
$PY scripts/run_server.py \
  --host 127.0.0.1 \
  --port 8000 \
  --no-env-file \
  --trial-user-id pilot_01
```

Do not store API keys in `.env` or tracked files for this runbook flow.

## Verify Auth Mode

```bash
curl -s http://127.0.0.1:8000/control-plane/readiness \
  -H 'X-Multimodal-Agent-User-Id: pilot_01' \
  -H 'X-Multimodal-Agent-Session-Id: pilot_session'
```

Expected in production-required pilot mode:

- `auth_bound_identity` check is `passed` when the headers are present.
- Missing headers should produce a blocked readiness report.
- A body/path/query user mismatch must be rejected before runtime dispatch.

## Smoke Requests

Single-agent path:

```bash
curl -s http://127.0.0.1:8000/agent/run \
  -H 'content-type: application/json' \
  -H 'X-Multimodal-Agent-User-Id: pilot_01' \
  -H 'X-Multimodal-Agent-Session-Id: pilot_session' \
  -d '{"user_id":"pilot_01","session_id":"pilot_session","text":"你好，做一次单 Agent pilot smoke"}'
```

Explicit multi-agent gateway:

```bash
curl -s http://127.0.0.1:8000/agents/run \
  -H 'content-type: application/json' \
  -H 'X-Multimodal-Agent-User-Id: pilot_01' \
  -H 'X-Multimodal-Agent-Session-Id: pilot_session' \
  -d '{"user_id":"pilot_01","session_id":"pilot_session","text":"你好，做一次 gateway smoke","collaboration_mode":"single","target_agent_id":"agent.worker"}'
```

Inbound A2A-compatible smoke:

```bash
curl -s http://127.0.0.1:8000/a2a/rpc \
  -H 'content-type: application/json' \
  -H 'X-Multimodal-Agent-User-Id: pilot_01' \
  -H 'X-Multimodal-Agent-Session-Id: pilot_session' \
  -d '{
    "jsonrpc": "2.0",
    "id": "pilot-smoke-1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "你好，做一次 A2A inbound smoke"}],
        "metadata": {"user_id": "pilot_01", "session_id": "pilot_session"}
      }
    }
  }'
```

## Collect Redacted Evidence

Preferred local/offline evidence package:

```bash
$PY scripts/collect_pilot_evidence.py --strict \
  --output .local/pilot/evidence-local.json
```

Default behavior:

- disables dotenv loading for the script process.
- forces `local_demo` and mock providers.
- enables header-bound identity for in-process API calls.
- exercises `/agent/run`, `/agents/run`, inbound `/a2a/rpc`, agent card, readiness, audit, route, delegation, budget, replay-preview, and trace-summary APIs.
- does not start a server, call a real provider, or call a remote agent.

Use the `run_id` and `trace_id` from a response:

```bash
curl -s http://127.0.0.1:8000/control-plane/runs/<run_id>
curl -s http://127.0.0.1:8000/control-plane/runs/<run_id>/route
curl -s http://127.0.0.1:8000/control-plane/runs/<run_id>/delegation-tree
curl -s http://127.0.0.1:8000/control-plane/runs/<run_id>/budget
curl -s http://127.0.0.1:8000/control-plane/runs/<run_id>/audit
curl -s http://127.0.0.1:8000/control-plane/runs/<run_id>/replay-preview
curl -s http://127.0.0.1:8000/control-plane/traces/<trace_id>
```

The public evidence must not contain:

- raw auth tokens.
- raw provider responses.
- inline media or base64 bodies.
- raw memory content.
- hidden reasoning.
- parent conversation history in child-run views.

The evidence package is the Phase G cutoff artifact for this control-plane
stage. It proves the local pilot path is observable and redacted; it is not a
production audit export and does not replace durable audit storage.

## Back Out

Stop the server, then remove pilot-only environment variables from the current
shell:

```bash
unset MULTIMODAL_AGENT_RUNTIME_PROFILE
unset MULTIMODAL_AGENT_AUTH_MODE
unset MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY
unset MULTIMODAL_AGENT_CHAT_PROVIDER
unset QWEN_API_KEY
```

Confirm local default behavior:

```bash
$PY scripts/check_pilot_readiness.py
$PY scripts/collect_pilot_evidence.py --strict
$PY scripts/check_env.py
$PY -m pytest tests/test_agent_pilot_readiness.py tests/test_api_agent_graph_runtime.py
```

## Current Limits

- Control-plane audit storage is process-local and lasts only for the running server process.
- `scripts/collect_pilot_evidence.py` collects a local/offline same-process evidence package; it is not a load test and not a durable audit export.
- Remote A2A validation in `scripts/check_pilot_readiness.py` is configuration-only.
- JWT and server-side session auth providers are deferred stubs.
- Real provider smoke remains opt-in and must use local shell environment variables.
