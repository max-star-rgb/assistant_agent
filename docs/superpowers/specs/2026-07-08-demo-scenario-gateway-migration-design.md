# Demo Scenario Gateway Migration Design

Date: 2026-07-08

## Goal

Move offline demo scenarios, including local CLI `--scenario`, behind Gateway
while preserving the existing demo summary JSON contract.

Target internal path:

```text
scripts/run_demo_flows.py / run_assistant_cli.py --scenario
    -> demo scenario entry adapter
    -> GatewayTurnFacade
    -> GatewaySessionManager / GatewaySessionService
    -> GatewayAgentAdapter
    -> AssistantRuntimeApp
    -> run_assistant_request
    -> AgentGraphRuntime
```

## Problem

`scripts/run_demo_flows.py` still constructs `AgentGraphRuntime` directly. The
local CLI `--scenario` path reuses that runner, so one CLI/demo path bypasses
Gateway even though HTTP `/agent/run`, local CLI `--text`, and legacy
`/ws/agent/{session_id}` already use Gateway internally.

That keeps a hidden runtime entrypoint alive and makes observer integration
harder: a scenario run can exercise tools, memory, media references, and trace
creation without passing through Gateway lifecycle and session history.

## Approach

Keep the public demo runner contract unchanged and change only its internal run
path.

For each selected scenario, `run_scenario()` creates a local
`GatewaySessionManager(start_reaper=False)` with a `GatewayAgentAdapter`
backend. The backend callback uses `AssistantRuntimeApp.run_request()` and
captures the returned `AssistantRunArtifacts`. `GatewayTurnFacade.run_turn()`
sends the scenario text and media references as a normalized `message.user`
frame and waits for `run.end`.

The result payload continues to come from the captured assistant artifacts, not
from Gateway wire frames. This keeps demo fields such as `tool_sequence`,
`errors`, `run_id`, `trace_id`, and `checks` identical to the current contract
while proving the request passed through Gateway session lifecycle.

Scenario metadata remains prompt-safe:

- media reference lists are moved to `image_ids` and `video_ids`;
- `offline=true` is added for local/demo visibility;
- `gateway.suppress_realtime_backend_source=true` keeps the realtime adapter
  from adding a misleading default source;
- no real provider profile is enabled.

## Eval Boundary

`scripts/run_evals.py` remains a direct offline eval harness in this phase.
It contains several deterministic eval modes that intentionally test lower
layers directly: rule routing, scripted native tool-call plan mode, provider
safety, memory retrieval, and MCP packaging. Migrating all eval modes through
Gateway would conflate product entry unification with lower-layer regression
tests.

The architecture docs should state this exception explicitly: demo/scenario
paths are entry adapters and should use Gateway, while eval harnesses may call
runtime or lower-layer services directly when their purpose is to measure those
layers.

## Error Handling

- Unknown scenario ids keep returning the existing `ValueError` / CLI exit
  code 2 behavior.
- The local Gateway manager is closed after each scenario run.
- If Gateway reaches `run.end` but the backend callback captured no assistant
  artifacts, the scenario runner raises a clear runtime error.
- Existing sanitization of sensitive keys and base64-like values remains in
  place.

## Testing

- Add a demo runner test that monkeypatches `create_runtime()` with a recording
  runtime, runs one scenario, and asserts the runtime request includes
  `metadata["runtime"]["history"]`. This proves the demo run entered
  `GatewaySessionService`.
- Keep existing demo runner JSON shape tests green.
- Keep existing CLI `--scenario` test green because it reuses the demo matrix.
- Add `scripts/run_demo_flows.py` to product entry boundary tests so it cannot
  reintroduce a direct `AgentGraphRuntime` import.

## Stop Point

Stop this phase after demo/scenario paths are Gateway-backed and eval is
documented as a deliberate offline harness exception. The next phase should be
an observer readiness checkpoint unless another product entrypoint still
bypasses Gateway.
