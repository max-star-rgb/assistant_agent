# Task 135-1 Shared Assistant Run Backend for CLI and Web

## Goal

Unify `scripts/demo_assistant_loop.py` and Web API execution on one shared backend run service.

## Read first

- `docs/135-1-shared-assistant-run-backend.md`
- `scripts/demo_assistant_loop.py`
- `src/multimodal_agent/api/routes_agent.py`
- `src/multimodal_agent/agent/runtime.py`

## Scope

- Shared `.env` loading.
- Shared runtime creation.
- Shared response payload formatting.
- Add `runtime_info`, `current_stage`, and `blocked_reason` to API response.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

Complete this task, then continue to Task 135-2 only because the user explicitly requested it.
