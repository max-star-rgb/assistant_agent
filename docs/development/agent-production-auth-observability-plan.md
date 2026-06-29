# Agent Production Auth And Observability Plan

Last updated: 2026-06-29

Status: planned.

This plan defines the next hardening stage after the local multi-agent gateway,
inbound A2A adapter, default-disabled outbound A2A pilot, request identity
boundary, and header-auth pilot are in place.

The goal is not to add more agents. The goal is to make the existing control
plane auditable, identity-bound, queryable, and ready for a constrained pilot.

## Goal

Build a production-auth-ready and observability-ready local Agent Control Plane:

```text
/agent/run
  remains the stable default single-agent entrypoint

/agents/run
  remains the explicit local multi-agent gateway

/.well-known/agent-card.json + /a2a/rpc
  remain inbound A2A-compatible protocol adapters

AuthContext
  becomes the single trusted identity boundary for production/pilot modes

Trace / run / pilot-readiness views
  become queryable, redacted, and useful for audit/replay
```

## Non-Goals

Do not implement these in this stage:

- Public remote agent network fabric.
- Agent marketplace.
- Automatic Agent Card discovery and enablement.
- LLM-only target-agent selection.
- Default remote A2A calls.
- Default real provider calls.
- More local agents such as `agent.vision`, `agent.product`, or `agent.render`.
- Moving A2A schema into `AgentGraphRuntime` or assistant loop internals.

## Current Baseline

Implemented baseline:

- `/agent/run` uses the default single `AgentGraphRuntime`.
- `/agents/run` uses `AgentGateway` for explicit local multi-agent routing.
- `agent.default` and `agent.worker` exist in the local gateway.
- `delegate_to_agent` is registry-level opt-in and enabled only for the gateway controller runtime.
- `AgentDirectory`, deterministic routing policy, delegation policy, delegation context filtering, and gateway route decision metadata exist.
- Inbound `/.well-known/agent-card.json` and `/a2a/rpc` expose an A2A-compatible adapter over the local gateway.
- Outbound `A2AJsonRpcTransport` exists but is default-disabled and requires explicit endpoint and allowlist configuration.
- `services/api_identity.py` centralizes request identity resolution.
- `api/auth.py` returns anonymous `AuthContext` by default.
- Header-auth pilot is disabled by default and enabled only with `MULTIMODAL_AGENT_AUTH_HEADER_ENABLED`.
- `IdentityPolicy` can classify request-derived identity as warning/failed and auth-bound identity as passed.
- `PilotReadinessChecker` reports runtime profile, remote A2A opt-in, identity policy, and trace redaction checks.
- Trace/run query APIs, memory audit APIs, and local observability docs already exist, but they are not yet a unified control-plane audit surface.

## Architecture Boundary

Trusted identity must flow through one boundary:

```text
HTTP / WebSocket / A2A request
  -> api/auth.py
  -> AuthContext
  -> resolve_request_identity(...)
  -> RequestIdentity
  -> route/runtime/memory/gateway services
```

Rules:

- Production or production-like pilot identity must be auth-bound.
- Body/path/query/A2A metadata `user_id` is request-derived input, not trusted auth.
- If `AuthContext.authenticated=True`, request-supplied `user_id` must match the auth principal unless a route explicitly documents a safer migration exception.
- `session_id`, `tenant_id`, `project_id`, and `allowed_scopes` should come from `AuthContext` in production-like modes.
- Memory tools and memory APIs must use bound `RequestIdentity`; model-supplied user identity must not override runtime context.
- Inbound A2A must map auth-bound identity into internal `AgentGatewayRunRequest` before dispatch.
- Outbound A2A must not receive auth secrets or raw auth tokens unless an explicit remote auth provider is implemented and allowlisted.

## Phase A: Contract And Policy Freeze

Goal: Make production auth and observability semantics explicit before adding new behavior.

Work:

- Add API contract notes for auth-bound identity across `/agent/run`, `/agents/run`, `/a2a/rpc`, WebSocket, memory APIs, and beta APIs.
- Define supported auth modes:
  - `anonymous_local`: current default local/offline behavior.
  - `header_pilot`: current disabled-by-default controlled-header pilot.
  - `trusted_header`: future reverse-proxy or gateway-bound header mode.
  - `jwt`: future signed-token mode.
  - `session`: future server-side session mode.
- Define production-required behavior:
  - request-derived identity is rejected when production identity is required.
  - auth/body mismatch returns structured `403` for HTTP and JSON-RPC `-32602` for `/a2a/rpc`.
  - WebSocket auth mismatch emits `agent_error` and closes with policy violation.
- Define safe metadata:
  - `identity_source`
  - `auth_bound_identity`
  - `auth_context_source`
  - requested user/session
  - warnings
  - no raw token/header values
- Extend docs for local/pilot/production identity expectations.

Acceptance:

- Existing local/offline behavior stays unchanged.
- Header-auth pilot remains disabled by default.
- Production-required identity policy has explicit tests.
- No real JWT/session implementation is introduced in this phase.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_trial_access.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_api_a2a.py \
  tests/test_websocket_graph_runtime.py \
  tests/test_agent_pilot_readiness.py
```

## Phase B: Auth Provider Adapter Boundary

Goal: Add a replaceable auth provider boundary without turning local defaults into production auth.

Work:

- Introduce an `AuthProvider` protocol or service boundary behind `api/auth.py`.
- Keep anonymous local provider as the default.
- Add explicit configured provider modes for:
  - trusted internal headers from a reverse proxy or gateway.
  - signed JWT validation, when dependencies/config are available.
  - test provider for deterministic tests.
- Add config names that make unsafe defaults obvious:
  - `MULTIMODAL_AGENT_AUTH_MODE=anonymous|header_pilot|trusted_header|jwt|session`
  - `MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY=true|false`
- Ensure keys/secrets are read only from environment or safe local config and never written to the repo.
- Add structured auth errors with stable codes.

Acceptance:

- Default remains anonymous/local.
- Setting random auth headers does nothing unless auth mode enables them.
- Production-required mode rejects request-derived identity.
- JWT/session mode can be stubbed behind tests without requiring network or real secrets.
- All auth decisions are represented in redacted request metadata or trace metadata.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_trial_access.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_api_a2a.py \
  tests/test_websocket_graph_runtime.py \
  tests/test_memory_audit_api.py
```

## Phase C: Control-Plane Observability API

Goal: Provide redacted query surfaces for run, gateway, delegation, identity, budget, and readiness state.

Work:

- Add or extend read-only control-plane endpoints for:
  - run summary by `run_id`.
  - trace summary by `trace_id`.
  - gateway route decision by run.
  - delegation tree by parent run.
  - pilot readiness report.
  - failure replay preview payload.
  - budget/cost/latency summary.
- Keep responses redacted by default:
  - no raw provider responses.
  - no full auth tokens.
  - no base64/media payloads.
  - no hidden reasoning.
  - no raw parent conversation history in child-run views.
- Add stable response schemas for observability views instead of ad hoc dictionaries.
- Preserve existing `/runs/{run_id}` and `/traces/{trace_id}` compatibility.

Acceptance:

- A `/agents/run` response can be followed to a redacted route/delegation summary.
- A failed inbound A2A request can be diagnosed as protocol failure, gateway failure, controller failure, worker failure, tool failure, or provider failure.
- Identity provenance is visible but sanitized.
- Budget/latency summaries are present for local delegation and outbound A2A pilot failures.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_trace_query_api.py \
  tests/test_agent_gateway.py \
  tests/test_agent_pilot_readiness.py \
  tests/test_api_agent_graph_runtime.py \
  tests/test_api_a2a.py
```

## Phase D: Audit Events And Replay Hygiene

Goal: Make pilot failures replayable enough for debugging without leaking secrets or raw payloads.

Work:

- Add structured audit events for:
  - auth decision.
  - route decision.
  - delegation allowed/blocked.
  - remote A2A blocked/allowed.
  - provider opt-in decision.
  - memory access/export/delete where relevant.
- Attach correlation IDs across parent and child runs.
- Store only replay-safe previews and references.
- Add redaction tests for:
  - authorization headers.
  - API keys.
  - raw provider responses.
  - base64/media bodies.
  - raw memory content unless explicitly requested and sanitized.
- Define retention and export behavior for audit events separately from long-term memory.

Acceptance:

- Replay payloads can reconstruct request shape, route, budgets, failure class, and refs without raw sensitive payloads.
- Audit event views are user/session scoped where user data is involved.
- Cross-agent delegation can be reconstructed as a parent/child tree.
- Redaction tests fail if secret-looking values appear in public responses.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_pilot_readiness.py \
  tests/test_trace_redaction.py \
  tests/test_sensitive_redaction.py \
  tests/test_memory_audit_api.py
```

## Phase E: Pilot Profile And Operational Runbook

Goal: Make a small controlled pilot operable without changing default developer behavior.

Work:

- Define `pilot` profile requirements:
  - explicit real provider opt-in.
  - auth-bound identity required.
  - remote A2A allowlist required if remote agents are configured.
  - trace redaction required.
  - cost/budget limits required.
- Add a pilot readiness command or API view that returns `ready`, `ready_with_warnings`, or `blocked`.
- Add an operator runbook for:
  - starting local/pilot server.
  - verifying auth mode.
  - checking readiness.
  - sending `/agent/run`, `/agents/run`, and `/a2a/rpc` smoke requests.
  - collecting redacted run/trace/delegation summaries.
  - backing out to anonymous local mode.

Acceptance:

- Default local/mock/offline path remains green.
- Pilot mode fails closed when auth-bound identity is missing.
- Real provider or remote agent calls remain impossible unless explicit profile/config/allowlist gates are satisfied.
- Operator docs include commands that do not write secrets to the repo.

Suggested validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
```

## Phase F: Optional UI Surface

Goal: Add a local operator-friendly view only after backend contracts are stable.

Work:

- Add a read-only Web Console view for:
  - current runtime profile.
  - pilot readiness.
  - recent runs.
  - selected run trace.
  - gateway route decision.
  - delegation tree.
  - redaction status.
- Keep the UI local-first and read-only for pilot observability.
- Do not add remote control actions, secret entry, or real provider toggles in the UI.

Acceptance:

- UI uses the same observability APIs as tests.
- Text does not expose raw secrets or raw provider payloads.
- Default demo console behavior remains unchanged.

## Implementation Order

Recommended sequence:

1. Phase A: contract and policy freeze.
2. Phase B: auth provider boundary.
3. Phase C: control-plane observability API.
4. Phase D: audit events and replay hygiene.
5. Phase E: pilot profile and operator runbook.
6. Phase F: optional read-only UI.

Do not begin Phase F before Phases A-C are test-covered.

## Success Criteria

This stage is complete when:

- Default local/offline behavior is unchanged and fully tested.
- Production-required identity rejects request-derived identity.
- Auth-bound identity works consistently for HTTP, WebSocket, memory APIs, and inbound A2A.
- Gateway route decisions and delegation trees are queryable and redacted.
- Pilot readiness reflects runtime profile, auth, remote allowlist, redaction, and budget gates.
- Failure replay payloads are useful for debugging and safe to store.
- Operator docs describe how to run, verify, observe, and back out of a pilot.

## Open Questions

- Which production auth mode should be implemented first: trusted proxy headers, JWT, or server-side session?
- Should `AuthContext.allowed_scopes` become the single source for memory API authorization, or remain advisory until a larger permission model exists?
- Should observability events be stored in the existing `TraceStore`, a new audit store, or memory-audit-adjacent storage?
- What is the minimum useful retention policy for pilot audit events?
- Should A2A inbound Agent Card advertise auth requirements differently when production auth is required?

