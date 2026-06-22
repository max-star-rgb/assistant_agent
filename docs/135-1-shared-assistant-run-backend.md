# 135-1 Shared Assistant Run Backend

## Goal

Make CLI smoke and Web UI use the same backend run path for ReAct assistant execution.

## Scope

- Shared `.env` loading.
- Shared `AgentGraphRuntime` creation.
- Shared run result formatting.
- Shared `react_steps`, runtime info, current stage, and blocked reason.
- Keep default tests offline.

## Out of Scope

- No new provider.
- No provider credential changes.
- No new capability.
- No production auth.

## Contract

The shared run payload includes:

```text
response_text
react_steps
tool_calls
errors
run_id
trace_id
runtime_info
current_stage
blocked_reason
```

CLI and Web API should both derive their output from this shared service.
