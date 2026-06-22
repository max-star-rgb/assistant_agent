# 135-2 Realtime Agent Progress Events

## Goal

Expose live agent progress for Web UI users so they can see where the assistant is running or blocked.

## Scope

- WebSocket route streams runtime events during execution.
- Events come from the same shared backend runtime path.
- Preserve existing event schema.
- Keep tests offline.

## Out of Scope

- No streaming LLM token deltas.
- No new provider.
- No auth or multi-user access control.

## Event Expectations

The Web UI can show progress from:

```text
task_started
graph_node_started
tool_started
tool_finished / tool_failed
graph_node_finished
final_response / task_failed
```

The route should not wait for the whole run before sending the first event.
