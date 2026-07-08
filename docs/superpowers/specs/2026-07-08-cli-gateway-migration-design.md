# CLI Gateway Migration Design

Date: 2026-07-08

## Goal

Move the local offline CLI `--text` path behind Gateway while preserving its
current JSON/text output contract.

Target path for this phase:

```text
scripts/run_assistant_cli.py --text
    -> local CLI entry adapter
    -> GatewayTurnFacade
    -> GatewaySessionManager / GatewaySessionService
    -> GatewayAgentAdapter
    -> AssistantRuntimeApp
    -> run_assistant_request
    -> AgentGraphRuntime
```

## Scope

Only `scripts/run_assistant_cli.py --text` is in scope.

Out of scope:

- `--scenario`, because it reuses the demo matrix and should be migrated with
  demo/eval paths.
- Remote `scripts/run_client.py`, because HTTP `/agent/run` already enters
  Gateway and legacy `/ws/agent` needs its own WebSocket phase.
- Observer wiring.

## Approach

The CLI remains in-process and offline. It creates a local
`GatewaySessionManager(start_reaper=False)` and a `GatewayTurnFacade`. The
Gateway backend is `GatewayAgentAdapter(run_request=local_run_request)`, where
`local_run_request` delegates to `AssistantRuntimeApp.run_request()` with
`load_env=False` and captures the returned `AssistantRunArtifacts`.

After `GatewayTurnFacade.run_turn()` reaches `run.end`, the CLI formats the
captured artifacts into the same payload it returns today:

- `status`
- `intent`
- `response_text`
- `tool_sequence`
- `run_id`
- `trace_id`
- `errors`
- `offline`
- `checks.non_generic_response`

## Metadata

The CLI sends prompt-safe metadata:

- `offline=True`
- `gateway.suppress_realtime_backend_source=True`

The old `source="assistant_cli"` metadata is not reintroduced as a trusted
Gateway source. Gateway strips ordinary user-provided `source`, `channel`, and
`system_prompt_profile`; this phase keeps that safety rule instead of adding a
CLI-specific exception.

## Error Handling

- If Gateway reaches `run.end` but no artifacts were captured, raise
  `ValueError("Gateway CLI run completed without assistant artifacts.")`.
- Always close the local `GatewaySessionManager` after the run.
- Keep `main()` behavior unchanged: `ValueError` becomes exit code `2` with a
  JSON error body.

## Testing

- Extend `tests/test_assistant_cli.py` with a recording runtime test that
  asserts `run_text_prompt()` sends the final runtime request through Gateway by
  checking `request.metadata["runtime"]["history"]`.
- Keep existing CLI subprocess JSON/text tests green.
- Run Gateway-focused tests and fast tests.

## Stop Point

Stop this phase after local CLI `--text` is Gateway-first and existing CLI
outputs remain stable. The next phase should migrate legacy `/ws/agent` or
demo/eval paths separately. Observer integration should still wait.
