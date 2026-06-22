# Task 036 API 错误码与响应协议版本

## Goal

稳定 HTTP 和 WebSocket 对外协议，增加 protocol_version 与统一错误结构。

## Read first

- `docs/33-api-error-versioning.md`
- 当前 `schemas/requests.py`
- 当前 API routes
- 当前 websocket events

## Requirements

- HTTP AgentResponse 或 API wrapper 包含 `protocol_version`。
- 错误结构统一为 code/message/detail/recoverable。
- 常见错误码集中定义。
- WebSocket 错误事件使用同一错误结构。
- 不暴露内部 traceback 给用户。

## Tests

新增或更新：

```text
tests/test_api_error_versioning.py
tests/test_websocket_error_events.py
```

覆盖：

- success response protocol_version。
- invalid request error。
- provider_unconfigured error。
- websocket task_failed error。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 037。
