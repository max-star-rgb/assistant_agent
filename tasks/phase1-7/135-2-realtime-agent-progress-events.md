# Task 135-2 Realtime Agent Progress Events for Web UI

## Goal

Make WebSocket progress events stream while the agent is running.

## Read first

- `docs/135-2-realtime-agent-progress-events.md`
- `src/multimodal_agent/api/websocket.py`
- `src/multimodal_agent/services/event_sink.py`
- `src/multimodal_agent/schemas/events.py`

## Scope

- Keep using existing `AgentEvent`.
- Stream runtime events from a shared backend runtime path.
- Keep default tests offline.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

Stop after this task. Do not proceed to the next Phase 7 track.
