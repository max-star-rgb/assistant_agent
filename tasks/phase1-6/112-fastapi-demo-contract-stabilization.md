# Task 112 FastAPI Demo Contract Stabilization

## Goal

稳定 FastAPI demo 接口，使本地 Web / API 演示可用。

## Read first

- `docs/117-phase6b-api-web-console-roadmap.md`
- 当前 `src/multimodal_agent/api/`
- 当前 API tests

## Requirements

- 确认 `POST /agent/run` 可用于 demo。
- 增加或确认 `GET /demo/scenarios`。
- 确认 `GET /runs/{run_id}` / `GET /traces/{trace_id}` 可用或有 service-level fallback。
- API 输出 protocol_version、run_id、trace_id、response_text、tool_calls、errors。
- 默认 mock/local。

## Acceptance

```bash
python -m pytest
```
